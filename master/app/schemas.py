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
    start_cmd: str = Field(default="python main.py", max_length=512)
    node_id: Optional[int] = None  # 管理员可指定节点；默认第一个可用节点
    # 仅 X-API-Key 调用时有效：为指定用户开通
    user_id: Optional[int] = None
    expire_at: Optional[datetime] = None


class InstanceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    start_cmd: Optional[str] = Field(default=None, min_length=1, max_length=512)
    note: Optional[str] = Field(default=None, max_length=255)


class InstallIn(BaseModel):
    file: str = Field(default="requirements.txt", max_length=128)


# ---------- 商城对接 ----------
class OpenInstanceCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    name: str = Field(min_length=1, max_length=64)
    image: str
    start_cmd: str = Field(default="python main.py", max_length=512)
    cpu_limit: float = Field(default=1.0, ge=0.1, le=16)
    mem_limit: int = Field(default=512, ge=64, le=32768)
    disk_quota: int = Field(default=2048, ge=256, le=1048576)
    expire_at: Optional[datetime] = None
    node_id: Optional[int] = None


class OpenRenew(BaseModel):
    expire_at: datetime
