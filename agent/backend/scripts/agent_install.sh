#!/usr/bin/env bash
# ============================================================
# PPanel 被控 Agent 服务器一键部署脚本
#
# 前提：已把 backend 代码包上传解压到服务器（见 deploy 说明），
#       服务器已安装 Docker。
#
# 用法（root 执行）：
#   bash scripts/agent_install.sh                  # 默认安装到 /root/ppanel-agent
#   bash scripts/agent_install.sh /opt/ppanel-agent  # 指定安装目录
# ============================================================
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"   # 代码包根（backend）
DEST_DIR="${1:-/root/ppanel-agent}"

[[ $EUID -eq 0 ]] || { echo "请用 root 执行"; exit 1; }
command -v docker >/dev/null || { echo "未检测到 Docker，请先安装：curl -fsSL https://get.docker.com | bash"; exit 1; }
command -v rsync >/dev/null || { echo "未检测到 rsync，请先安装（apt install rsync 或 yum install rsync）"; exit 1; }

echo "== 1/6 同步代码 -> $DEST_DIR"
mkdir -p "$DEST_DIR"
rsync -a --delete \
  --exclude='.venv' --exclude='__pycache__' --exclude='data' \
  --exclude='.env' --exclude='tests' --exclude='.git' \
  "$SRC_DIR"/ "$DEST_DIR"/
cd "$DEST_DIR"

echo "== 2/6 安装 uv 与依赖"
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
uv sync --frozen --no-dev

echo "== 3/6 生成配置 .env（已存在则跳过）"
if [[ ! -f .env ]]; then
  NODE_TOKEN="ppnode_$(openssl rand -hex 16)"
  OPEN_API_KEY="ppopen_$(openssl rand -hex 16)"
  cat > .env <<EOF
# ===== 被控 Agent 生产配置 =====
AGENT_HOST=0.0.0.0
AGENT_PORT=9100

# 主控接入令牌：主控「节点管理-添加节点」时填这个
NODE_TOKEN=$NODE_TOKEN
# 商城开通密钥：发给你的商城服务端（/open/* 接口用），泄露即换
OPEN_API_KEY=$OPEN_API_KEY
# 面板对外地址（开通响应里 panel_url 的来源），建议配置，如：
# PANEL_PUBLIC_URL=http://服务器IP:9100/panel
PANEL_PUBLIC_URL=
# 商城 Webhook 回调（可选）
WEBHOOK_URL=

JWT_SECRET=$(openssl rand -hex 32)
JWT_EXPIRE_MINUTES=720
DATA_ROOT=/data/inst
DB_URL=sqlite:///./data/ppanel.db
PORT_START=30000
PORT_END=39999
INNER_APP_PORT=8000
PIDS_LIMIT=256
UPLOAD_LIMIT_MB=50
DOCKER_NETWORK=ppanel-net
# 实例到期后面板行为：readonly（默认，只读可备份）/ block（全锁）
EXPIRED_PANEL_MODE=readonly
EOF
  echo "已生成 .env，密钥如下（请妥善保管）："
  echo "  NODE_TOKEN   = $NODE_TOKEN   # 给主控"
  echo "  OPEN_API_KEY = $OPEN_API_KEY # 给商城"
else
  echo ".env 已存在，保留原配置"
fi

echo "== 4/6 创建数据目录与 systemd 服务"
mkdir -p /data/inst
cat > /etc/systemd/system/ppanel-agent.service <<EOF
[Unit]
Description=PPanel Agent (Docker node agent)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$DEST_DIR
ExecStart=$DEST_DIR/.venv/bin/uvicorn agent_main:app --host 0.0.0.0 --port 9100
Restart=on-failure
RestartSec=3
TimeoutStopSec=8s

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now ppanel-agent

echo "== 5/6 启动校验"
sleep 2
systemctl is-active ppanel-agent
curl -s -m 5 http://127.0.0.1:9100/agent/ping && echo

echo "== 6/6 收尾建议（手动执行）"
echo "  a. 预拉镜像（买家开通更快，避免首次在线拉取超时）："
echo "     docker pull python:3.9-slim"
echo "  b. 容器内网隔离（安全配套，阻断容器访问宿主机内网）："
echo "     bash $DEST_DIR/scripts/block_container_lan.sh"
echo "  c. 防火墙放行：9100（面板/接口）、30000-39999（实例端口）"
echo ""
echo "部署完成。查看日志：journalctl -u ppanel-agent -f"
