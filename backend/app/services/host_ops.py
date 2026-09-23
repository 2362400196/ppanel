"""宿主机操作：daemon.json 读写、dockerd 重启、宿主机命令执行。

两种运行形态自动适配：
- local：面板与 dockerd 同机（Linux 生产环境，直接操作 /etc/docker/daemon.json）
- wsl：面板在 Windows、dockerd 在 WSL（本机开发态，通过 wsl -u root 间接操作）
"""
import base64
import json
import platform
import shlex
import subprocess

from fastapi import HTTPException

DAEMON_JSON = "/etc/docker/daemon.json"


def host_kind() -> str:
    if platform.system() == "Windows":
        return "wsl"
    return "local"


def _wsl_root(cmd: str, timeout: int = 30) -> str:
    """在 WSL 内以 root 执行命令（仅开发态使用）。"""
    try:
        p = subprocess.run(
            ["wsl", "-u", "root", "-e", "bash", "-c", cmd],
            capture_output=True, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (p.stdout or b"").decode("utf-8", "replace")


# ---------- daemon.json ----------

def read_daemon_json() -> dict:
    if host_kind() == "wsl":
        raw = _wsl_root(f"cat {DAEMON_JSON} 2>/dev/null")
        if not raw.strip():
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    try:
        with open(DAEMON_JSON, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}


def write_daemon_json(data: dict) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if host_kind() == "wsl":
        b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
        out = _wsl_root(
            f"cp {DAEMON_JSON} {DAEMON_JSON}.bak 2>/dev/null; "
            f"mkdir -p /etc/docker && echo {b64} | base64 -d > {DAEMON_JSON} && cat {DAEMON_JSON}",
        )
        if "{" not in out:
            raise HTTPException(status_code=500, detail="写入 WSL /etc/docker/daemon.json 失败")
        return
    import os
    os.makedirs("/etc/docker", exist_ok=True)
    try:
        import shutil
        shutil.copy2(DAEMON_JSON, DAEMON_JSON + ".bak")
    except FileNotFoundError:
        pass
    with open(DAEMON_JSON, "w", encoding="utf-8") as f:
        f.write(text)


# ---------- dockerd 重启 ----------

def restart_dockerd() -> None:
    """重启 dockerd 使 daemon.json 生效。面板与 Docker 的连接会短暂中断。"""
    if host_kind() == "wsl":
        _wsl_root("systemctl restart docker", timeout=90)
        return
    try:
        subprocess.run(["systemctl", "restart", "docker"], check=True, timeout=90)
    except (OSError, subprocess.SubprocessError):
        subprocess.run(["service", "docker", "restart"], timeout=90)


# ---------- 宿主机命令执行（终端用） ----------

TERM_TIMEOUT = 120


def _parse_pwd(out: str) -> tuple[str, str]:
    """拆掉脚本附加的 @@PWD@@ 标记行，返回 (正文, 新cwd)。"""
    pwd = ""
    lines = out.splitlines()
    while lines and lines[-1].strip() == "":
        lines.pop()
    if lines and lines[-1].startswith("@@PWD@@"):
        pwd = lines.pop()[len("@@PWD@@"):].strip()
    return "\n".join(lines), pwd


def run_host_command(cmd: str, cwd: str) -> tuple[str, str]:
    """在宿主机（或 WSL）执行一条 shell 命令，返回 (输出, 新cwd)。

    脚本结构：cd 到会话目录 → 执行用户命令 → 打印 cwd 标记 → 还原退出码。
    """
    from app.config import settings
    if len(cmd) > 4000:
        raise HTTPException(status_code=400, detail="命令过长")
    quiet_cwd = shlex.quote(cwd) if cwd else "~"
    script = (
        f"cd {quiet_cwd} 2>/dev/null || cd ~\n"
        f"{cmd}\n"
        f"__ppanel_rc=$?\n"
        f'printf "\\n@@PWD@@%s\\n" "$(pwd)"\n'
        f"exit $__ppanel_rc"
    )
    try:
        if settings.inner_shell_mode == "wsl" or (
            settings.inner_shell_mode == "auto" and platform.system() == "Windows"
        ):
            p = subprocess.run(["wsl", "-e", "bash", "-c", script],
                               capture_output=True, timeout=TERM_TIMEOUT)
        else:
            p = subprocess.run(["bash", "-c", script],
                               capture_output=True, timeout=TERM_TIMEOUT)
    except subprocess.TimeoutExpired:
        out = "[终端] 命令执行超时（120s），已终止"
        return out, cwd
    except OSError as e:
        raise HTTPException(status_code=503, detail=f"无法在宿主机创建 shell：{e}")

    text = (p.stdout or b"") + (p.stderr or b"")
    out, new_cwd = _parse_pwd(text.decode("utf-8", "replace"))
    return out, new_cwd or cwd
