from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
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


class Node(Base):
    __tablename__ = "nodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    base_url: Mapped[str] = mapped_column(String(255))   # 如 http://localhost:9100
    token: Mapped[str] = mapped_column(String(128))      # X-Node-Token
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Instance(Base):
    __tablename__ = "instances"

    uuid: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("nodes.id"), index=True)
    agent_iid: Mapped[int] = mapped_column(Integer, index=True)  # 被控本地实例 ID
    name: Mapped[str] = mapped_column(String(64))
    image: Mapped[str] = mapped_column(String(128))
    start_cmd: Mapped[str] = mapped_column(String(512), default="python main.py")
    ext_port: Mapped[int] = mapped_column(Integer, default=0)
    cpu_limit: Mapped[float] = mapped_column(Float, default=1.0)
    mem_limit: Mapped[int] = mapped_column(Integer, default=512)      # MB
    disk_quota: Mapped[int] = mapped_column(Integer, default=2048)    # MB
    note: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="creating")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    traffic_gb: Mapped[float | None] = mapped_column(Float, nullable=True)  # 月流量限额 GB，空=不限


class OpLog(Base):
    __tablename__ = "op_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    instance_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Metric(Base):
    """用量采样点：每次采样所有 running 实例的一条快照。"""
    __tablename__ = "metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_uuid: Mapped[str] = mapped_column(String(36), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    cpu_percent: Mapped[float] = mapped_column(Float, default=0.0)
    mem_used_mb: Mapped[float] = mapped_column(Float, default=0.0)
    mem_limit_mb: Mapped[float] = mapped_column(Float, default=0.0)
    net_rx_mb: Mapped[float] = mapped_column(Float, default=0.0)  # 累计接收
    net_tx_mb: Mapped[float] = mapped_column(Float, default=0.0)  # 累计发送


class Domain(Base):
    """自定义域名绑定：一条实例只绑一个域名（MVP）。"""
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Plan(Base):
    """商城商品：套餐规格 + 价格，管理员可编辑与上下架。"""
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64))
    desc: Mapped[str] = mapped_column(String(255), default="")
    cpu: Mapped[float] = mapped_column(Float, default=1.0)
    mem: Mapped[int] = mapped_column(Integer, default=512)        # MB
    disk: Mapped[int] = mapped_column(Integer, default=2048)      # MB
    days: Mapped[int] = mapped_column(Integer, default=30)        # 有效期
    traffic_gb: Mapped[int] = mapped_column(Integer, default=0)   # 月流量 GB；0=不限
    price_cents: Mapped[int] = mapped_column(Integer, default=500)  # 价格（分）
    sort: Mapped[int] = mapped_column(Integer, default=0)         # 越小越靠前
    node_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 绑定节点；空=自动分配
    image: Mapped[str] = mapped_column(String(128), default="")  # 运行环境（开通实例的镜像）；空=默认
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # 上架状态
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
