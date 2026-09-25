#!/usr/bin/env bash
# ============================================================
#  PPanel 独立面板（被控节点）一键安装管理器
#
#  交互菜单：   bash install.sh
#  免交互安装： bash install.sh install     （curl | bash 场景：bash -s -- install）
#  其他子命令： reinstall | uninstall | reset-admin | status
#
#  环境变量：PORT=9100  SRC_DIR=/opt/ppanel  REPO_URL=...  PYPI_MIRROR=...
# ============================================================
set -euo pipefail

REPO_URL="${REPO_URL:-https://gitee.com/zhuxiaohuaqn/ppanel.git}"
SRC_DIR="${SRC_DIR:-/opt/ppanel}"
APP_DIR="$SRC_DIR/agent/backend"
SERVICE="ppanel-agent"
PORT="${PORT:-9100}"
PYPI_MIRROR="${PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"

c_g='\033[0;32m'; c_c='\033[0;36m'; c_y='\033[0;33m'; c_r='\033[0;31m'; c_dim='\033[2m'; c_b='\033[1m'; c_off='\033[0m'
info()  { echo -e "  ${c_g}➜${c_off} $*"; }
ok()    { echo -e "  ${c_g}✔${c_off} $*"; }
warn()  { echo -e "  ${c_y}⚠${c_off} $*"; }
fail()  { echo -e "  ${c_r}✘ $*${c_off}" >&2; exit 1; }
step()  { echo -e "\n${c_c}${c_b}── $* ──${c_off}"; }

banner() {
  echo -e "${c_c}"
  echo -e "  ____   ___  ____   ___  _   _ _____ "
  echo -e " |  _ \ / _ \|  _ \ / _ \| \ | | ____|"
  echo -e " | |_) | | | | |_) | | | |  \| |  _|  "
  echo -e " |  __/| |_| |  __/| |_| | |\  | |___ "
  echo -e " |_|    \___/|_|    \___/|_| \_|_____|"
  echo -e "${c_off}${c_dim}          独立面板 · 被控节点安装管理器${c_off}"
  echo -e "${c_dim}  ─────────────────────────────────────────────────────${c_off}"
}

confirm() {  # confirm "标题" -> 0=继续
  read -rp "  ${c_y}$1 [y/N]: ${c_off}" a
  [[ "$a" =~ ^[Yy]$ ]]
}

# ============================================================
#  环境检测
# ============================================================
PKG=""
detect_env() {
  [ "$(id -u)" = 0 ] || fail "请用 root 运行：sudo bash install.sh"
  if command -v apt-get >/dev/null 2>&1; then PKG="apt"
  elif command -v dnf >/dev/null 2>&1; then PKG="dnf"
  elif command -v yum >/dev/null 2>&1; then PKG="yum"
  else fail "未识别的发行版（需要 apt/dnf/yum）"
  fi
  ok "系统环境：$(. /etc/os-release && echo "$PRETTY_NAME") · $PKG"
}

pkg_install() {
  case "$PKG" in
    apt)
      export DEBIAN_FRONTEND=noninteractive
      info "apt-get update（源慢时请耐心等待）..."
      apt-get update -y 2>&1 | tail -2 || true
      info "apt-get install: $*"
      apt-get install -y -o DPkg::Lock::Timeout=120 "$@"
      ;;
    dnf) dnf install -y "$@" ;;
    yum) yum install -y "$@" ;;
  esac
}

switch_mirror() {  # 国内环境自动换 apt 源
  IS_CN=0
  TZ_VAL=$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo "")
  case "$TZ_VAL" in
    Asia/Shanghai|Asia/Chongqing|Asia/Harbin|Asia/Urumqi|Asia/Hong_Kong|Asia/Macau|Asia/Taipei) IS_CN=1 ;;
  esac
  if [ "$IS_CN" = 0 ] && command -v curl >/dev/null 2>&1; then
    C=$(curl -s --max-time 3 https://ipinfo.io/country 2>/dev/null | tr -d '[:space:]')
    [ "$C" = "CN" ] && IS_CN=1
  fi
  if [ "$IS_CN" = 1 ] && [ "$PKG" = "apt" ]; then
    info "检测到国内环境，切换 apt 源为清华镜像..."
    CHANGED=0
    for f in /etc/apt/sources.list /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; do
      [ -f "$f" ] || continue
      grep -qE 'deb\.debian\.org|security\.debian\.org|archive\.ubuntu\.com|security\.ubuntu\.com|raspbian\.raspberrypi\.org|archive\.raspberrypi\.org' "$f" || continue
      [ -f "$f.bak.ppanel" ] || cp "$f" "$f.bak.ppanel"
      sed -i \
        -e 's#deb\.debian\.org#mirrors.tuna.tsinghua.edu.cn#g' \
        -e 's#security\.debian\.org#mirrors.tuna.tsinghua.edu.cn#g' \
        -e 's#archive\.ubuntu\.com#mirrors.tuna.tsinghua.edu.cn#g' \
        -e 's#security\.ubuntu\.com#mirrors.tuna.tsinghua.edu.cn#g' \
        -e 's#raspbian\.raspberrypi\.org/raspbian#mirrors.tuna.tsinghua.edu.cn/raspbian/raspbian#g' \
        -e 's#archive\.raspberrypi\.org/debian#mirrors.tuna.tsinghua.edu.cn/raspberrypi/debian#g' \
        "$f"
      CHANGED=1
    done
    [ "$CHANGED" = 1 ] && ok "apt 源已切换（原文件备份为 *.bak.ppanel）" || ok "apt 源已是国内镜像"
  fi
}

