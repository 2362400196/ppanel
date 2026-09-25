from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- auth ----------
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class ChangePasswordIn(BaseModel):
    old_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=6, max_length=128)


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ---------- admin ----------
class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = Field(default="user")


class NodeCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    base_url: str = Field(min_length=8, max_length=255)  # http(s)://host:port
    token: str = Field(min_length=8, max_length=128)
    note: str = Field(default="", max_length=255)
    enabled: bool = True


class NodeUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    base_url: Optional[str] = Field(default=None, min_length=8, max_length=255)
    token: Optional[str] = Field(default=None, min_length=8, max_length=128)
    note: Optional[str] = Field(default=None, max_length=255)
    enabled: Optional[bool] = None


# ---------- instances（主控侧编排请求体与面板前端一致） ----------
class InstanceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    image: str
    cpu_limit: float = Field(default=1.0, ge=0.1, le=16)
    mem_limit: int = Field(default=512, ge=64, le=32768)      # MB
    disk_quota: int = Field(default=2048, ge=256, le=1048576)  # MB
    start_cmd: str = Field(default="", max_length=512)  # 空则按镜像取默认
    node_id: Optional[int] = None  # 管理员可指定节点；默认第一个可用节点
    # 仅 X-API-Key 调用时有效：为指定用户开通
    user_id: Optional[int] = None
    expire_at: Optional[datetime] = None
    traffic_gb: Optional[float] = None  # 月流量限额 GB；空=不限


class InstanceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    start_cmd: Optional[str] = Field(default=None, min_length=1, max_length=512)
    note: Optional[str] = Field(default=None, max_length=255)


class InstallIn(BaseModel):
    file: str = Field(default="requirements.txt", max_length=128)


# ---------- 商城商品 ----------
class PlanIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    desc: str = Field(default="", max_length=255)
    cpu: float = Field(default=1.0, ge=0.1, le=16)
    mem: int = Field(default=512, ge=64, le=32768)        # MB
    disk: int = Field(default=2048, ge=256, le=1048576)   # MB
    days: int = Field(default=30, ge=1, le=3650)
    traffic_gb: int = Field(default=0, ge=0, le=10240)  # 月流量 GB；0=不限
    price_cents: int = Field(default=500, ge=0, le=100_000_000)  # 分
    sort: int = Field(default=0, ge=0, le=9999)
    node_id: Optional[int] = Field(default=None, ge=1)  # 绑定节点；空=自动分配
    image: str = Field(default="", max_length=128)  # 运行环境镜像；空=默认
    enabled: bool = True


class PlanOut(BaseModel):
    id: int
    name: str
    desc: str
    cpu: float
    mem: int
    disk: int
    days: int
    traffic_gb: int = 0
    price_cents: int
    sort: int
    node_id: Optional[int] = None
    image: str = ""
    enabled: bool
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ---------- 商城对接 ----------
class OpenInstanceCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    image: str
    start_cmd: str = Field(default="", max_length=512)  # 空则按镜像取默认
    cpu_limit: float = Field(default=1.0, ge=0.1, le=16)
    mem_limit: int = Field(default=512, ge=64, le=32768)
    disk_quota: int = Field(default=2048, ge=256, le=1048576)
    expire_at: Optional[datetime] = None
    traffic_gb: Optional[float] = None  # 月流量限额 GB；空=不限
    node_id: Optional[int] = None


class OpenRenew(BaseModel):
    expire_at: datetime
