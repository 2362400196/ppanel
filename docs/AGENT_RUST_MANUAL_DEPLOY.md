# PPanel Rust 被控 · 手动部署指南

> 适用于不使用 `installer/install.sh`、希望自己掌控每一步的部署场景。
> 一键安装请直接看 README 的「Rust 被控部署」一节（`bash install.sh agent-rs`）。

| 项 | 值 |
|---|---|
| 发布包 | `ppanel-agent-linux-amd64.tar.gz`（约 11 MB） |
| 包内结构 | `./ppanel-agent`、`./app/panel_ui/`（约 15 MB）、`./VERSION` |
| 版本 | `ppanel-agent 0.1.0 (x86_64-unknown-linux-musl)` |
| 包 sha256 | `fabf4ed38e22dae8bdb404ff1f7bfa57e0ad6372b28c457ba4a6ef7fa96a88a7` |
| 二进制 | 静态 ELF 18 MB，**零 glibc 依赖**，不需要装 Rust / Python |
| 已验证系统 | Ubuntu 24.04 / Debian 12（x86_64） |

---

## 0. 手动部署 vs 一键安装

| | 一键 `install.sh agent-rs` | 本文手动部署 |
|---|---|---|
| 装 Docker / Caddy | Rust 路径**不装**（连同机 Python 路径才会） | 你自己负责 |
| 生成 `.env` | 自动随机化 | 你手写，见第 3 步 |
| 复用旧库 | 自动从 Python 版迁移 | 手动改 `DB_URL` 指过去 |
| 适用 | 常规重装 / 原地替换 | 定制目录、特殊 user、离线环境、需要逐步核对 |

> ⚠️ Rust 版**只包含被控本体**。它依赖宿主机已有的 Docker 来跑实例；Docker 没装好时服务能起来、`/health` 也正常，但所有实例操作都会失败。

---

## 1. 前置检查

```bash
# 1) 架构必须是 x86_64（arm64 需另行交叉编译 arm64 包）
uname -m

# 2) systemd 可用
systemctl --version | head -1

# 3) Docker 必须已运行（被控通过 /var/run/docker.sock 管理实例）
docker info --format '{{.ServerVersion}}'
ls -l /var/run/docker.sock

# 4) 9100 端口空闲（若旧 Python 被控还在，先停掉）
ss -lntp | grep ':9100' || echo "9100 空闲"

# 5) 磁盘与内存（面板资源 15 MB，实例数据另算）
df -h / /data 2>/dev/null; free -m | head -2

# 6) 基础工具
command -v curl tar openssl
```

必须满足：`uname -m` 为 `x86_64`、Docker 正常、9100 无占用。任一条不满足，先解决再往下走。

---

## 2. 解包与校验

```bash
# 上传发布包（在你自己的机器上执行）
# scp ppanel-agent-linux-amd64.tar.gz root@<服务器IP>:/root/

cd /root
sha256sum ppanel-agent-linux-amd64.tar.gz
# 应为 fabf4ed38e22dae8bdb404ff1f7bfa57e0ad6372b28c457ba4a6ef7fa96a88a7
```

校验不一致就**停止**，不要继续装。

规划好目录（下文统一用这两个路径，可自行替换）：

```
/opt/ppanel-agent/                 安装目录
├── ppanel-agent                   静态二进制（755）
├── VERSION                        版本行（可选，install.sh status 会读）
├── .env                           配置（600）
├── app/panel_ui/                  面板前端资源（约 15 MB）
└── data/ppanel.db                 SQLite 库（首次启动自动建表）

/data/inst/                        实例数据根目录（DATA_ROOT）
└── <实例ID>/                      每个实例一个子目录，挂进容器 /app
```

```bash
mkdir -p /opt/ppanel-agent/app /opt/ppanel-agent/data /data/inst
chmod 755 /opt/ppanel-agent /data/inst

mkdir -p /tmp/ppanel-agent-unpack
tar xzf ppanel-agent-linux-amd64.tar.gz -C /tmp/ppanel-agent-unpack

install -m 0755 /tmp/ppanel-agent-unpack/ppanel-agent /opt/ppanel-agent/ppanel-agent
rm -rf /opt/ppanel-agent/app/panel_ui
cp -r /tmp/ppanel-agent-unpack/app/panel_ui /opt/ppanel-agent/app/panel_ui
cp /tmp/ppanel-agent-unpack/VERSION /opt/ppanel-agent/VERSION 2>/dev/null || true

ls -l /opt/ppanel-agent/ppanel-agent
ls /opt/ppanel-agent/app/panel_ui/index.html
```

