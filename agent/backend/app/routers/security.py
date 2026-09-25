"""宿主机安全防护 API（/agent/security）：fail2ban SSH 防爆破 + 防火墙端口策略。

鉴权：X-Node-Token（主控）或 X-API-Key（OPEN_API_KEY / PANEL_API_KEY 开放对接）。
防火墙部分复用 agent.py 的 ufw/firewalld 实现，供 open API 对接方调用。
"""
import os
import re as _re
import shutil
import subprocess as _sp
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app import tasks
from app.agent_auth import require_node_or_api
from app.routers.agent import (_run, firewall_allow, firewall_delete, firewall_deny,
                               firewall_status, firewall_toggle, PortRule)

router = APIRouter(dependencies=[Depends(require_node_or_api)])

JAIL_CONF = "/etc/fail2ban/jail.d/ppanel-sshd.local"


# ---------- fail2ban（SSH 防爆破） ----------

class Toggle(BaseModel):
    enable: bool


class IpIn(BaseModel):
    ip: str


def _f2b_socket() -> str:
    """server 监听的 socket：读 fail2ban.conf 的 socket= 行（宝塔等面板会改到
    私有路径），未配置则用默认。与 server 端始终配套（同一份 conf）。"""
    try:
        for line in open("/etc/fail2ban/fail2ban.conf", encoding="utf-8"):
            line = line.strip()
            if line.startswith("socket") and "=" in line and not line.startswith("#"):
                v = line.split("=", 1)[1].strip()
                if v:
                    return v
    except OSError:
        pass
    return "/var/run/fail2ban/fail2ban.sock"


def _f2b(args: list, timeout: int = 10):
    """fail2ban-client 统一带 -s：宝塔等面板可能把 client 替换成 wrapper、
    默认 socket 指向面板私有路径，显式指定系统 server 的 socket 才能连上。"""
    return _run(["fail2ban-client", "-s", _f2b_socket()] + args, timeout=timeout)


def _f2b_installed() -> bool:
    return bool(shutil.which("fail2ban-client"))


def _f2b_version() -> str:
    rc, out = _run(["fail2ban-client", "--version"], timeout=8)  # --version 纯本地，不连 socket
    return out.strip().splitlines()[0] if rc == 0 else ""


def _f2b_active() -> bool:
    rc, out = _run(["systemctl", "is-active", "fail2ban"], timeout=8)
    return rc == 0 and out.strip() == "active"


@router.get("/security/fail2ban")
def fail2ban_status():
    """fail2ban 安装/运行状态 + sshd jail 封禁情况。"""
    out = {"installed": _f2b_installed(), "active": False, "version": "",
           "jails": [], "sshd": None, "error": None}
    if not out["installed"]:
        return out
    out["version"] = _f2b_version()
    out["active"] = _f2b_active()
    if not out["active"]:
        out["error"] = "fail2ban 服务未运行，可点击「安装/启用」修复"
        return out
    rc, out_all = _f2b(["status"])
    if rc == 0:
        m = _re.search(r"Jail list:\s*(\S.*)$", out_all, _re.M)
        out["jails"] = [j.strip() for j in m.group(1).split(",")] if m else []
    out["sshd"] = _sshd_status()
    return out


def _sshd_status() -> dict:
    """解析 fail2ban-client status sshd 的失败计数与封禁列表。"""
    rc, out = _f2b(["status", "sshd"])
    if rc != 0:
        last = (out or "").strip().splitlines()
        return {"enabled": False, "error": (last[-1] if last else "sshd jail 未启用")[:200]}
    def num(name: str) -> int:
        m = _re.search(rf"{name}:\s*(\d+)", out)
        return int(m.group(1)) if m else 0
    m = _re.search(r"Banned IP list:\s*(.*)", out)
    return {"enabled": True, "error": None,
            "currently_failed": num("Currently failed"),
            "total_failed": num("Total failed"),
            "currently_banned": num("Currently banned"),
            "total_banned": num("Total banned"),
            "banned": m.group(1).split() if m and m.group(1).strip() else []}


def _install_fail2ban(tid: str = "") -> str | None:
    """包管理器安装 fail2ban。返回 None=成功，否则错误信息。"""
    apt = shutil.which("apt-get")
    if apt:
        env = {**os.environ, "DEBIAN_FRONTEND": "noninteractive"}
        for attempt in (1, 2):  # 第一次失败先 update 刷新索引再试
            if tid:
                tasks.log(tid, f"apt 安装 fail2ban（第 {attempt} 次尝试，最长 5 分钟）…")
            p = _sp.run([apt, "install", "-y", "-qq", "fail2ban"], env=env,
                        capture_output=True, text=True, timeout=300)
            if p.returncode == 0:
                return None
            if attempt == 1:
                if tid:
                    tasks.log(tid, "安装失败，apt update 刷新索引后重试…")
                _sp.run([apt, "update", "-qq"], capture_output=True, timeout=180)
        return ((p.stderr or "") + (p.stdout or "")).strip()[-300:] or "apt 安装失败"
    for mgr in ("dnf", "yum"):
        if shutil.which(mgr):
            # redhat 系 fail2ban 在 EPEL
            if tid:
                tasks.log(tid, f"{mgr} 安装 epel-release 与 fail2ban…")
            _sp.run([mgr, "install", "-y", "epel-release"], capture_output=True, timeout=300)
            p = _sp.run([mgr, "install", "-y", "fail2ban"], capture_output=True,
                        text=True, timeout=300)
            return None if p.returncode == 0 else ((p.stderr or "") + (p.stdout or "")).strip()[-300:] or "安装失败"
    return "未识别的包管理器（需要 apt/dnf/yum）"


