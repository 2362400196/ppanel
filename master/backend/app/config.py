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
    jwt_expire_minutes: int = field(default_factory=lambda: _env_int("JWT_EXPIRE_MINUTES", 43200))  # 默认30天
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
    node_images: list = field(default_factory=lambda: _env_list(
        "NODE_IMAGES",
        ["node:20-slim", "node:22-slim"],
    ))
    go_images: list = field(default_factory=lambda: _env_list(
        "GO_IMAGES",
        ["golang:1.23-alpine", "golang:1.24-alpine"],
    ))
    # PHP 安全：默认禁用的危险函数（需与被控节点 PHP_DISABLE_FUNCTIONS 一致）
    php_disable_functions: str = field(default_factory=lambda: _env(
        "PHP_DISABLE_FUNCTIONS",
        "exec,shell_exec,system,passthru,proc_open,popen,pcntl_exec,pcntl_fork,"
        "pcntl_waitpid,show_source,dl,syslog,ini_alter,ini_restore,chroot,chgrp,chown"))

    @property
    def all_images(self) -> list:
        """全部允许的运行环境（白名单校验统一入口）。"""
        return self.python_images + self.php_images + self.node_images + self.go_images

    # 经主控中转的上传/下载内存上限
    transfer_limit_mb: int = field(default_factory=lambda: _env_int("TRANSFER_LIMIT_MB", 200))

    # 调被控的超时（秒）
    agent_timeout: int = field(default_factory=lambda: _env_int("AGENT_TIMEOUT", 60))
    agent_health_timeout: float = field(default_factory=lambda: _env_float("AGENT_HEALTH_TIMEOUT", 3.0))

    # 用量采样：间隔与保留时长
    metrics_interval_seconds: int = field(default_factory=lambda: _env_int("METRICS_INTERVAL_SECONDS", 60))
    metrics_retention_hours: int = field(default_factory=lambda: _env_int("METRICS_RETENTION_HOURS", 24))

    # ---------- 售卖生命周期（到期状态机 / 额度执行） ----------
    # 到期后动作：suspend=停机保数据（面板只读）；off=只提醒不动容器
    expire_action: str = field(default_factory=lambda: _env("EXPIRE_ACTION", "suspend"))
    # 到期后宽限天数：期内可续费恢复，超过则删除回收（0=永不删除）
    expire_grace_days: int = field(default_factory=lambda: _env_int("EXPIRE_GRACE_DAYS", 7))
    # 到期前提醒天数（逗号分隔）
    expire_remind_days: str = field(default_factory=lambda: _env("EXPIRE_REMIND_DAYS", "7,3,1"))
    # 流量超限动作：stop=停机（可续费/买流量后恢复）；warn=只提醒
    traffic_action: str = field(default_factory=lambda: _env("TRAFFIC_ACTION", "stop"))
    # 磁盘超限动作：stop=停机；warn=只提醒（磁盘统计在被控，每分钟巡检）
    disk_action: str = field(default_factory=lambda: _env("DISK_ACTION", "stop"))


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, default))
    except (TypeError, ValueError):
        return default


settings = Settings()


def env_display_name(image: str) -> str:
    """镜像 → 环境显示名，用于实例命名前缀：python:3.11-slim → Python、php:8.2-cli → PHP。"""
    base = (image or "").split(":")[0].split("/").pop().strip().lower()
    return {"python": "Python", "php": "PHP", "node": "Node", "nodejs": "Node",
            "golang": "Go", "go": "Go", "java": "Java", "nginx": "Nginx",
            "redis": "Redis", "mysql": "MySQL"}.get(base, base.capitalize() or "App")


def default_start_cmd(image: str, mem_limit_mb: int | None = None) -> str:
    """与被控一致的默认启动命令（空 start_cmd 时兜底）。

    各运行时都是一行干净命令：依赖安装/运行参数由被控容器入口自动完成
    （Python=requirements.txt 自举，PHP=禁用函数/上传/内存由入口动态注入）。
    """
    img = image or ""
    if img.startswith("node:"):
        return "node index.js"
    if img.startswith("golang:"):
        return "[ -f go.mod ] || go mod init ppanel-app; go build -o app . && ./app"
    if img.startswith("php:"):
        return "php -S 0.0.0.0:8000 -t /app"
    return "python main.py"