> 面板资源必须落在 `app/panel_ui/`，且启动时通过 `PPANEL_UI_DIR` 指过去。放错位置的唯一症状是 API 全部正常、`/panel` 返回 404。

---

## 3. 写配置文件 `.env`

被控按以下顺序找配置：环境变量 `PPANEL_ENV_FILE` → **当前工作目录及往上 4 级** 的 `.env` → 可执行文件所在目录往上的 `.env`。
所以只要 systemd 的 `WorkingDirectory=/opt/ppanel-agent`，把 `.env` 放在 `/opt/ppanel-agent/.env` 即可，无需额外指定。

### 3.1 最小可用配置

```bash
umask 077
cat > /opt/ppanel-agent/.env <<EOF
# ---- 基础 ----
DATA_ROOT=/data/inst
AGENT_HOST=0.0.0.0
AGENT_PORT=9100

# ---- 鉴权（生产必须自己生成，勿用示例值）----
NODE_TOKEN=ppnode_$(openssl rand -hex 16)
JWT_SECRET=$(openssl rand -hex 32)

# ---- 数据库：绝对路径必须四个斜杠 ----
DB_URL=sqlite:////opt/ppanel-agent/data/ppanel.db
EOF
chmod 600 /opt/ppanel-agent/.env
grep -E '^(NODE_TOKEN|DB_URL)=' /opt/ppanel-agent/.env
```

**斜杠数量是这里的头号坑**：`sqlite:////opt/...`（4 个斜杠）才是绝对路径；`sqlite:///opt/...`（3 个）会被当成相对路径，sqlx 会静默新建一个空库，表现为「服务正常但实例全没了」，且日志无任何报错。启动日志里 `[agent] 数据库：/opt/...` 那一行就是给你核对绝对路径的。

### 3.2 可选配置

按需追加到同一个文件（完整清单见附录 A）：

```bash
cat >> /opt/ppanel-agent/.env <<EOF

# 对外开通接口 /open/*（不配则所有 /open/* 一律 403）
# OPEN_API_KEY=$(openssl rand -hex 24)

# 面板对外地址（用于实例面板里生成回调/分享链接）
# PANEL_PUBLIC_URL=https://panel.example.com

# 实例端口分配区间（默认 30000-39999，映射到容器内 8000）
# PORT_START=30000
# PORT_END=39999
# INNER_APP_PORT=8000

# 单文件上传上限（MB）
# UPLOAD_LIMIT_MB=50

# 容器网络与命名前缀（同机多套被控时必须区分，见第 9 节）
# DOCKER_NETWORK=ppanel-net
# CONTAINER_PREFIX=ppanel-
EOF
```

---

## 4. 注册 systemd 服务

必须以 **root** 运行：被控要读写 `/var/run/docker.sock`、管理宿主机文件与备份。降权到普通用户需要额外处理 docker 组与目录权限，不推荐。

```bash
cat > /etc/systemd/system/ppanel-agent.service <<'EOF'
[Unit]
Description=PPanel Agent (Rust, standalone panel + docker node agent)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/ppanel-agent
ExecStart=/opt/ppanel-agent/ppanel-agent
Environment=PPANEL_UI_DIR=/opt/ppanel-agent/app/panel_ui
Environment=AGENT_PORT=9100
Restart=on-failure
RestartSec=3
# 大量并发上传/下载与容器 exec 需要较高 fd 上限
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now ppanel-agent
systemctl status ppanel-agent --no-pager
```

`AGENT_PORT` 在 unit 里显式给了一份，是为了 `Environment` 优先于 `.env`、避免改文件后忘记重启导致端口认知不一致；想改端口就改这里和 `.env` 两处。

---

## 5. 启动自检

```bash
TOKEN=$(grep -E '^NODE_TOKEN=' /opt/ppanel-agent/.env | cut -d= -f2-)

# 1) 健康检查：预期 {"ok":true}
curl -s http://127.0.0.1:9100/health

# 2) 节点鉴权接口：预期 {"ok":true,"service":"ppanel-agent"}
curl -s -H "X-Node-Token: $TOKEN" http://127.0.0.1:9100/agent/ping

# 3) 未带 token 应被拒：预期 401
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9100/agent/instances

# 4) Docker 连通性（这一步能证明 socket 权限没问题）
curl -s -H "X-Node-Token: $TOKEN" http://127.0.0.1:9100/agent/docker/info | head -c 200; echo

# 5) 面板前端：预期 200，且返回 HTML
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:9100/panel
curl -s http://127.0.0.1:9100/panel | head -5

# 6) 宿主机 CPU/内存/磁盘统计（确认走的是 Linux statvfs 分支，disk.percent 应有真实值）
curl -s -H "X-Node-Token: $TOKEN" http://127.0.0.1:9100/agent/host/stats

# 7) 启动日志关键三行
journalctl -u ppanel-agent -n 30 --no-pager | grep -E '数据库|已就绪|开通接口'
```

