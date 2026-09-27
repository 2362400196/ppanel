"""被控在线升级（主控面板一键触发，X-Node-Token 鉴权）：
- GET  /agent/version        当前版本 vs 远程版本（git 部署才可升级）
- GET  /agent/upgrade/status 升级进度（主控轮询展示）
- POST /agent/upgrade        后台执行：拉代码 → 装依赖 → 自动重启服务

升级流水线与 install.sh 同源：pull 前预清理本地改动/残留 merge 状态，
失败硬对齐远程；.env/数据目录为未跟踪文件不受影响。
重启通过 systemctl restart（Popen，不阻塞当前进程退出）。
"""
import os
import shutil
import subprocess
import threading
import time

from fastapi import APIRouter

router = APIRouter(tags=["upgrade"])

STATE = {"running": False, "stage": "", "log": [], "ok": None,
         "error": "", "started_at": 0.0, "finished_at": 0.0}
LOCK = threading.Lock()


def _repo_root() -> str:
    """向上查找 .git 得到部署仓库根（/opt/ppanel）；找不到说明非 git 部署。"""
    d = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))  # routers -> app -> backend -> 仓库根
    while d and d != os.path.dirname(d):
        if os.path.isdir(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return ""


def _run(cmd, cwd=None, timeout=300) -> tuple[bool, str]:
    try:
        r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                           timeout=timeout, shell=False)
        out = (r.stdout or "") + (r.stderr or "")
        return r.returncode == 0, out.strip()[-500:]
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]


def _git(root: str, *args, timeout=300):
    return _run(["git", "-C", root, *args], timeout=timeout)


def _log(msg: str) -> None:
    with LOCK:
        STATE["log"].append(msg)
        STATE["log"] = STATE["log"][-200:]


@router.get("/version")
def version():
    root = _repo_root()
    if not root:
        return {"mode": "unknown", "upgradable": False,
                "reason": "非 git 部署（手动拷贝/容器），请用安装脚本部署以支持在线升级"}
    ok1, cur = _git(root, "rev-parse", "--short", "HEAD", timeout=10)
    ok2, br = _git(root, "rev-parse", "--abbrev-ref", "HEAD", timeout=10)
    ok3, ls = _git(root, "ls-remote", "origin", "HEAD", timeout=20)
    remote = ls.split()[0][:7] if ok3 and ls else None
    return {"mode": "git", "branch": br if ok2 else "",
            "current": cur if ok1 else None, "remote": remote,
            "upgradable": bool(ok1 and remote and cur and remote != cur)}


@router.get("/upgrade/status")
def status():
    with LOCK:
        return dict(STATE)


@router.post("/upgrade")
def upgrade():
    with LOCK:
        if STATE["running"]:
            return {"detail": "升级正在进行中", "running": True}
        STATE.update({"running": True, "stage": "拉取代码", "log": [], "ok": None,
                      "error": "", "started_at": time.time(), "finished_at": 0.0})
    threading.Thread(target=_pipeline, daemon=True, name="ppanel-upgrade").start()
    return {"detail": "升级已开始，节点将在完成后自动重启（约半分钟）", "running": True}


def _uv_bin(root: str) -> str:
    for c in (shutil.which("uv"), os.path.expanduser("~/.local/bin/uv"),
              os.path.join(root, "agent", "backend", ".venv", "bin", "uv")):
        if c and os.path.exists(c):
            return c
    return ""


def _self_check(backend: str) -> tuple[bool, str]:
    """启动自检：用 venv 解释器导入入口模块，提前暴露缺依赖/坏代码，避免带病重启。"""
    py = os.path.join(backend, ".venv", "bin", "python")
    if not os.path.exists(py):
        return True, ""
    return _run([py, "-c", "import agent_main"], cwd=backend, timeout=120)


def _rollback(root: str, old_hash: str, backend: str) -> None:
    """升级失败回滚：代码退回升级前版本，并按旧清单恢复依赖。"""
    _log(f"回滚代码到 {old_hash}...")
    _git(root, "reset", "--hard", old_hash, timeout=60)
    uv = _uv_bin(root)
    if uv:
        _run([uv, "sync", "--inexact", "--no-dev", "--no-install-project"],
             cwd=backend, timeout=600)
    _log("已回滚：服务继续运行旧版本，可排查后重试升级")


def _pipeline() -> None:
    root = _repo_root()
    backend = ""
    old_hash = ""
    try:
        if not root:
            raise RuntimeError("非 git 部署，无法在线升级")
        backend = os.path.join(root, "agent", "backend")
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
            _git(root, "fetch", "origin", timeout=120)
            ok, out = _git(root, "reset", "--hard",
                           f"origin/{br if _bok else 'main'}", timeout=60)
        if not ok:
            raise RuntimeError(f"代码拉取失败：{out}")
        ok, cur = _git(root, "rev-parse", "--short", "HEAD", timeout=10)
        _log(f"代码已更新到 {cur if ok else '?'}")

        with LOCK:
            STATE["stage"] = "安装依赖"
        dep_ok = True
        uv = _uv_bin(root)
        if uv:
            dep_ok, out = _run([uv, "sync", "--inexact", "--no-dev", "--no-install-project"],
                               cwd=backend, timeout=600)
            _log("依赖已同步（uv）" if dep_ok else f"uv sync 输出：{out[-200:]}")
        else:
            pip = os.path.join(backend, ".venv", "bin", "pip")
            if os.path.exists(pip):
                dep_ok, out = _run([pip, "install", "-q", "."], cwd=backend, timeout=600)
                _log("依赖已安装（pip）" if dep_ok else f"pip 输出：{out[-200:]}")
            else:
                _log("未找到 uv/pip，跳过依赖安装")
        if not dep_ok and _ok and old_hash:
            _rollback(root, old_hash, backend)
            raise RuntimeError("依赖安装失败，已回滚到升级前版本")

        with LOCK:
            STATE["stage"] = "启动自检"
        _log("自检：验证新代码可正常加载...")
        chk_ok, out = _self_check(backend)
        if not chk_ok:
            _log(f"自检失败：{out[-200:]}")
            if _ok and old_hash:
                _rollback(root, old_hash, backend)
            raise RuntimeError(f"启动自检失败，已回滚到升级前版本：{out[-150:]}")
        _log("自检通过")

        with LOCK:
            STATE["stage"] = "重启服务"
        svc = os.environ.get("PPANEL_AGENT_SERVICE", "ppanel-agent")
        if shutil.which("systemctl"):
            subprocess.Popen(["systemctl", "restart", svc])
            _log(f"已触发 systemctl restart {svc}")
        else:
            _log("无 systemd 环境，请手动重启 agent 进程生效")
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
