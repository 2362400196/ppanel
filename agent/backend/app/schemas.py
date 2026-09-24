from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


# ---------- auth ----------
class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=128)


class UserOut(BaseModel):
    id: int
    username: str
    role: str
    created_at: Optional[datetime] = None

    class Config:
        from_attributes = True


# ---------- instances ----------
class InstanceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    image: str
    cpu_limit: float = Field(default=1.0, ge=0.1, le=16)
    mem_limit: int = Field(default=512, ge=64, le=32768)      # MB
    disk_quota: int = Field(default=2048, ge=256, le=1048576)  # MB
    start_cmd: str = Field(default="", max_length=512)  # 空则按镜像取默认（python main.py / php -S …）
    # 仅 X-API-Key 调用时有效：为指定用户开通
    user_id: Optional[int] = None


class InstanceUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=64)
    start_cmd: Optional[str] = Field(default=None, min_length=1, max_length=512)
    note: Optional[str] = Field(default=None, max_length=255)


class InstanceOut(BaseModel):
    id: int
    user_id: int
    name: str
    image: str
    start_cmd: str
    ext_port: int
    cpu_limit: float
    mem_limit: int
    disk_quota: int
    note: str
    status: str
    created_at: Optional[datetime] = None
    expire_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class InstallIn(BaseModel):
    file: str = Field(default="requirements.txt", max_length=128)


# ---------- files ----------
class FileEntry(BaseModel):
    name: str
    is_dir: bool
    size: int
    mtime: float


class SaveIn(BaseModel):
    path: str
    content: str


class MkdirIn(BaseModel):
    path: str


class RenameIn(BaseModel):
    src: str
    dst: str


class CopyIn(BaseModel):
    src: str
    dst: str


class ZipIn(BaseModel):
    paths: list[str]
    name: str
    z_type: str = "tar.gz"


class UnzipIn(BaseModel):
    src: str


class RuntimeIn(BaseModel):
    image: str


class UploadExistsIn(BaseModel):
    path: str = "/"
    file_name: str
    total_size: int = 0


# ---------- admin ----------
class UserCreate(BaseModel):
    username: str = Field(min_length=2, max_length=64)
    password: str = Field(min_length=6, max_length=128)
    role: str = Field(default="user")