期望的日志（顺序可能略有差异）：

```
[agent] 数据库：/opt/ppanel-agent/data/ppanel.db
[agent] 开通接口未启用（未配置 OPEN_API_KEY）
[agent] 已就绪：http://0.0.0.0:9100
```

`数据库：` 后面必须是绝对路径，且与你写的 `DB_URL` 一致 —— 这一行不对，后面所有"实例消失"类问题都源于此。

内存占用参考：空载 RSS 约 **10-12 MB**（同机 Python 版约 80 MB）。

---

## 6. 接入主控

被控装好后，主控本身并不会自动发现它，需要在主控里登记一条节点：

1. 主控后台 → **节点管理 → 添加节点**
2. **节点地址**：`http://<服务器公网IP>:9100`
3. **节点 Token**：`/opt/ppanel-agent/.env` 里的 `NODE_TOKEN`
4. 保存后刷新，节点应显示在线，并带 CPU / 内存 / 磁盘数据

命令行取 Token：

```bash
grep -E '^NODE_TOKEN=' /opt/ppanel-agent/.env | cut -d= -f2-
```

> 如果这台机器之前装过 Python 被控、且主控里已有该节点记录：只要复用同一份 `.env`（Token 不变），主控无需重新添加。本例中主控 `nodes` 表已被清空，需要重新添加。

---

## 7. 防火墙

被控自身不做端口放行，两处都要开：

```bash
# 主机防火墙
ufw allow 9100/tcp
# 或 firewalld
# firewall-cmd --permanent --add-port=9100/tcp && firewall-cmd --reload
```

**云服务器还需在厂商控制台的安全组放行 TCP 9100**，否则本机 `curl` 通、主控侧显示节点不可达（主控心跳会记录 `502: 被控节点不可达`）。

---

## 8. 升级 / 回滚 / 卸载

### 升级（保留数据）

```bash
systemctl stop ppanel-agent
install -m 0755 /tmp/ppanel-agent-unpack/ppanel-agent /opt/ppanel-agent/ppanel-agent.new
mv /opt/ppanel-agent/ppanel-agent.new /opt/ppanel-agent/ppanel-agent
rm -rf /opt/ppanel-agent/app/panel_ui && cp -r /tmp/ppanel-agent-unpack/app/panel_ui /opt/ppanel-agent/app/panel_ui
systemctl start ppanel-agent
curl -s http://127.0.0.1:9100/health
```

数据库结构在启动时自动 `CREATE TABLE IF NOT EXISTS` + 补列迁移，升级不需要手动改表。`.env`、`data/ppanel.db`、`/data/inst` 全部保留。

### 回滚到 Python 版

两套实现的表结构与 `.env` 完全兼容（`instances` 23 列同序），可以互相指同一个库：

1. 停 Rust 服务、移走二进制
2. 恢复 Python 版代码目录，`ExecStart` 改回 `.venv/bin/uvicorn agent_main:app --host 0.0.0.0 --port 9100`
3. `.env` 里的 `DB_URL` 若为四斜杠绝对路径，Python（SQLAlchemy）同样能正确解析

### 卸载

```bash
systemctl disable --now ppanel-agent
rm -f /etc/systemd/system/ppanel-agent.service
systemctl daemon-reload
rm -rf /opt/ppanel-agent

# 实例数据与容器：确认不再需要后再删
# docker ps -a --filter "name=ppanel-"
# docker rm -f $(docker ps -aq --filter "name=ppanel-")
# rm -rf /data/inst
```

**`/data/inst` 是租户业务数据，删除前务必确认。** 主控侧的节点记录也要一并清理，否则会一直显示该节点离线。

---

