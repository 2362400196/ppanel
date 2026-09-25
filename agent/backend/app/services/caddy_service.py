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


def _port_in_use(port: int) -> bool:
    """本机回环探测端口是否被占用（0.5s 超时）。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:  # noqa: BLE001
        return False


def caddy_info() -> dict:
    """Caddy 安装/运行状态与版本；未运行时给出可读原因。"""
    if not caddy_installed():
        return {"installed": False, "active": False, "version": "", "reason": ""}
    ver = ""
    try:
        r = subprocess.run(["caddy", "version"], capture_output=True,
                           text=True, timeout=5)
        if r.returncode == 0:
            ver = r.stdout.strip().split()[0]
    except Exception:  # noqa: BLE001
        pass
    active = False
    state = ""
    try:
        r = subprocess.run(["systemctl", "is-active", "caddy"],
                           capture_output=True, text=True, timeout=5)
        active = r.stdout.strip() == "active"
        state = r.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    reason = ""
    if not active:
        if _port_in_use(80) or _port_in_use(443):
            reason = ("80/443 端口被宿主机其他 Web 服务占用（如 nginx/宝塔），Caddy 无法绑定。"
                      "需停用占用方，或由该服务自行配置反代与证书")
        elif state == "failed":
            reason = "服务启动失败（可执行 systemctl restart caddy 重试，journalctl -u caddy 查看详情）"
        else:
            reason = "服务未运行（可执行 systemctl restart caddy 启动）"
    return {"installed": True, "active": active, "version": ver, "reason": reason}


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


def hash_password(plaintext: str) -> str:
    """用本机 caddy 生成 bcrypt 哈希（basic_auth 要求）。"""
    r = subprocess.run(["caddy", "hash-password", "--plaintext", plaintext],
                       capture_output=True, text=True, timeout=10)
    if r.returncode != 0 or not r.stdout.strip():
        raise RuntimeError("生成密码哈希失败：" + (r.stderr or "").strip()[:120])
    return r.stdout.strip().splitlines()[-1].strip()


def _ip_matcher(name: str, cidrs: str, negate: bool = False) -> list[str]:
    toks = [t for t in cidrs.replace(",", " ").split() if t]
    if not toks:
        return []
    kw = "not remote_ip" if negate else "remote_ip"
    return [f"    @{name} {kw} {' '.join(toks)}"]


def write_site(instance_id: int, domain: str, ext_port: int,
               cfg: dict | None = None) -> None:
    """写入实例站点配置并 reload。

    cfg（可空）：mode=proxy/static、static_dir（容器 /app 下相对目录）、
    app_root（容器 /app 的宿主机挂载源，static 模式必需）、
    auth_user/auth_hash（basic_auth）、
    ip_whitelist / ip_blacklist（空格分隔 CIDR，白名单优先于黑名单）。
    """
    if not DOMAIN_RE.match(domain):
        raise ValueError("域名格式不正确（示例：panel.example.com）")
    cfg = cfg or {}
    ensure_dirs()
    os.makedirs(SITES_DIR, exist_ok=True)

    body: list[str] = [f"# ppanel 实例 {instance_id} 自动生成", f"{domain} {{"]
    wl = (cfg.get("ip_whitelist") or "").strip()
    bl = (cfg.get("ip_blacklist") or "").strip()
    if wl:  # 白名单：不在名单内一律拒绝（优先于黑名单）
        body += _ip_matcher(f"wl_{instance_id}", wl, negate=True) + [f"    abort @wl_{instance_id}"]
    elif bl:
        body += _ip_matcher(f"bl_{instance_id}", bl) + [f"    abort @bl_{instance_id}"]
    au, ah = (cfg.get("auth_user") or "").strip(), (cfg.get("auth_hash") or "").strip()
    if au and ah:
        body += [f"    basic_auth {{", f"        {au} {ah}", f"    }}"]
    if cfg.get("mode") == "static":
        sub = (cfg.get("static_dir") or "public_html").strip("/").replace("..", "")
        # app_root = 实例数据目录（容器 /app 的宿主机挂载源），由调用方传入；
        # 旧配置未传时按默认 DATA_ROOT 兜底
        base = (cfg.get("app_root") or f"/opt/ppanel/data/{instance_id}").rstrip("/")
        body += [f"    root * {base}/{sub}", "    file_server"]
    else:
        body += [f"    reverse_proxy 127.0.0.1:{ext_port}"]
    body += ["}"]
    conf = "\n".join(body) + "\n"

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