# ============================================================
#  安装步骤
# ============================================================
install_base() {
  step "1/6 安装基础依赖"
  pkg_install curl git ca-certificates
  if ! command -v python3 >/dev/null 2>&1; then
    case "$PKG" in
      apt) pkg_install python3 python3-venv python3-pip ;;
      *)   pkg_install python3 python3-pip ;;
    esac
  fi
  PYV=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
    || fail "需要 Python >= 3.10，当前 $PYV"
  ok "Python $PYV"
}

install_docker() {
  step "2/6 检查 Docker（运行用户实例容器）"
  if ! command -v docker >/dev/null 2>&1; then
    info "安装 Docker..."
    curl -fsSL https://get.docker.com | sh -s -- --mirror Aliyun >/dev/null 2>&1 \
      || curl -fsSL https://get.docker.com | sh >/dev/null 2>&1 \
      || fail "Docker 安装失败，请手动安装后重试"
    ok "Docker 安装完成"
  else
    ok "Docker 已存在：$(docker --version | sed 's/,.*//')"
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1 || fail "Docker 服务未运行（systemctl status docker）"
  ok "Docker 服务运行中"
}

install_caddy() {
  step "3/6 安装 Caddy（域名自动 HTTPS / Let's Encrypt 证书）"
  if command -v caddy >/dev/null 2>&1; then
    ok "Caddy 已存在：$(caddy version 2>/dev/null | head -1)"
  else
    if [ "$PKG" = "apt" ]; then
      info "安装 Caddy（caddy 官方仓库）..."
      apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null 2>&1 || true
      curl -fsSL "https://caddyserver.com/api/download-gpg" 2>/dev/null | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null \
        || curl -fsSL "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" 2>/dev/null | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null || true
      echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
        > /etc/apt/sources.list.d/caddy-stable.list 2>/dev/null || true
      apt-get update -qq >/dev/null 2>&1 || true
      if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
        # 仓库不可达时退回静态二进制（GitHub 直链 / 国内加速）
        info "apt 安装失败，尝试静态二进制..."
        ARCH=$(uname -m); case "$ARCH" in x86_64) CA=amd64 ;; aarch64) CA=arm64 ;; *) CA=amd64 ;; esac
        curl -fsSL --max-time 120 "https://github.com/caddyserver/caddy/releases/latest/download/caddy_${CA}.tar.gz" -o /tmp/caddy.tgz 2>/dev/null \
          || curl -fsSL --max-time 120 "https://ghfast.top/https://github.com/caddyserver/caddy/releases/latest/download/caddy_${CA}.tar.gz" -o /tmp/caddy.tgz 2>/dev/null \
          || { warn "Caddy 安装失败（不影响面板运行，仅「域名与 SSL」功能不可用）"; return 0; }
        tar -xzf /tmp/caddy.tgz -C /usr/local/bin caddy && chmod +x /usr/local/bin/caddy && rm -f /tmp/caddy.tgz
        # 静态安装补 systemd 单元
        cat > /etc/systemd/system/caddy.service <<'UNIT'
[Unit]
Description=Caddy
After=network.target network-online.target
Wants=network-online.target

[Service]
User=root
ExecStart=/usr/local/bin/caddy run --environ --config /etc/caddy/Caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --force
TimeoutStopSec=5s
LimitNOFILE=1048576