## 9. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| 服务正常，但「实例全部消失」 | `DB_URL` 写成三斜杠，建了空库 | 改 `sqlite:////绝对路径`；核对日志 `[agent] 数据库：` 是否绝对路径 |
| `/panel` 返回 404，API 正常 | `PPANEL_UI_DIR` 没设或路径不对 | unit 里加 `Environment=PPANEL_UI_DIR=/opt/ppanel-agent/app/panel_ui`，确认 `app/panel_ui/index.html` 存在 |
| 所有 `/agent/*` 返回 401 | `NODE_TOKEN` 为空，或 `.env` 没被加载 | 检查 `.env` 位置与 `WorkingDirectory`；`Environment` 会覆盖 `.env`，别写冲突值 |
| 服务启动即退出，`status=1` | 9100 被占用（旧 Python 被控还在） | `ss -lntp \| grep 9100`，先停旧服务 |
| `/agent/docker/info` 报错 `permission denied` | 非 root 运行，或 Docker 未启动 | 用 root 运行该服务；`systemctl status docker` |
| 主控显示节点离线，本机 curl 正常 | 云安全组未放行 9100 | 控制台放行 TCP 9100 |
| `/open/*` 一直 403 | 未配 `OPEN_API_KEY` | 这是设计行为：未启用时无论请求体如何都返回 403 |
| 实例创建成功但 60 秒内被杀 | 同机第二套被控的孤儿对账在清理 `ppanel-*` | 两套必须用不同 `CONTAINER_PREFIX`（如 `CONTAINER_PREFIX=rsagent-`），或只保留一套 |
| Web 终端连不上 | 宿主机无 `bash`，或 `SHELL` 指向不存在的路径 | `apt install bash`；或显式 `Environment=SHELL=/bin/bash` |
| 面板资源加载 404（JS/CSS） | 用反向代理时路径前缀被改写 | 直接访问 `http://IP:9100/panel` 验证；反代需保留原路径不做 rewrite |

盯日志：

```bash
journalctl -u ppanel-agent -f
journalctl -u ppanel-agent -n 100 --no-pager
```

---

## 附录 A：环境变量全表

读取自 `src/config.rs`，默认值与代码一致。`必填` 列指生产环境。

| 变量 | 默认值 | 必填 | 说明 |
|---|---|---|---|
| `DATA_ROOT` | `/data/inst` | 建议显式 | 实例数据根目录，每实例一个子目录挂到容器 `/app` |
| `AGENT_HOST` | `0.0.0.0` | 否 | 监听地址；只走内网可设 `127.0.0.1` 配合反代 |
| `AGENT_PORT` | `9100` | 否 | 监听端口，主控节点地址要一致 |
| `NODE_TOKEN` | 空 | **是** | 主控调 `/agent/*` 的 `X-Node-Token`；为空则全部 401 |
| `JWT_SECRET` | 内置 dev 值 | **是** | 实例面板 JWT 签名密钥，默认值是公开的，必须替换 |
| `DB_URL` | `sqlite:///./data/ppanel.db` | **是** | 建议 `sqlite:////opt/ppanel-agent/data/ppanel.db`（四斜杠） |
| `OPEN_API_KEY` | 空 | 否 | 启用 `/open/*` 商城主控侧接口（`X-API-Key`）；空则 403 |
| `PANEL_API_KEY` | 空 | 否 | 面板侧接口密钥 |
| `PANEL_PUBLIC_URL` | 空 | 否 | 面板对外地址，用于生成外链 |
| `WEBHOOK_URL` | 空 | 否 | 到期/异常通知回调 |
| `WEBHOOK_WARN_DAYS` | `3` | 否 | 到期前提醒天数 |
| `EXPIRED_PANEL_MODE` | `readonly` | 否 | 实例过期后面板行为：`readonly` / `off` |
| `PORT_START` / `PORT_END` | `30000` / `39999` | 否 | 实例对外端口分配区间 |
| `INNER_APP_PORT` | `8000` | 否 | 容器内应用端口（映射目标） |
| `UPLOAD_LIMIT_MB` | `50` | 否 | 单文件上传上限 |
| `PIDS_LIMIT` | `256` | 否 | 容器 PIDs 上限 |
| `DOCKER_NETWORK` | `ppanel-net` | 否 | 实例使用的 docker 网络 |
| `CONTAINER_PREFIX` | `ppanel-` | 否 | 实例容器名前缀，孤儿清理按此匹配，多套被控必须区分 |
| `DOCKER_VOLUME_ROOT` | 空 | 否 | 数据卷根目录覆盖（默认走 `DATA_ROOT`） |
| `PYTHON_IMAGES` | `python:3.9/3.10/3.11/3.12-slim` | 否 | 可选运行时镜像，逗号分隔 |
| `PHP_IMAGES` | `php:8.2/8.3/8.4-cli` | 否 | 同上 |
| `NODE_IMAGES` | `node:20-slim,node:22-slim` | 否 | 同上 |
| `GO_IMAGES` | `golang:1.23/1.24-alpine` | 否 | 同上 |
| `PHP_DISABLE_FUNCTIONS` | 一长串危险函数 | 否 | PHP 实例默认禁用函数 |
| `ADMIN_USERNAME` / `ADMIN_PASSWORD` | `admin` / `admin123` | — | **兼容保留，Rust 版不生效**：被控没有独立管理员登录 |

