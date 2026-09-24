# PPanel 后端

Python 环境容器托管面板（单机版 Docker Panel）后端服务。

## 运行

```bash
uv sync                      # 安装依赖
uv run uvicorn app.main:app --port 8000    # 开发启动
# 或
uv run main.py
```

首次启动自动创建管理员（默认 admin / admin123，可用 ADMIN_USERNAME / ADMIN_PASSWORD 覆盖）。

配置全部通过环境变量或 `backend/.env`（参考 `.env.example`）。

## 部署（Linux 生产）

1. 面板与 Docker 同机，通过 `/var/run/docker.sock` 控制（docker-py 自动读取 `DOCKER_HOST`）。
2. 设置 `PANEL_HOST` 对外监听，并务必修改 `JWT_SECRET`。
3. 实例目录默认 `/data/inst/{instance_id}`，可用 `DATA_ROOT` 调整。
4. 执行 `scripts/block_container_lan.sh` 阻断容器访问宿主机内网（安全红线 3 配套）：

```bash
sudo bash scripts/block_container_lan.sh                 # 默认阻断 192.168.0.0/16
sudo bash scripts/block_container_lan.sh 10.8.0.0/16     # 指定内网网段
sudo bash scripts/block_container_lan.sh clean           # 移除规则
```

## 测试

```bash
uv run python tests/smoke_test.py    # 不依赖 Docker：登录/权限/路径穿越防护
```

## 说明

- 镜像约定：官方 python slim 镜像，用户应用监听容器内 8000 端口，映射到外部端口 30000-39999。
- 磁盘配额（disk_quota）本期仅记录与展示，硬限制需文件系统 quota 支持（预留）。
- 管理级接口支持 `X-API-Key` 请求头（设置 `PANEL_API_KEY` 后生效），为商城系统预留。