[Install]
WantedBy=multi-user.target
UNIT
        systemctl daemon-reload
      fi
    else
      # dnf/yum
      $PKG_INSTALL caddy || { warn "Caddy 安装失败（不影响面板运行，仅「域名与 SSL」功能不可用）"; return 0; }
    fi
    ok "Caddy 安装完成：$(caddy version 2>/dev/null | head -1)"
  fi
  # 主 Caddyfile：确保 include 实例站点目录
  mkdir -p /etc/caddy/sites
  if [ ! -f /etc/caddy/Caddyfile ]; then
    printf '# PPanel 主配置：实例站点在 /etc/caddy/sites/ 下自动管理\nimport /etc/caddy/sites/*\n' > /etc/caddy/Caddyfile
  elif ! grep -q "import /etc/caddy/sites" /etc/caddy/Caddyfile; then
    printf '\nimport /etc/caddy/sites/*\n' >> /etc/caddy/Caddyfile
  fi
  systemctl enable --now caddy >/dev/null 2>&1 || true
  # 80/443 放行（ACME HTTP-01 验证与 HTTPS 访问必需）
  ufw allow 80/tcp >/dev/null 2>&1 || true
  ufw allow 443/tcp >/dev/null 2>&1 || true
  firewall-cmd --permanent --add-service=http >/dev/null 2>&1 || true
  firewall-cmd --permanent --add-service=https >/dev/null 2>&1 || true
  firewall-cmd --reload >/dev/null 2>&1 || true
  ok "Caddy 已启动，80/443 端口已放行"
}

fetch_code() {
  step "4/6 拉取代码"
  if [ -d "$SRC_DIR/.git" ]; then
    MODE_UPGRADE=1
    info "检测到已有安装（$SRC_DIR），更新代码..."
    if git -C "$SRC_DIR" pull --ff-only > /tmp/ppanel-git.log 2>&1; then
      ok "代码已是最新"
    else
      tail -4 /tmp/ppanel-git.log
      warn "git pull 失败，尝试自动修复（本地改动仅限已跟踪文件，不影响 .env 与数据）..."
      # 已跟踪文件的本地改动（如 uv.lock 被改写）→ 还原后重试
      git -C "$SRC_DIR" checkout -- . 2>/dev/null || true
      if git -C "$SRC_DIR" pull --ff-only > /tmp/ppanel-git.log 2>&1; then
        ok "已修复并更新"
      else
        # 远程历史分叉（如强推）→ 硬对齐远程（未跟踪的 .env/data 不受影响）
        BR=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD)
        if git -C "$SRC_DIR" fetch origin > /dev/null 2>&1 \
           && git -C "$SRC_DIR" reset --hard "origin/$BR" > /dev/null 2>&1; then
          ok "已对齐远程分支 $BR"
        else
          warn "无法自动更新，沿用现有代码继续"
        fi
      fi
    fi
  else
    MODE_UPGRADE=0
    info "克隆仓库 -> $SRC_DIR"
    git clone --depth 1 "$REPO_URL" "$SRC_DIR" >/dev/null 2>&1 || fail "克隆失败，检查网络或仓库地址"
  fi
  [ -f "$APP_DIR/agent_main.py" ] || fail "仓库结构异常：找不到 $APP_DIR/agent_main.py"
  ok "代码就绪"
}

install_deps() {
  step "5/6 安装 Python 依赖（uv 加速）"
  cd "$APP_DIR"
  export UV_DEFAULT_INDEX="$PYPI_MIRROR"
  export UV_INDEX_URL="$PYPI_MIRROR"
  export UV_PYTHON_PREFERENCE=only-system
  export UV_PYTHON="$(command -v python3)"

  UV_BIN=""
  [ -x "$HOME/.local/bin/uv" ] && UV_BIN="$HOME/.local/bin/uv"
  [ -z "$UV_BIN" ] && [ -x "$APP_DIR/.venv/bin/uv" ] && UV_BIN="$APP_DIR/.venv/bin/uv"
  if [ -z "$UV_BIN" ]; then
    info "安装 uv 包管理器（清华 PyPI 镜像）..."
    [ -d "$APP_DIR/.venv" ] || python3 -m venv "$APP_DIR/.venv"
    "$APP_DIR/.venv/bin/pip" install -q uv -i "$PYPI_MIRROR" && UV_BIN="$APP_DIR/.venv/bin/uv"
  fi

  UV_OK=0
  if [ -n "$UV_BIN" ]; then
    info "uv sync 安装依赖..."
    # --inexact：保留 venv 内 uv 本体；--no-install-project：平铺结构不构建项目本身
    if "$UV_BIN" sync --inexact --no-dev --no-install-project > /tmp/ppanel-uv-sync.log 2>&1; then
      UV_OK=1
      git checkout -- uv.lock 2>/dev/null || true
    else
      tail -5 /tmp/ppanel-uv-sync.log
      if [ "$PKG" = "apt" ]; then
        warn "疑似缺编译头文件（aarch64 常见），安装工具链后重试..."
        pkg_install libffi-dev python3-dev gcc
        if "$UV_BIN" sync --inexact --no-dev --no-install-project > /tmp/ppanel-uv-sync.log 2>&1; then
          UV_OK=1
          git checkout -- uv.lock 2>/dev/null || true
        else
          tail -5 /tmp/ppanel-uv-sync.log
        fi
      fi
    fi
  fi
  if [ "$UV_OK" != 1 ]; then
    warn "uv 不可用，退回 pip 安装（较慢）..."
    [ -x "$APP_DIR/.venv/bin/pip" ] || python3 -m venv --clear "$APP_DIR/.venv"
    "$APP_DIR/.venv/bin/pip" install -q --upgrade pip -i "$PYPI_MIRROR"
    "$APP_DIR/.venv/bin/pip" install -q . -i "$PYPI_MIRROR"
  fi
  "$APP_DIR/.venv/bin/python" -c "import fastapi, docker, uvicorn" \
    || fail "依赖校验失败（fastapi/docker/uvicorn 导入失败）"
  ok "依赖就绪（校验通过）"
}

