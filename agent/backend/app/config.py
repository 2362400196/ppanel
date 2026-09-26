"""全局配置：全部通过环境变量 / backend/.env 覆盖，均有安全默认值。"""
import json
import os
from dataclasses import dataclass, field


def _load_dotenv() -> None:
    """轻量 .env 加载，避免引入额外依赖。"""
    env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    if not os.path.exists(env_file):
        return
    with open(env_file, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv()


def _env(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


def _env_list(key: str, default: list) -> list:
    raw = os.environ.get(key)
    if not raw:
        return list(default)
    try:
        val = json.loads(raw)
        if isinstance(val, list):
            return [str(x) for x in val]
    except json.JSONDecodeError:
        pass
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass
class Settings:
    # 面板监听（安全红线 6：默认只监听本机，需要对外时自行改 0.0.0.0 并配合鉴权/反代）
    host: str = field(default_factory=lambda: _env("PANEL_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("PANEL_PORT", 8000))

    # 认证
    jwt_secret: str = field(default_factory=lambda: _env(
        "JWT_SECRET", "ppanel-dev-secret-change-me-to-a-long-random-string"))
    jwt_expire_minutes: int = field(default_factory=lambda: _env_int("JWT_EXPIRE_MINUTES", 720))
    api_key: str = field(default_factory=lambda: _env("PANEL_API_KEY", ""))  # 商城系统预留 X-API-Key

    # 首次启动自动创建的管理员
    admin_username: str = field(default_factory=lambda: _env("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", "admin123"))

    # 存储
    data_root: str = field(default_factory=lambda: _env("DATA_ROOT", "/data/inst"))
    db_url: str = field(default_factory=lambda: _env("DB_URL", "sqlite:///./data/ppanel.db"))
    # 跨系统挂载根（如面板在 Windows、dockerd 在 WSL：DATA_ROOT=D:/ppanel/data/inst
    # 供文件 API 使用，DOCKER_VOLUME_ROOT=/mnt/d/ppanel/data/inst 供容器挂载使用；为空则同根）
    docker_volume_root: str = field(default_factory=lambda: _env("DOCKER_VOLUME_ROOT", ""))
    # 宿主机终端/daemon.json 的执行通道：auto（Win→WSL，Linux→本机）/ local / wsl
    inner_shell_mode: str = field(default_factory=lambda: _env("INNER_SHELL_MODE", "auto"))

    # 被控 Agent（agent_main.py 入口）：主控用 X-Node-Token 调用
    node_token: str = field(default_factory=lambda: _env("NODE_TOKEN", ""))
    agent_host: str = field(default_factory=lambda: _env("AGENT_HOST", "0.0.0.0"))
    agent_port: int = field(default_factory=lambda: _env_int("AGENT_PORT", 9100))

    # 开放开通面 /open/*：任何商城/开通系统凭 X-API-Key 对接（空=禁用整组接口）
    open_api_key: str = field(default_factory=lambda: _env("OPEN_API_KEY", ""))
    # 面板对外访问地址（如 http://1.2.3.4:9100/panel），开通响应里下发；
    # 空则按请求 Host 自动拼接
    panel_public_url: str = field(default_factory=lambda: _env("PANEL_PUBLIC_URL", ""))
    # 商城回调通知（webhook）：实例崩溃/临近到期时 POST JSON；空=关闭
    webhook_url: str = field(default_factory=lambda: _env("WEBHOOK_URL", ""))
    webhook_warn_days: int = field(default_factory=lambda: _env_int("WEBHOOK_WARN_DAYS", 3))
    # 实例到期后面板行为：readonly（只读，可看可备份）/ block（完全锁死）
    expired_panel_mode: str = field(default_factory=lambda: _env("EXPIRED_PANEL_MODE", "readonly"))

    # 实例参数
    port_start: int = field(default_factory=lambda: _env_int("PORT_START", 30000))
    port_end: int = field(default_factory=lambda: _env_int("PORT_END", 39999))
    inner_app_port: int = field(default_factory=lambda: _env_int("INNER_APP_PORT", 8000))
    upload_limit_mb: int = field(default_factory=lambda: _env_int("UPLOAD_LIMIT_MB", 50))
    pids_limit: int = field(default_factory=lambda: _env_int("PIDS_LIMIT", 256))
    docker_network: str = field(default_factory=lambda: _env("DOCKER_NETWORK", "ppanel-net"))
    python_images: list = field(default_factory=lambda: _env_list(
        "PYTHON_IMAGES",
        ["python:3.9-slim", "python:3.10-slim", "python:3.11-slim", "python:3.12-slim"],
    ))
    php_images: list = field(default_factory=lambda: _env_list(
        "PHP_IMAGES",
        ["php:8.2-cli", "php:8.3-cli", "php:8.4-cli"],
    ))
    node_images: list = field(default_factory=lambda: _env_list(
        "NODE_IMAGES",
        ["node:20-slim", "node:22-slim"],
    ))
    go_images: list = field(default_factory=lambda: _env_list(
        "GO_IMAGES",
        ["golang:1.23-alpine", "golang:1.24-alpine"],
    ))
    # PHP 安全：默认禁用的危险函数（烧进入口命令，容器重建依然生效）；置空可关闭
    php_disable_functions: str = field(default_factory=lambda: _env(
        "PHP_DISABLE_FUNCTIONS",
        "exec,shell_exec,system,passthru,proc_open,popen,pcntl_exec,pcntl_fork,"
        "pcntl_waitpid,show_source,dl,syslog,ini_alter,ini_restore,chroot,chgrp,chown"))

    @property
    def all_images(self) -> list:
        """全部允许的运行环境（白名单校验统一入口）。"""
        return self.python_images + self.php_images + self.node_images + self.go_images


settings = Settings()