@router.post("/security/fail2ban/setup")
def fail2ban_setup(request: Request = None):
    """安装（如缺）fail2ban + 写入 sshd 防爆破策略（失败 5 次封 10 分钟）+ 启用。

    先用 systemd backend（现代发行版无 auth.log 也能取日志）；
    服务起不来（如无 sshd 单元的环境）自动降级默认 backend 重试一次。"""
    tid = (request.headers.get("x-task-id") if request else "") or ""
    if tid:
        tasks.start(tid, "安装/启用 fail2ban（SSH 防爆破）")
    try:
        if not _f2b_installed():
            if tid:
                tasks.log(tid, "未检测到 fail2ban，开始安装…")
            err = _install_fail2ban(tid)
            if err:
                raise HTTPException(status_code=502, detail="fail2ban 安装失败：" + err)
        if tid:
            tasks.log(tid, "写入 sshd 防爆破策略（失败 5 次封 10 分钟）…")
        os.makedirs(os.path.dirname(JAIL_CONF), exist_ok=True)
        active = False
        for backend in ("backend = systemd\n", ""):
            with open(JAIL_CONF, "w", encoding="utf-8") as f:
                f.write("# PPanel SSH 防爆破策略（由节点面板管理）\n"
                        "[sshd]\nenabled = true\n" + backend +
                        "maxretry = 5\nfindtime = 10m\nbantime = 10m\n")
            _run(["systemctl", "enable", "fail2ban"], timeout=30)
            _run(["systemctl", "restart", "fail2ban"], timeout=30)
            # systemd Type=simple 启动即 active，初始化失败要等一会才暴露，不能立刻判定
            time.sleep(2.5)
            if _f2b_active():
                active = True
                break
        if not active:
            raise HTTPException(status_code=502, detail="fail2ban 已安装但服务启动失败，"
                                "请检查 journalctl -u fail2ban（sshd jail 日志源不可用）")
        if tid:
            tasks.finish(tid, True, "✔ fail2ban 已安装并启用")
        return fail2ban_status()
    except Exception as e:  # noqa: BLE001
        if tid:
            tasks.finish(tid, False, f"✘ 设置失败：{e}")
        raise


@router.post("/security/fail2ban/toggle")
def fail2ban_toggle(body: Toggle):
    if not _f2b_installed():
        raise HTTPException(status_code=400, detail="fail2ban 未安装，请先执行安装/启用")
    rc, out = _run(["systemctl", "enable" if body.enable else "disable", "--now", "fail2ban"],
                   timeout=30)
    if rc != 0:
        raise HTTPException(status_code=502, detail=(out or "执行失败").strip()[:300])
    return fail2ban_status()


def _check_ip(ip: str) -> None:
    parts = (ip or "").split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return
    if ":" in (ip or "") and _re.fullmatch(r"[0-9A-Fa-f:]{2,45}", ip):
        return
    raise HTTPException(status_code=400, detail="IP 格式无效")


@router.post("/security/fail2ban/ban")
def fail2ban_ban(body: IpIn):
    _check_ip(body.ip)
    if not _f2b_active():
        raise HTTPException(status_code=400, detail="fail2ban 服务未运行")
    rc, out = _f2b(["set", "sshd", "banip", body.ip])
    if rc != 0:
        raise HTTPException(status_code=502, detail=(out or "封禁失败").strip()[:300])
    return {"ok": True, "detail": f"已封禁 {body.ip}"}


@router.post("/security/fail2ban/unban")
def fail2ban_unban(body: IpIn):
    _check_ip(body.ip)
    if not _f2b_active():
        raise HTTPException(status_code=400, detail="fail2ban 服务未运行")
    rc, out = _f2b(["set", "sshd", "unbanip", body.ip])
    if rc != 0:
        raise HTTPException(status_code=502, detail=(out or "解封失败").strip()[:300])
    return {"ok": True, "detail": f"已解封 {body.ip}"}


# ---------- 防火墙（复用 agent.py 实现，开放给 X-API-Key 对接方） ----------

@router.get("/security/firewall")
def sec_firewall_status():
    return firewall_status()


@router.post("/security/firewall/allow")
def sec_fw_allow(body: PortRule):
    return firewall_allow(body)


@router.post("/security/firewall/deny")
def sec_fw_deny(body: PortRule):
    return firewall_deny(body)


@router.post("/security/firewall/delete")
def sec_fw_delete(body: dict):
    return firewall_delete(body)


@router.post("/security/firewall/toggle")
def sec_fw_toggle(body: dict):
    return firewall_toggle(body)