setup_service() {
  step "6/6 注册服务并启动"
  mkdir -p /data/inst "$APP_DIR/data"
  if [ ! -f "$APP_DIR/.env" ]; then
    NODE_TOKEN="ppnode_$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    JWT_SECRET="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    # 管理员为内部占位（无人工登录），随机化杜绝弱口令撞库；接口均走 X-Node-Token 鉴权
    ADMIN_PASS="$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)"
    cat > "$APP_DIR/.env" <<EOF
# PPanel 独立面板配置（由 install.sh 生成）
DATA_ROOT=/data/inst

# 管理员为内部占位（无人工登录），已随机化防撞库；接口均走 X-Node-Token / X-API-Key 鉴权
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASS

# JWT 密钥（已随机生成）
JWT_SECRET=$JWT_SECRET

# 节点令牌：主控「节点管理」添加节点时填入
NODE_TOKEN=$NODE_TOKEN
EOF
    ok "配置文件已生成（管理员密码已随机化，不对外展示）"
  else
    NODE_TOKEN=$(grep -E '^NODE_TOKEN=' "$APP_DIR/.env" | cut -d= -f2)
    [ -n "$NODE_TOKEN" ] || fail ".env 缺少 NODE_TOKEN，请补填"
    ok "沿用现有配置文件"
  fi

  info "写入 systemd 服务（端口 $PORT）..."
  cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=PPanel Agent (standalone panel + docker node agent)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/uvicorn agent_main:app --host 0.0.0.0 --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$SERVICE"
  sleep 2
  systemctl is-active --quiet "$SERVICE" || fail "服务启动失败：journalctl -u $SERVICE -n 50"

  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$PORT/tcp" >/dev/null 2>&1 && ok "ufw 已放行 $PORT"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$PORT/tcp" >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1 && ok "firewalld 已放行 $PORT"
  fi
}

summary() {
  IP=$(hostname -I 2>/dev/null | awk '{print $1}'); [ -n "$IP" ] || IP="<服务器IP>"
  echo ""
  if [ "$MODE_UPGRADE" = 1 ]; then
    echo -e "  ${c_g}${c_b}━━━━━━━━━━━━━ 升 级 完 成 ━━━━━━━━━━━━━${c_off}"
  else
    echo -e "  ${c_g}${c_b}━━━━━━━━━━━━━ 安 装 完 成 ━━━━━━━━━━━━━${c_off}"
  fi
  echo -e "  独立面板  ${c_b}http://$IP:$PORT/panel${c_off}"
  echo -e "  说明      仅供主控与 API 调用（X-Node-Token 鉴权），无需登录"
  echo -e "  节点Token ${c_b}$NODE_TOKEN${c_off}"
  echo -e "  ${c_dim}─────────────────────────────────────────────${c_off}"
  echo -e "  下一步    主控「节点管理」→ 添加节点 → 填入上方 Token 与节点地址"
  echo -e "  常用      systemctl restart $SERVICE · journalctl -u $SERVICE -f"
  echo -e "  提醒      云服务器请在控制台安全组放行 TCP $PORT"
  echo -e "  ${c_g}${c_b}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_off}"
}

# ============================================================
#  动作
# ============================================================
action_install() {
  detect_env
  switch_mirror
  install_base
  install_docker
  install_caddy
  fetch_code
  install_deps
  setup_service
  summary
}

