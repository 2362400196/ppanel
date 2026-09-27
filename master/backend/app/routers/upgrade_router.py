"""主控在线升级（管理员 Web 面板一键触发）+ 节点升级透传：
- GET  /admin/system/version          主控当前/远程版本（git 部署才可升级；Docker 部署提示命令行）
- GET  /admin/system/upgrade/status  升级进度（前端轮询）
- POST /admin/system/upgrade          后台执行：拉代码 → uv 依赖 → npm 构建前端 → 重启服务
- GET  /admin/nodes/{nid}/version     透传被控版本检查
- POST /admin/nodes/{nid}/upgrade     透传被控一键升级
- GET  /admin/nodes/{nid}/upgrade/status

升级流水线与 install.sh 同源：pull 前预清理本地改动/残留 merge 状态，
失败硬对齐远程；.env/数据未跟踪文件不受影响。重启自身用 Popen（systemd restart），
先返回响应再由 systemd 拉起新进程。
"""
import os
import shutil
import subprocess
import threading
import time

from fastapi import APIRouter, Depends, HTTPException

from app.agent_client import agent_json
from app.auth import require_admin
from app.database import get_db
from app.models import Node
from sqlalchemy.orm import Session

router = APIRouter(tags=["upgrade"], dependencies=[Depends(require_admin)])

STATE = {"running": False, "stage": "", "log": [], "ok": None,
         "error": "", "started_at": 0.0, "finished_at": 0.0}
LOCK = threading.Lock()


def _repo_root() -> str:
    """向上查找 .git（backend -> master -> 仓库根）；找不到为非 git 部署。"""
    d = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))  # routers -> app -> backend -> 仓库根
    while d and d != os.path.dirname(d):
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return ""


def _run(cmd, cwd=None, timeout=600) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, shell=False)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out.strip()[-800:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]


def _git(root: str, *args, timeout=300):
    return _run(["git", "-C", root, *args], timeout=timeout)


def _remote_name(root: str) -> str:
    """取仓库第一个远程名（不写死 origin，开发机远程可能叫别的名字）。"""
    ok, out = _git(root, "remote", timeout=10)
    if ok and out:
        return out.splitlines()[0].strip()
    return "origin"


def _log(msg: str) -> None:
    with LOCK:
        STATE["log"].append(msg)
        STATE["log"] = STATE["log"][-300:]


# ---------- 主控自身 ----------

@router.get("/admin/system/version")
def master_version():
    root = _repo_root()
    if not root:
        return {"mode": "unknown", "upgradable": False,
                "reason": "非 git 部署（Docker/手动），请用安装脚本部署以支持在线升级"}
    ok1, cur = _git(root, "rev-parse", "--short", "HEAD", timeout=10)
    ok2, br = _git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    ok3, ls = _git(root, "ls-remote", _remote_name(root), "HEAD", timeout=20)
    remote = ls.split()[0][:7] if ok3 and ls else None
    return {"mode": "git", "branch": br if ok2 else "",
            "current": cur if ok1 else None, "remote": remote,
            "upgradable": bool(ok1 and remote and cur and remote != cur)}


@router.get("/admin/system/upgrade/status")
def master_status():
    with LOCK:
        return dict(STATE)


@router.post("/admin/system/upgrade")
def master_upgrade():
    with LOCK:
        if STATE["running"]:
            return {"detail": "升级正在进行中", "running": True}
        STATE.update({"running": True, "stage": "拉取代码", "log": [], "ok": None,
                      "error": "", "started_at": time.time(), "finished_at": 0.0})
    threading.Thread(target=_pipeline, daemon=True, name="ppanel-upgrade").start()
    return {"detail": "升级已开始，完成后主控将自动重启（约 1-3 分钟）", "running": True}


def _uv_sync(backend: str) -> bool:
    """同步后端依赖；返回是否成功（无工具可跳过时视为成功，自检兜底）。"""
    uv = ""
    for c in (os.path.join(backend, ".venv", "bin", "uv"),
              os.path.join(backend, ".venv", "Scripts", "uv.exe"),
              shutil.which("uv"), os.path.expanduser("~/.local/bin/uv")):
        if c and os.path.exists(c):
            uv = c
            break
    if uv:
        ok, out = _run([uv, "sync", "--inexact", "--no-dev", "--no-install-project"],
                       cwd=backend, timeout=600)
        _log("后端依赖已同步（uv）" if ok else f"uv sync 输出：{out[-200:]}")
        return ok
    pip = os.path.join(backend, ".venv", "bin", "pip")
    if os.name == "nt":
        pip = os.path.join(backend, ".venv", "Scripts", "pip.exe")
    if os.path.exists(pip):
        ok, out = _run([pip, "install", "-q", "."], cwd=backend, timeout=600)
        _log("后端依赖已安装（pip）" if ok else f"pip 输出：{out[-200:]}")
        return ok
    _log("未找到 uv/pip，跳过后端依赖")
    return True