进程级变量（不进 `.env`，需在 unit 里 `Environment=` 设置）：

| 变量 | 说明 |
|---|---|
| `PPANEL_UI_DIR` | 面板前端资源目录，**不设则 `/panel` 404** |
| `PPANEL_ENV_FILE` | 强制指定 `.env` 路径（多环境部署时用） |
| `SHELL` | Web 终端使用的 shell，默认 `/bin/bash` |
| `INNER_SHELL_MODE` | 宿主机命令执行方式：`auto`（默认）/ `local` / `wsl` |
| `RUST_LOG` | tracing 过滤级别，如 `RUST_LOG=debug` |

> 注：`PANEL_HOST` / `PANEL_PORT`（默认 `127.0.0.1:8000`）是从 Python 版继承的字段，Rust 版读入但不用于监听 —— 面板和 API 都在 `AGENT_HOST:AGENT_PORT` 同一个端口上，靠路径区分。

---

## 附录 B：端点分区

所有接口都在同一个端口（默认 9100），按路径前缀区分鉴权方式：

| 前缀 | 鉴权 | 用途 |
|---|---|---|
| `/health` | 无 | 存活探针，返回 `{"ok":true}` |
| `/agent/*` | `X-Node-Token` | 主控调用：实例增删启停、Docker 管理、文件、备份、防火墙、安全、升级 |
| `/agent/ws/*` | `X-Node-Token` | 主控侧 WebSocket（容器终端、宿主终端） |
| `/panel`、`/panel/*` | 实例面板 JWT | 租户面板：实例信息、文件、终端、站点、数据库 |
| `/panel/ws/*` | 面板 JWT | 租户侧终端等实时通道 |
| `/open/*` | `X-API-Key` | 商城/主控开通接口；未配 `OPEN_API_KEY` 时恒 403 |

### 主机系统运维接口（`/agent/host/*`，`X-Node-Token`）

主控「系统运维」页面通过 `/api/host/<path>?node=<id>` 转发到这些端点：

| 端点 | 参数 | 说明 |
|---|---|---|
| `GET /agent/host/system` | — | 主机名、发行版、内核、架构、uptime、CPU（含采样）、内存、swap、load、磁盘、网卡 |
| `GET /agent/host/processes` | `sort=cpu\|mem`、`limit=1-200` | `ps` 解析的进程列表 |
| `GET /agent/host/services` | — | systemd 服务清单（含 `active` / `sub` / 自启状态） |
| `POST /agent/host/services/action` | `{"unit":"caddy.service","action":"start"}` | 支持 `start/stop/restart/reload/enable/disable`，单元名走白名单校验 |
| `GET /agent/host/ports` | — | `ss -lntup` 监听端口 → 进程/PID 映射 |
| `GET /agent/host/journal` | `unit=`、`lines=10-500` | journald 日志 |
| `GET /agent/host/users` | — | `/etc/passwd` 账户 + `who` 在线会话 |
| `GET /agent/host/ssh-logins` | `limit=` | SSH 成功/失败登录审计 |

> 这些接口只做「机器级」读写，不触碰实例目录或容器生命周期；实例相关操作仍然走 `/agent/instances/*`。

面板登录用的是**实例的 `panel_token`**（长度 8-128），不是管理员账号密码：

```bash
curl -s -X POST http://127.0.0.1:9100/panel/login \
  -H 'Content-Type: application/json' \
  -d '{"token":"<实例面板令牌>"}'
# 返回 {"token":"<面板JWT>","instance":{...}}
```

---

## 附录 C：与 Python 版的差异（部署角度）

| 维度 | Python 版 | Rust 版 |
|---|---|---|
| 依赖 | Python 3 + venv（数百 MB） | 单个静态二进制，无运行时依赖 |
| 空载内存 | 约 80 MB | 约 10-12 MB |
| 目录 | `/opt/ppanel/agent/backend`（在源码树内） | `/opt/ppanel-agent`（与源码树解耦） |
| 启动方式 | `uvicorn agent_main:app --port 9100` | `ExecStart=/opt/ppanel-agent/ppanel-agent` |
| 数据库 | `DB_URL` 走 SQLAlchemy | 同一张库、同一套表，可互换（`instances` 23 列同序） |
| 面板 | `/panel` | `/panel`（同一套前端资源） |
| 管理员登录 | 有（首次启动自动创建管理员，`ADMIN_*` 生效） | 无（面板按实例 token 登录，`ADMIN_*` 仅保留） |