action_reinstall() {  # 全新重装：清除旧安装（含数据）后重装
  detect_env
  echo -e "  ${c_y}将删除：$SRC_DIR（代码+配置）、/data/inst（全部实例数据）、$SERVICE 服务${c_off}"
  confirm "确认完全清除并重新安装？" || { warn "已取消"; return 0; }
  systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$SERVICE.service"; systemctl daemon-reload
  rm -rf "$SRC_DIR" /data/inst /tmp/ppanel-uv-sync.log
  ok "旧安装已清除"
  switch_mirror
  install_base
  install_docker
  install_caddy
  fetch_code
  install_deps
  setup_service
  summary
}

action_uninstall() {
  detect_env
  [ -d "$SRC_DIR" ] || [ -f "/etc/systemd/system/$SERVICE.service" ] || fail "未检测到已安装的 PPanel 面板"
  echo -e "  ${c_y}卸载将删除：服务、$SRC_DIR${c_off}"
  confirm "是否同时删除实例数据 /data/inst？（推荐卸载前备份）" \
    && RM_DATA=1 || RM_DATA=0
  confirm "确认卸载？" || { warn "已取消"; return 0; }
  info "停止服务..."
  systemctl disable --now "$SERVICE" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$SERVICE.service"; systemctl daemon-reload
  rm -rf "$SRC_DIR" /tmp/ppanel-uv-sync.log
  [ "$RM_DATA" = 1 ] && { rm -rf /data/inst; ok "实例数据已删除"; } || ok "实例数据保留于 /data/inst"
  ok "PPanel 独立面板已卸载"
}

action_reset_admin() {
  [ -f "$APP_DIR/data/ppanel.db" ] || fail "未找到数据库（$APP_DIR/data/ppanel.db）"
  confirm "删除内置管理员并按 .env 随机密码重建？" || { warn "已取消"; return 0; }
  "$APP_DIR/.venv/bin/python" - <<PYEOF
import sqlite3
c = sqlite3.connect("$APP_DIR/data/ppanel.db")
c.execute("DELETE FROM users WHERE username='admin'")
c.commit()
print("  管理员已删除")
PYEOF
  systemctl restart "$SERVICE"
  ok "已重建（随机密码见 $APP_DIR/.env 的 ADMIN_PASSWORD）"
}

action_status() {
  echo ""
  if systemctl is-active --quiet "$SERVICE" 2>/dev/null; then
    ok "服务状态：${c_g}运行中${c_off}（$SERVICE，端口 $PORT）"
  else
    warn "服务状态：未运行"
  fi
  [ -f "$APP_DIR/.env" ] && info "配置文件：$APP_DIR/.env（NODE_TOKEN=$(grep -E '^NODE_TOKEN=' "$APP_DIR/.env" | cut -d= -f2 | head -c 12)...）"
  info "实时日志：journalctl -u $SERVICE -f"
  echo ""
}

menu() {
  banner
  detect_env || exit 1
  echo ""
  echo -e "  ${c_b}[1]${c_off} 安装 / 升级        ${c_dim}自动识别：已安装则升级，未安装则全新安装${c_off}"
  echo -e "  ${c_b}[2]${c_off} 全新重装            ${c_dim}清除旧面板与全部数据后重装${c_off}"
  echo -e "  ${c_b}[3]${c_off} 卸载                ${c_dim}移除面板，可选保留实例数据${c_off}"
  echo -e "  ${c_b}[4]${c_off} 重置管理员          ${c_dim}删除内置管理员，按随机密码重建${c_off}"
  echo -e "  ${c_b}[5]${c_off} 运行状态"
  echo -e "  ${c_b}[0]${c_off} 退出"
  echo ""
  read -rp "  请选择 [0-5]: " c
  echo ""
  case "$c" in
    1) action_install ;;
    2) action_reinstall ;;
    3) action_uninstall ;;
    4) action_reset_admin ;;
    5) action_status ;;
    0) exit 0 ;;
    *) warn "无效选择"; exit 1 ;;
  esac
}

# ---------- 入口分发 ----------
case "${1:-menu}" in
  menu)        menu ;;
  install)     banner; action_install ;;
  reinstall)   banner; action_reinstall ;;
  uninstall)   banner; action_uninstall ;;
  reset-admin) banner; action_reset_admin ;;
  status)      banner; action_status ;;
  *) echo -e "用法：bash install.sh [menu|install|reinstall|uninstall|reset-admin|status]"; exit 1 ;;
esac