def _self_check(backend: str) -> tuple[bool, str]:
    """启动自检：用 venv 解释器导入入口模块，提前暴露缺依赖/坏代码。"""
    py = os.path.join(backend, ".venv", "Scripts", "python.exe" if os.name == "nt"
                      else "bin", "python.exe" if os.name == "nt" else "python")
    if not os.path.exists(py):
        return True, ""
    return _run([py, "-c", "import main"], cwd=backend, timeout=120)


def _rollback(root: str, old_hash: str, backend: str) -> None:
    """升级失败回滚：代码退回升级前版本，并按旧清单恢复依赖。"""
    _log(f"回滚代码到 {old_hash}...")
    _git(root, "reset", "--hard", old_hash, timeout=60)
    _uv_sync(backend)
    _log("已回滚：服务继续运行旧版本，可排查后重试升级")


def _npm_build(frontend: str) -> None:
    if not os.path.isdir(os.path.join(frontend, "src")):
        _log("未找到前端目录，跳过构建")
        return
    npm = shutil.which("npm")
    if not npm:
        _log("未安装 npm，跳过前端构建（界面未更新，可安装 Node 后重试升级）")
        return
    ok, out = _run([npm, "install", "--registry=https://registry.npmmirror.com"],
                   cwd=frontend, timeout=900)
    _log("前端依赖安装完成" if ok else f"npm install 输出：{out[-200:]}")
    ok, out = _run([npm, "run", "build"], cwd=frontend, timeout=900)
    if ok:
        _log("前端构建完成（dist/）")
    else:
        _log(f"前端构建失败（服务仍会重启，界面保持旧版）：{out[-200:]}")


def _pipeline() -> None:
    root = _repo_root()
    backend = ""
    old_hash = ""
    try:
        if not root:
            raise RuntimeError("非 git 部署，无法在线升级")
        backend = os.path.join(root, "master", "backend")
        with LOCK:
            STATE["stage"] = "拉取代码"
        _ok, old_hash = _git(root, "rev-parse", "--short", "HEAD", timeout=10)
        _log("恢复本地改动、清理残留状态...")
        _git(root, "merge", "--abort")
        _git(root, "checkout", "--", ".")
        ok, out = _git(root, "pull", "--ff-only", timeout=120)
        if not ok:
            _log(f"pull 失败，硬对齐远程：{out[-200:]}")
            _bok, br = _git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
            _rn = _remote_name(root)
            _git(root, "fetch", _rn, timeout=120)
            ok, out = _git(root, "reset", "--hard",
                           f"{_rn}/{br if _bok else 'master'}", timeout=60)
        if not ok:
            raise RuntimeError(f"代码拉取失败：{out}")
        ok, cur = _git(root, "rev-parse", "--short", "HEAD", timeout=10)
        _log(f"代码已更新到 {cur if ok else '?'}")

        with LOCK:
            STATE["stage"] = "安装依赖"
        if not _uv_sync(backend) and _ok and old_hash:
            _rollback(root, old_hash, backend)
            raise RuntimeError("后端依赖安装失败，已回滚到升级前版本")

        with LOCK:
            STATE["stage"] = "启动自检"
        _log("自检：验证新后端代码可正常加载...")
        chk_ok, out = _self_check(backend)
        if not chk_ok:
            _log(f"自检失败：{out[-200:]}")
            if _ok and old_hash:
                _rollback(root, old_hash, backend)
            raise RuntimeError(f"启动自检失败，已回滚到升级前版本：{out[-150:]}")
        _log("自检通过")

        with LOCK:
            STATE["stage"] = "构建前端"
        _npm_build(os.path.join(root, "master", "frontend"))

        with LOCK:
            STATE["stage"] = "重启服务"
        svc = os.environ.get("PPANEL_MASTER_SERVICE", "ppanel-master")
        if shutil.which("systemctl"):
            subprocess.Popen(["systemctl", "restart", svc])
            _log(f"已触发 systemctl restart {svc}，新进程几秒内就绪")
        else:
            _log("无 systemd 环境，请手动重启主控进程生效")
        with LOCK:
            STATE.update({"ok": True, "stage": "完成", "finished_at": time.time()})
    except Exception as e:  # noqa: BLE001
        with LOCK:
            STATE.update({"ok": False, "error": str(e)[:300], "stage": "失败",
                          "finished_at": time.time()})
        _log(f"升级失败：{e}")
    finally:
        with LOCK:
            STATE["running"] = False


# ---------- 节点升级透传 ----------

def _node(nid: int, db: Session) -> Node:
    node = db.get(Node, nid)
    if not node:
        raise HTTPException(status_code=404, detail="节点不存在")
    return node


@router.get("/admin/nodes/{nid}/version")
def node_version(nid: int, db: Session = Depends(get_db)):
    return agent_json(_node(nid, db), "GET", "/agent/version")


@router.post("/admin/nodes/{nid}/upgrade")
def node_upgrade(nid: int, db: Session = Depends(get_db)):
    return agent_json(_node(nid, db), "POST", "/agent/upgrade")


@router.get("/admin/nodes/{nid}/upgrade/status")
def node_upgrade_status(nid: int, db: Session = Depends(get_db)):
    return agent_json(_node(nid, db), "GET", "/agent/upgrade/status")
