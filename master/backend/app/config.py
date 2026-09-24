"""主控全局配置：全部通过环境变量 / master/.env 覆盖。"""
import json
import os
from dataclasses import dataclass, field


def _load_dotenv() -> None:
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
    # 主控监听（默认本机；对外时改 0.0.0.0 并配合反代/鉴权）
    host: str = field(default_factory=lambda: _env("MASTER_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("MASTER_PORT", 8001))

    # 认证
    jwt_secret: str = field(default_factory=lambda: _env(
        "JWT_SECRET", "ppanel-master-dev-secret-change-me"))
    jwt_expire_minutes: int = field(default_factory=lambda: _env_int("JWT_EXPIRE_MINUTES", 720))
    # 商城对接 X-API-Key（空则 /api/open 禁用）
    api_key: str = field(default_factory=lambda: _env("MASTER_API_KEY", ""))

    # 首次启动管理员
    admin_username: str = field(default_factory=lambda: _env("ADMIN_USERNAME", "admin"))
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", "admin123"))

    # 存储
    db_url: str = field(default_factory=lambda: _env("DB_URL", "sqlite:///./data/master.db"))

    # 实例参数（与被控保持一致的展示面）
    python_images: list = field(default_factory=lambda: _env_list(
        "PYTHON_IMAGES",
        ["python:3.9-slim", "python:3.10-slim", "python:3.11-slim", "python:3.12-slim"],
    ))
    php_images: list = field(default_factory=lambda: _env_list(
        "PHP_IMAGES",
        ["php:8.2-cli", "php:8.3-cli", "php:8.4-cli"],
    ))
    # PHP 安全：默认禁用的危险函数（需与被控节点 PHP_DISABLE_FUNCTIONS 一致）
    php_disable_functions: str = field(default_factory=lambda: _env(
        "PHP_DISABLE_FUNCTIONS",
        "exec,shell_exec,system,passthru,proc_open,popen,pcntl_exec,pcntl_fork,"
        "pcntl_waitpid,show_source,dl,syslog,ini_alter,ini_restore,chroot,chgrp,chown"))

    @property
    def all_images(self) -> list:
        """全部允许的运行环境（白名单校验统一入口）。"""
        return self.python_images + self.php_images

    # 经主控中转的上传/下载内存上限
    transfer_limit_mb: int = field(default_factory=lambda: _env_int("TRANSFER_LIMIT_MB", 200))

    # 调被控的超时（秒）
    agent_timeout: int = field(default_factory=lambda: _env_int("AGENT_TIMEOUT", 60))
    agent_health_timeout: float = field(default_factory=lambda: _env_float("AGENT_HEALTH_TIMEOUT", 3.0))

    # 用量采样：间隔与保留时长
    metrics_interval_seconds: int = field(default_factory=lambda: _env_int("METRICS_INTERVAL_SECONDS", 60))
    metrics_retention_hours: int = field(default_factory=lambda: _env_int("METRICS_RETENTION_HOURS", 24))


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


settings = Settings()


def default_start_cmd(image: str, mem_limit_mb: int | None = None) -> str:
    """与被控一致的默认启动命令（空 start_cmd 时兜底，PHP 注入安全参数）。"""
    if not (image or "").startswith("php:"):
        return "python main.py"
    parts = ["php"]
    if settings.php_disable_functions.strip():
        parts.append(f"-d disable_functions={settings.php_disable_functions.strip()}")
    upload = 50  # 与被控 UPLOAD_LIMIT_MB 默认一致
    parts.append(f"-d upload_max_filesize={upload}M")
    parts.append(f"-d post_max_size={upload + 16}M")
    if mem_limit_mb:
        parts.append(f"-d memory_limit={int(mem_limit_mb)}M")
    parts.append("-S 0.0.0.0:8000 -t /app")
    return " ".join(parts)
