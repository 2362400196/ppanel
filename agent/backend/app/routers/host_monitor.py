"""宿主机运维监控（前缀 /agent，X-Node-Token 鉴权，由 agent_main router 级依赖承担）：

监控曲线、流量统计、阈值告警配置、端口监听、进程管理、systemd 服务、
journal 日志、登录审计、SSL 证书到期检测。全部零第三方依赖（subprocess + /proc）。
"""
import os
import re as _re
import ssl as _ssl
import socket as _socket
import subprocess as _sp
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.models import HostAlertLog, HostMetric, Instance
from app.services import host_metrics_service as hms

router = APIRouter()

_VIRTUAL_IF = ("lo", "docker", "veth", "br-", "virbr", "tun", "tap", "kube", "cni",
               "flannel", "vpp", "cali", "wg", "zt")


def _resolve(binname: str) -> str:
    """systemd 环境 PATH 可能缺 /usr/sbin，兜底常见 sbin 路径。"""
    import shutil
    p = shutil.which(binname)
    if p:
        return p
    for pre in ("/usr/sbin", "/sbin", "/usr/local/sbin", "/usr/bin"):
        cand = f"{pre}/{binname}"
        if os.path.exists(cand):
            return cand
    return binname


def _run(cmd: list, timeout: int = 15) -> tuple[int, str]:
    """列表参数 subprocess（防注入），LC_ALL=C 保证输出可解析。"""
    env = dict(os.environ, LC_ALL="C")
    try:
        p = _sp.run([_resolve(cmd[0])] + cmd[1:], capture_output=True,
                    text=True, timeout=timeout, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, "command not found"
    except _sp.TimeoutExpired:
        return 124, "命令超时"


# ---------- 监控曲线 ----------

@router.get("/host/metrics")
def host_metrics(hours: int = 24, db: Session = Depends(get_db)):
    """历史曲线：24h（5 分钟粒度）/ 7d（30 分钟粒度），含 24h 收发流量合计。"""
    hours = 168 if hours > 24 else max(1, min(hours, 24))
    bucket_min = 5 if hours <= 24 else 30
    since = datetime.utcnow() - timedelta(hours=hours)

    rows = (db.query(HostMetric)
            .filter(HostMetric.ts >= since)
            .order_by(HostMetric.ts.asc()).all())
    if not rows:
        return {"points": [], "totals": {"rx_mb": 0, "tx_mb": 0}, "bucket_min": bucket_min}

    # 按桶聚合（均值 + 首末差），避免 7 天 1 万条原始点拖垮前端
    buckets: dict = {}
    for r in rows:
        key = r.ts.replace(second=0, microsecond=0)
        key = key - timedelta(minutes=key.minute % bucket_min)
        b = buckets.setdefault(key, [])
        b.append(r)
    points = []
    for key, items in buckets.items():
        n = len(items)
        mem_total = items[-1].mem_total_mb or 0
        points.append({
            "ts": key.isoformat(),
            "cpu": round(sum(i.cpu_percent for i in items) / n, 1),
            "mem": round(sum(i.mem_used_mb for i in items) / n / mem_total * 100, 1) if mem_total else 0,
            "disk": round(items[-1].disk_used_gb / items[-1].disk_total_gb * 100, 1) if items[-1].disk_total_gb else 0,
            "load": round(sum(i.load1 for i in items) / n, 2),
            "rx_rate": round(sum(i.rx_rate_kbps for i in items) / n, 1),
            "tx_rate": round(sum(i.tx_rate_kbps for i in items) / n, 1),
        })

    first, last = rows[0], rows[-1]
    rx_mb = max(0, last.net_rx_mb - first.net_rx_mb)
    tx_mb = max(0, last.net_tx_mb - first.net_tx_mb)
    return {"points": points, "totals": {"rx_mb": round(rx_mb, 1), "tx_mb": round(tx_mb, 1)},
            "bucket_min": bucket_min}


@router.get("/host/traffic")
def host_traffic(db: Session = Depends(get_db)):
    """实时流量：/proc/net/dev 各物理网卡累计收发 + 24h 合计。"""
    ifaces = []
    try:
        with open("/proc/net/dev") as f:
            for line in f.read().splitlines()[2:]:
                name, _, data = line.partition(":")
                name = name.strip()
                if not name or any(name.startswith(p) or name == p for p in _VIRTUAL_IF):
                    continue
                cols = data.split()
                if len(cols) >= 9:
                    try:
                        ifaces.append({
                            "name": name,
                            "rx_mb": round(int(cols[0]) / 1024 / 1024, 1),
                            "tx_mb": round(int(cols[8]) / 1024 / 1024, 1),
                        })
                    except ValueError:
                        pass
    except OSError:
        pass

    totals = {"rx_mb": 0, "tx_mb": 0}
    since = datetime.utcnow() - timedelta(hours=24)
    rows = (db.query(HostMetric).filter(HostMetric.ts >= since)
            .order_by(HostMetric.ts.asc()).all())
    if rows:
        totals = {
            "rx_mb": round(max(0, rows[-1].net_rx_mb - rows[0].net_rx_mb), 1),
            "tx_mb": round(max(0, rows[-1].net_tx_mb - rows[0].net_tx_mb), 1),
        }
    return {"interfaces": ifaces, "totals_24h": totals}


# ---------- 阈值告警 ----------

class AlertConfigIn(BaseModel):
    enabled: bool = False
    cpu_threshold: float = 90.0
    mem_threshold: float = 90.0
    disk_threshold: float = 90.0
    webhook_type: str = ""      # feishu / dingtalk / telegram / custom / ""
    webhook_url: str = ""
    webhook_extra: str = ""     # telegram chat_id / 飞书钉钉签名密钥
    quiet_minutes: int = 10


@router.get("/host/alerts")
def alerts_get(db: Session = Depends(get_db)):
    cfg = hms.get_or_init_config(db)
    logs = (db.query(HostAlertLog).order_by(HostAlertLog.id.desc())
            .limit(50).all())
    return {
        "config": {
            "enabled": bool(cfg.enabled), "cpu_threshold": cfg.cpu_threshold,
            "mem_threshold": cfg.mem_threshold, "disk_threshold": cfg.disk_threshold,
            "webhook_type": cfg.webhook_type, "webhook_url": cfg.webhook_url,
            "webhook_extra": cfg.webhook_extra, "quiet_minutes": cfg.quiet_minutes,
        },
        "logs": [{"ts": l.ts.isoformat(), "metric": l.metric, "value": l.value,
                  "threshold": l.threshold, "message": l.message, "sent": bool(l.sent)}
                 for l in logs],
    }


@router.put("/host/alerts")
def alerts_put(body: AlertConfigIn, db: Session = Depends(get_db)):
    for name, v in (("CPU", body.cpu_threshold), ("内存", body.mem_threshold),
                    ("磁盘", body.disk_threshold)):
        if not 1 <= v <= 100:
            raise HTTPException(status_code=400, detail=f"{name}阈值需在 1-100 之间")
    if body.webhook_type and body.webhook_type not in ("feishu", "dingtalk", "telegram", "custom"):
        raise HTTPException(status_code=400, detail="不支持的 Webhook 类型")
    cfg = hms.get_or_init_config(db)
    cfg.enabled = 1 if body.enabled else 0
    cfg.cpu_threshold = body.cpu_threshold
    cfg.mem_threshold = body.mem_threshold
    cfg.disk_threshold = body.disk_threshold
    cfg.webhook_type = body.webhook_type
    cfg.webhook_url = body.webhook_url.strip()
    cfg.webhook_extra = body.webhook_extra.strip()
    cfg.quiet_minutes = max(1, min(body.quiet_minutes, 1440))
    db.commit()
    return {"detail": "告警配置已保存"}


@router.post("/host/alerts/test")
def alerts_test(db: Session = Depends(get_db)):
    """向已配置的 Webhook 发送测试消息。"""
    cfg = hms.get_or_init_config(db)
    if not cfg.webhook_url:
        raise HTTPException(status_code=400, detail="请先填写 Webhook 地址")
    ok = hms.push_webhook(cfg, "[PPanel 节点告警] 这是一条测试消息，收到即说明推送链路正常")
    if not ok:
        raise HTTPException(status_code=502, detail="推送失败：请检查 Webhook 地址/密钥/网络")
    return {"detail": "测试消息已发送"}


@router.delete("/host/alerts/logs")
def alerts_clear_logs(db: Session = Depends(get_db)):
    db.query(HostAlertLog).delete()
    db.commit()
    return {"detail": "告警记录已清空"}


# ---------- 端口监听（ss -tulnp 实时状态） ----------

@router.get("/host/ports")
def host_ports():
    rc, out = _run(["ss", "-tulnpH"], timeout=15)
    rows = []
    if rc == 0 and out:
        proc_re = _re.compile(r'\("([^"]+)",pid=(\d+)')
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 6:
                continue
            proto, local = parts[0], parts[4]
            name, pid = "", 0
            m = proc_re.search(line)
            if m:
                name, pid = m.group(1), int(m.group(2))
            host, _, port = local.rpartition(":")
            rows.append({"proto": proto, "port": port, "addr": host or "*",
                         "process": name or "-", "pid": pid})
    else:
        # 无 ss 时兜底解析 /proc/net/{tcp,udp}（无进程信息）
        def parse_proc(path, proto):
            try:
                with open(path) as f:
                    for i, line in enumerate(f.read().splitlines()[1:]):
                        cols = line.split()
                        local = cols[1]
                        port = int(local.rsplit(":", 1)[1], 16)
                        addr_hex = local.rsplit(":", 1)[0]
                        ip = ".".join(str(int(addr_hex[j:j + 2], 16))
                                      for j in range(6, -2, -2))
                        rows.append({"proto": proto, "port": str(port), "addr": ip,
                                     "process": "-", "pid": 0})
            except OSError:
                pass
        parse_proc("/proc/net/tcp", "tcp")
        parse_proc("/proc/net/tcp6", "tcp")
        parse_proc("/proc/net/udp", "udp")

    # 同端口 v4/v6 重复绑定去重（保留带进程名的一条）
    seen: dict = {}
    for r in rows:
        key = (r["proto"], r["port"])
        if key not in seen or (seen[key]["process"] == "-" and r["process"] != "-"):
            seen[key] = r
    out_rows = sorted(seen.values(), key=lambda r: (r["proto"], int(r["port"] or 0)))
    return {"rows": out_rows, "tool": "ss" if rc == 0 else "proc"}


# ---------- 进程管理 ----------

@router.get("/host/processes")
def host_processes(sort: str = "cpu"):
    """进程排行（按 CPU 或内存，取前 200）。"""
    key = {"cpu": "-pcpu", "mem": "-pmem", "rss": "-rss"}.get(sort, "-pcpu")
    rc, out = _run(["ps", "-eo", "pid,user,pcpu,pmem,rss,state,etimes,args",
                    "--no-headers", f"--sort={key}"], timeout=15)
    rows = []
    if rc == 0:
        for line in out.splitlines():
            parts = line.split(None, 7)
            if len(parts) < 8:
                continue
            try:
                rows.append({
                    "pid": int(parts[0]), "user": parts[1],
                    "cpu": float(parts[2]), "mem": float(parts[3]),
                    "rss_mb": round(int(parts[4]) / 1024, 1),
                    "state": parts[5],
                    "uptime_s": int(parts[6]) if parts[6].isdigit() else 0,
                    "cmd": parts[7][:200],
                })
            except ValueError:
                continue
        rows = rows[:200]
    return {"rows": rows, "error": out.strip()[:200] if rc != 0 else ""}


class KillIn(BaseModel):
    pid: int
    sig: str = "TERM"


@router.post("/host/processes/kill")
def host_process_kill(body: KillIn):
    if body.pid <= 1:
        raise HTTPException(status_code=400, detail="不允许终止 pid<=1 的进程")
    if body.pid == os.getpid():
        raise HTTPException(status_code=400, detail="不允许终止面板进程")
    sig = {"TERM": 15, "KILL": 9}.get(body.sig.upper())
    if not sig:
        raise HTTPException(status_code=400, detail="信号仅支持 TERM/KILL")
    try:
        os.kill(body.pid, sig)
    except PermissionError:
        raise HTTPException(status_code=403, detail="无权限终止该进程")
    except ProcessLookupError:
        raise HTTPException(status_code=404, detail="进程不存在（可能已退出）")
    return {"detail": f"已向进程 {body.pid} 发送 {body.sig.upper()} 信号"}


# ---------- systemd 服务管理 ----------

@router.get("/host/services")
def host_services():
    rc, out = _run(["systemctl", "list-units", "--type=service", "--all",
                    "--no-legend", "--no-pager"], timeout=20)
    rows = []
    if rc == 0:
        for line in out.splitlines():
            if not line.strip() or "not-found" in line:
                continue
            parts = line.split(None, 4)
            if len(parts) >= 4:
                rows.append({
                    "unit": parts[0], "load": parts[1], "active": parts[2],
                    "sub": parts[3],
                    "desc": parts[4] if len(parts) > 4 else "",
                })
    return {"rows": rows, "error": out.strip()[:200] if rc != 0 else ""}


_UNIT_RE = _re.compile(r"^[A-Za-z0-9@._\\-]+\.service$")


def _check_unit(unit: str):
    if not _UNIT_RE.match(unit):
        raise HTTPException(status_code=400, detail="服务名格式无效")


@router.post("/host/services/{unit}/{action}")
def host_service_action(unit: str, action: str):
    _check_unit(unit)
    if action not in ("start", "stop", "restart", "enable", "disable"):
        raise HTTPException(status_code=400, detail="不支持的操作")
    rc, out = _run(["systemctl", action, unit], timeout=30)
    if rc != 0:
        raise HTTPException(status_code=502, detail=(out.strip() or "执行失败")[:300])
    return {"detail": f"{unit} {action} 执行成功"}


# ---------- 日志中心（journalctl） ----------

@router.get("/host/journal")
def host_journal(unit: str = "", lines: int = 200):
    lines = max(10, min(lines, 2000))
    if unit:
        _check_unit(unit)
        rc, out = _run(["journalctl", "-u", unit, "-n", str(lines),
                        "--no-pager", "-o", "short-iso"], timeout=20)
    else:
        rc, out = _run(["journalctl", "-n", str(lines), "--no-pager", "-o", "short-iso"],
                       timeout=20)
    if rc != 0:
        raise HTTPException(status_code=502, detail=(out.strip() or "journalctl 执行失败")[:300])
    return {"text": out[-200000:]}


# ---------- 登录审计（last / lastb） ----------

def _parse_last(out: str) -> list:
    """last -F 输出解析：user tty host 完整时间 剩余段。"""
    pat = _re.compile(
        r"^(\S+)\s+(\S+)\s+(\S+)\s+(\w{3}\s+\w{3}\s+\d{1,2}\s+[\d:]+\s+\d{4})\s+(.*)$")
    rows = []
    for line in out.splitlines():
        if not line.strip() or line.startswith("wtmp begins"):
            continue
        m = pat.match(line)
        if not m:
            continue
        tail = m.group(5)
        if "still logged in" in tail:
            status = "online"
        elif tail.startswith("-"):
            status = "logout"
        else:
            status = "abnormal"   # crash / down / gone
        rows.append({"user": m.group(1), "tty": m.group(2), "source": m.group(3),
                     "time": m.group(4), "status": status, "tail": tail.strip()})
    return rows


@router.get("/host/logins")
def host_logins():
    rc, out = _run(["last", "-F", "-n", "50"], timeout=10)
    logins = _parse_last(out) if rc == 0 else []
    rc2, out2 = _run(["lastb", "-F", "-n", "50"], timeout=10)
    fails = []
    if rc2 == 0 and os.path.exists("/var/log/btmp"):
        fails = _parse_last(out2)
    return {"logins": logins, "failed": fails,
            "available": rc == 0 and os.path.exists("/var/log/wtmp")}


# ---------- SSL 证书到期检测 ----------

def _collect_domains() -> list:
    """域名来源：Caddy 站点配置 + 实例绑定域名。"""
    domains = []
    try:
        for p in Path("/etc/caddy/sites").glob("*.caddy"):
            for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or not s.endswith("{"):
                    continue
                d = s[:-1].strip()
                if d and " " not in d and "." in d:
                    domains.append(d)
    except OSError:
        pass
    db = SessionLocal()
    try:
        for (d,) in (db.query(Instance.domain)
                     .filter(Instance.domain.isnot(None), Instance.domain != "",
                             Instance.ext_port > 0).all()):
            domains.append(d)
    except Exception:  # noqa: BLE001
        pass
    finally:
        db.close()
    return sorted(set(domains))


def _check_cert(domain: str) -> dict:
    """TLS 握手直接取证书到期时间（不依赖 openssl 二进制）。"""
    item = {"domain": domain, "not_after": "", "days_left": None, "ok": False, "error": ""}
    try:
        ctx = _ssl.create_default_context()
        with _socket.create_connection((domain, 443), timeout=8) as sock:
            with ctx.wrap_socket(sock, server_hostname=domain) as tls:
                cert = tls.getpeercert()
        after = cert.get("notAfter")
        if after:
            dt = datetime.strptime(after, "%b %d %H:%M:%S %Y %Z")
            item["not_after"] = dt.strftime("%Y-%m-%d %H:%M")
            item["days_left"] = (dt - datetime.utcnow()).days
            item["ok"] = True
    except Exception as e:  # noqa: BLE001 证书不可达如实上报
        item["error"] = e.__class__.__name__
    return item


@router.get("/host/certs")
def host_certs():
    domains = _collect_domains()
    if not domains:
        return {"rows": []}
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_check_cert, d): d for d in domains}
        for fut in as_completed(futures, timeout=20):
            try:
                rows.append(fut.result())
            except Exception:  # noqa: BLE001
                rows.append({"domain": futures[fut], "not_after": "",
                             "days_left": None, "ok": False, "error": "timeout"})
    rows.sort(key=lambda r: (r["days_left"] is None, r["days_left"] if r["days_left"] is not None else 0))
    return {"rows": rows}
