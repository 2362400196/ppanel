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
    balance_cents: Mapped[int] = mapped_column(Integer, default=0)  # 钱包余额（分）
    points: Mapped[int] = mapped_column(Integer, default=0)         # 积分（签到/消费获得，可兑换天数）
    level_exp: Mapped[int] = mapped_column(Integer, default=0)      # 等级经验（累计实付金额：分）
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
    # 本次启动时间（被控容器 StartedAt 快照；status 非 running 时置空，前端计运行时长）
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
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


class NodeHeartbeat(Base):
    """节点心跳快照：主控每 60s ping 一次被控，记录可达性/延迟/资源水位。

    供稳定性评分使用（在线率、资源水位）；只滚动保留 14 天。"""
    __tablename__ = "node_heartbeats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    node_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)      # agent 可达
    latency_ms: Mapped[float] = mapped_column(Float, default=0.0)
    mem_percent: Mapped[float | None] = mapped_column(Float, nullable=True)  # 节点内存水位（可达时采样）
    disk_percent: Mapped[float | None] = mapped_column(Float, nullable=True)  # 节点磁盘水位
    detail: Mapped[str] = mapped_column(String(255), default="")  # 不可达原因摘要


class InstanceEvent(Base):
    """实例生命周期事件：崩溃 / 人为停止等，供稳定性评分区分意外与计划内。"""
    __tablename__ = "instance_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_uuid: Mapped[str] = mapped_column(String(36), index=True)
    node_id: Mapped[int] = mapped_column(Integer, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    event: Mapped[str] = mapped_column(String(24))  # crash / stopped_planned / started
    detail: Mapped[str] = mapped_column(String(255), default="")


class Domain(Base):
    """自定义域名绑定：一条实例只绑一个域名（MVP）。"""
    __tablename__ = "domains"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    instance_uuid: Mapped[str] = mapped_column(String(36), unique=True, index=True)
    hostname: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Ticket(Base):
    """工单：用户提交问题/需求，管理员回复处理。"""
    __tablename__ = "tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    title: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(12), default="open", index=True)  # open/closed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class TicketMsg(Base):
    """工单会话消息。content 可为空（纯附件消息）。"""
    __tablename__ = "ticket_msgs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer)
    is_admin: Mapped[int] = mapped_column(Integer, default=0)  # 1=管理员回复
    content: Mapped[str] = mapped_column(Text, default="")
    file_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 关联附件
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TicketFile(Base):
    """工单附件：图片/文件落盘 data/ticket_files/，随机名存储防路径问题。"""
    __tablename__ = "ticket_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    orig_name: Mapped[str] = mapped_column(String(255))
    stored_name: Mapped[str] = mapped_column(String(64), unique=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    is_image: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Checkin(Base):
    """每日签到：一人一天一条（user_id+day 唯一）。"""
    __tablename__ = "checkins"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    day: Mapped[str] = mapped_column(String(10), index=True)   # YYYY-MM-DD（本地时区）
    points: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Coupon(Base):
    """优惠券：管理员创建发放，购买时凭 code 抵扣。"""
    __tablename__ = "coupons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)              # 抵扣金额（分）
    min_spend_cents: Mapped[int] = mapped_column(Integer, default=0)  # 使用门槛（商品原价）
    total: Mapped[int] = mapped_column(Integer, default=1)          # 发放总量
    used: Mapped[int] = mapped_column(Integer, default=0)           # 已核销数
    expire_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    enabled: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class CouponUse(Base):
    """券核销记录。"""
    __tablename__ = "coupon_uses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    coupon_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    order_no: Mapped[str] = mapped_column(String(32))
    amount_cents: Mapped[int] = mapped_column(Integer)   # 实际抵扣
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


class AppSetting(Base):
    """平台级键值配置（如 DeepSeek API Key 等敏感信息，只存服务端）。"""
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Order(Base):
    """钱包流水：recharge=微信充值单（查单驱动入账），shop=余额消费单，admin=管理员调整。"""
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    out_trade_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16), default="recharge")  # recharge/shop/admin
    plan_id: Mapped[int] = mapped_column(Integer, default=0)
    plan_name: Mapped[str] = mapped_column(String(64), default="")
    amount_cents: Mapped[int] = mapped_column(Integer)          # 金额（分）：充值=入账，消费=扣款
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)  # pending/paid/failed
    code_url: Mapped[str] = mapped_column(String(255), default="")
    instance_uuid: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
