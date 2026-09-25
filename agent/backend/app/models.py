from datetime import datetime, timezone

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16), default="user")  # admin / user
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Instance(Base):
    __tablename__ = "instances"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    image: Mapped[str] = mapped_column(String(128))
    start_cmd: Mapped[str] = mapped_column(String(512), default="python main.py")
    ext_port: Mapped[int] = mapped_column(Integer)
    cpu_limit: Mapped[float] = mapped_column(Float, default=1.0)
    mem_limit: Mapped[int] = mapped_column(Integer, default=512)      # MB
    disk_quota: Mapped[int] = mapped_column(Integer, default=2048)    # MB（预留，本期仅记录与展示）
    note: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="creating")  # creating/created/running/exited
    container_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    host_dir: Mapped[str] = mapped_column(String(255), default="")
    # 独立单容器面板令牌：非空即允许该令牌登录 /panel 操作本实例（清空即吊销）
    panel_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # 绑定域名（独立面板设置，agent 按 Host 反代到实例端口）
    domain: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)
    # 到期时间（UTC， naive）：商城开通时写入，续期顺延；空=永不过期
    expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # 开通系统侧对账单号（owner_ref），面板与 /open/* 均可读
    owner_ref: Mapped[str] = mapped_column(String(128), default="")
    # 实例自定义 PHP 禁用函数（逗号分隔；空=使用节点默认 PHP_DISABLE_FUNCTIONS）
    disabled_funcs: Mapped[str] = mapped_column(String(512), default="")
    # 流量限制与计量（GB / MB）：gb 空=不限；used 为本期累计（自然月重置）；last 为采集游标
    traffic_gb: Mapped[float | None] = mapped_column(Float, nullable=True)
    traffic_used_mb: Mapped[float] = mapped_column(Float, default=0.0)
    traffic_last_mb: Mapped[float] = mapped_column(Float, default=0.0)
    traffic_month: Mapped[str] = mapped_column(String(7), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AgentMetric(Base):
    """被控自持的实例用量采样（供独立面板画历史曲线，脱离主控可用）。"""
    __tablename__ = "agent_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0)
    mem_used_mb: Mapped[float] = mapped_column(Float, default=0.0)
    mem_limit_mb: Mapped[float] = mapped_column(Float, default=0.0)
    net_rx_mb: Mapped[float] = mapped_column(Float, default=0.0)
    net_tx_mb: Mapped[float] = mapped_column(Float, default=0.0)


class AgentOpLog(Base):
    """被控自持的操作记录（面板与主控转发的变更都留档）。"""
    __tablename__ = "agent_op_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class MySqlService(Base):
    """节点级共享 MySQL 服务（每版本一个容器，多实例共享进程、各持独立库）。"""
    __tablename__ = "mysql_services"

    version: Mapped[str] = mapped_column(String(16), primary_key=True)  # "5.7"/"8.0"/"8.4"
    container_id: Mapped[str] = mapped_column(String(64), default="")
    root_password: Mapped[str] = mapped_column(String(64), default="")
    host_port: Mapped[int] = mapped_column(Integer, default=0)  # 外网直连端口（映射容器 3306）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InstanceDb(Base):
    """实例的 MySQL 数据库发放记录（一实例一库，删除实例时联动回收）。"""
    __tablename__ = "instance_dbs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    version: Mapped[str] = mapped_column(String(16))
    db_name: Mapped[str] = mapped_column(String(64))
    db_user: Mapped[str] = mapped_column(String(64))
    db_password: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class InstanceCron(Base):
    """实例定时任务（宿主调度，docker exec 进实例容器执行）。"""
    __tablename__ = "instance_crons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_id: Mapped[int] = mapped_column(Integer, index=True)
    schedule: Mapped[str] = mapped_column(String(64))          # 5 段 cron 表达式
    command: Mapped[str] = mapped_column(String(512))
    enabled: Mapped[int] = mapped_column(Integer, default=1)   # 1/0
    last_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(16), default="")   # ok/fail/timeout
    last_output: Mapped[str] = mapped_column(Text, default="")  # 最近一次输出（截断保存）
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HostBackupJob(Base):
    """节点管理员定时备份任务（宿主级：任意容器 / 容器目录 / 数据库整库或表级）。"""
    __tablename__ = "host_backup_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule: Mapped[str] = mapped_column(String(64))           # 5 段 cron 表达式
    kind: Mapped[str] = mapped_column(String(16))               # container / dir / db_full / db_table
    target: Mapped[str] = mapped_column(String(128))            # 容器名；或 "版本:库名"（如 5.7:ppanel_1）
    dir_path: Mapped[str] = mapped_column(String(255), default="")   # kind=dir：容器内目录
    table_name: Mapped[str] = mapped_column(String(64), default="")  # kind=db_table：表名（空=整库）
    keep: Mapped[int] = mapped_column(Integer, default=5)       # 保留最近 N 份，超出自动删最旧
    dest: Mapped[str] = mapped_column(String(8), default="local")    # local / cos / both
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    last_run: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_status: Mapped[str] = mapped_column(String(16), default="")   # running/ok/fail
    last_output: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CosConfig(Base):
    """节点级腾讯云 COS 备份配置（单行，id=1；secret_key 读取时掩码）。"""
    __tablename__ = "cos_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    secret_id: Mapped[str] = mapped_column(String(128), default="")
    secret_key: Mapped[str] = mapped_column(String(128), default="")
    bucket: Mapped[str] = mapped_column(String(128), default="")
    region: Mapped[str] = mapped_column(String(64), default="")     # 如 ap-guangzhou
    prefix: Mapped[str] = mapped_column(String(128), default="ppanel-backups")  # 对象 key 前缀
    keep_local: Mapped[int] = mapped_column(Integer, default=1)     # 上传后是否保留本地副本
    enabled: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class InstanceSite(Base):
    """实例站点配置（Caddy 防护与静态托管；domain 挂在 instances 表）。"""
    __tablename__ = "instance_sites"

    instance_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), default="proxy")      # proxy/static
    static_dir: Mapped[str] = mapped_column(String(255), default="public_html")  # 容器 /app 下相对目录
    auth_user: Mapped[str] = mapped_column(String(64), default="")
    auth_hash: Mapped[str] = mapped_column(String(128), default="")     # bcrypt（caddy hash-password 生成）
    ip_whitelist: Mapped[str] = mapped_column(Text, default="")         # 空格分隔 CIDR
    ip_blacklist: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OpLog(Base):
    __tablename__ = "op_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
