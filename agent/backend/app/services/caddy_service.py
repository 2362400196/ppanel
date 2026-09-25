"""Caddy 反向代理与自动 HTTPS 管理（实例级「域名与 SSL」）。

一个实例绑定一个自定义域名：写 /etc/caddy/sites/<name>.caddy 反代到
127.0.0.1:<ext_port>，Caddy 自动申请/续期 Let's Encrypt 证书。
仅 Linux 生效；未安装 Caddy 时状态接口会如实返回，前端引导安装。
"""
import os
import re
import shutil
import socket
import subprocess
import time

DOMAIN_RE = re.compile(r"^(?=.{4,253}$)[a-z0-9]([a-z0-9-]*[a-z0-9])?"
                       r"(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

SITES_DIR = "/etc/caddy/sites"


def caddy_installed() -> bool:
    return bool(shutil.which("caddy"))


def caddy_info() -> dict:
    """Caddy 安装/运行状态与版本。"""
    if not caddy_installed():
        return {"installed": False, "active": False, "version": ""}
    ver = ""
    try:
        r = subprocess.run(["caddy", "version"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            ver = r.stdout.strip().split()[0]
    except Exception:  # noqa: BLE001
        pass
    active = False
    try:
        r = subprocess.run(["systemctl", "is-active", "caddy"],
                           capture_output=True, text=True, timeout=5)
        active = r.stdout.strip() == "active"
    except Exception:  # noqa: BLE001
        pass
    return {"installed": True, "active": active, "version": ver}


def public_ip(timeout: float = 3.0) -> str | None:
    """本机公网出口 IP（缓存 10 分钟）；探测失败返回 None。"""
    global _PUB_IP, _PUB_IP_AT
    if _PUB_IP and time.time() - _PUB_IP_AT < 600:
        return _PUB_IP
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            r = subprocess.run(["curl", "-fs", "--max-time", str(int(timeout)), url],
                               capture_output=True, text=True, timeout=timeout + 3)
            ip = r.stdout.strip()
            if r.returncode == 0 and re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip):
                _PUB_IP, _PUB_IP_AT = ip, time.time()
                return ip
        except Exception:  # noqa: BLE001
            continue
    return None


_PUB_IP: str | None = None
_PUB_IP_AT: float = 0.0


def check_dns(domain: str) -> tuple[bool, str]:
    """校验域名 A 记录是否指向本机公网 IP；无法取公网 IP 时仅做解析检查。"""
    try:
        resolved = socket.gethostbyname(domain)
    except Exception:  # noqa: BLE001
        return False, "域名无法解析，请先到 DNS 服务商添加 A 记录"
    ip = public_ip()
    if not ip:
        return True, f"解析到 {resolved}（未能探测本机公网 IP，跳过比对）"
    if resolved != ip:
        return False, f"解析到 {resolved}，与本机公网 IP {ip} 不一致，证书将无法签发"
    return True, f"解析正确（{resolved}）"


def site_file(instance_id: int) -> str:
    return os.path.join(SITES_DIR, f"ppanel-{instance_id}.caddy")


def write_site(instance_id: int, domain: str, ext_port: int) -> None:
    """写入实例反代配置并 reload Caddy。"""
    if not DOMAIN_RE.match(domain):
        raise ValueError("域名格式不正确（示例：panel.example.com）")
    ensure_dirs()
    os.makedirs(SITES_DIR, exist_ok=True)
    conf = (f"# ppanel 实例 {instance_id} 自动生成\n"
            f"{domain} {{\n"
            f"    reverse_proxy 127.0.0.1:{ext_port}\n"
            f"}}\n")
    tmp = site_file(instance_id) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(conf)
    os.replace(tmp, site_file(instance_id))
    _reload()


def remove_site(instance_id: int) -> None:
    """删除实例反代配置并 reload Caddy（文件不存在则静默）。"""
    path = site_file(instance_id)
    if os.path.exists(path):
        os.remove(path)
        _reload()


def _reload() -> None:
    if not caddy_installed():
        raise RuntimeError("Caddy 未安装")
    r = subprocess.run(["systemctl", "reload", "caddy"],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0:
        r = subprocess.run(["systemctl", "restart", "caddy"],
                           capture_output=True, text=True, timeout=15)
    if r.returncode != 0:
        raise RuntimeError("Caddy 重载失败：" + (r.stderr or "").strip()[:200])


def ensure_dirs() -> None:
    """首次使用时准备 sites 目录并让主配置 include 它。"""
    os.makedirs(SITES_DIR, exist_ok=True)
    main_conf = "/etc/caddy/Caddyfile"
    try:
        if os.path.exists(main_conf):
            body = open(main_conf, encoding="utf-8").read()
            if "import /etc/caddy/sites/*" not in body:
                with open(main_conf, "a", encoding="utf-8") as f:
                    f.write("\nimport /etc/caddy/sites/*\n")
    except Exception:  # noqa: BLE001
        pass
