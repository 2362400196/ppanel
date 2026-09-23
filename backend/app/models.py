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


class OpLog(Base):
    __tablename__ = "op_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
