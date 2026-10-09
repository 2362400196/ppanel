#!/usr/bin/env bash
# ============================================================
#  PPanel 总安装器
#
#  一个脚本管好主控 + 被控（交互菜单选择，或子命令免交互）：
#    bash install.sh                   -> 菜单
#    bash install.sh agent             -> 独立面板安装/升级（被控节点）
#    bash install.sh agent-reinstall   -> 被控全新重装（清数据）
#    bash install.sh agent-uninstall   -> 被控卸载
#    bash install.sh agent-reset-admin -> 被控重置管理员
#    bash install.sh master            -> 主控面板（服务器直装，systemd）
#    bash install.sh master-docker     -> 主控面板（Docker）
#    bash install.sh status            -> 运行状态
#
#  环境变量：SRC_DIR=/opt/ppanel  REPO_URL=...  MASTER_PORT=8001  PORT=9100
#            PYPI_MIRROR=...  NPM_REGISTRY=...
# ============================================================
set -euo pipefail

REPO_URL="${REPO_URL:-}"   # 显式指定则优先；留空时按服务器位置自动选源（国内 gitee / 海外含香港 github）
REPO_URL_GITEE="https://gitee.com/zhuxiaohuaqn/ppanel.git"
REPO_URL_GITHUB="https://github.com/2362400196/ppanel.git"
SRC_DIR="${SRC_DIR:-/opt/ppanel}"
MASTER_SERVICE="ppanel-master"
MASTER_PORT="${MASTER_PORT:-8001}"
PYPI_MIRROR="${PYPI_MIRROR:-https://pypi.tuna.tsinghua.edu.cn/simple}"
NPM_REGISTRY="${NPM_REGISTRY:-https://registry.npmmirror.com}"

MASTER_DIR="$SRC_DIR/master"
MASTER_BACKEND="$MASTER_DIR/backend"
MASTER_FRONTEND="$MASTER_DIR/frontend"

AGENT_SERVICE="ppanel-agent"
AGENT_APP="$SRC_DIR/agent/backend"
AGENT_PORT="${PORT:-9100}"

# 颜色用 $'...' 定义为真实 ESC 字节：read -rp 的提示符不解析 \033 转义，字面定义会在确认提示里打出乱码
c_g=$'\033[0;32m'; c_c=$'\033[0;36m'; c_y=$'\033[0;33m'; c_r=$'\033[0;31m'; c_dim=$'\033[2m'; c_b=$'\033[1m'; c_off=$'\033[0m'
info()  { echo -e "  ${c_g}➜${c_off} $*"; }
ok()    { echo -e "  ${c_g}✔${c_off} $*"; }
warn()  { echo -e "  ${c_y}⚠${c_off} $*"; }
fail()  { echo -e "  ${c_r}✘ $*${c_off}" >&2; exit 1; }
step()  { echo -e "\n${c_c}${c_b}── $* ──${c_off}"; }

banner() {
  echo -e "${c_c}"
  echo -e "  ____   ___  ____   ___  _   _ _____ "
  echo -e " |  _ \\ / _ \\|  _ \\ / _ \\| \\ | | ____|"
  echo -e " | |_) | | | | |_) | | | |  \\| |  _|  "
  echo -e " |  __/| |_| |  __/| |_| | |\\  | |___ "
  echo -e " |_|    \\___/|_|    \\___/|_| \\_|_____|"
  echo -e "${c_off}${c_dim}        云面板总安装器 · 主控 / 被控${c_off}"
  echo -e "${c_dim}  ─────────────────────────────────────────────────────${c_off}"
}

confirm() { read -rp "  ${c_y}$1 [y/N]: ${c_off}" a; [[ "$a" =~ ^[Yy]$ ]]; }

# ============================================================
#  环境检测（与被控安装脚本同一套）
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
      apt-get update -y 2>&1 | tail -1 || true
      apt-get install -y -o DPkg::Lock::Timeout=120 "$@"
      ;;
    dnf) dnf install -y "$@" ;;
    yum) yum install -y "$@" ;;
  esac
}

is_cn() {
  TZ_VAL=$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo "")
  case "$TZ_VAL" in
    Asia/Shanghai|Asia/Chongqing|Asia/Harbin|Asia/Urumqi|Asia/Hong_Kong|Asia/Macau|Asia/Taipei) return 0 ;;
  esac
  command -v curl >/dev/null 2>&1 || return 1
  [ "$(curl -s --max-time 3 https://ipinfo.io/country 2>/dev/null | tr -d '[:space:]')" = "CN" ]
}

open_port() {  # 防火墙放行端口（有防火墙才操作）
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$1/tcp" >/dev/null 2>&1 && ok "ufw 已放行 $1"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$1/tcp" >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1 && ok "firewalld 已放行 $1"
  fi
}

remove_master_container() {  # 删除主控 Docker 容器；删不掉必须中止——
  # 若带着存活容器继续删数据卷/仓库，容器会握住幽灵挂载点（SQLite unable to
  # open database file），且 up -d 会复用旧容器不重建，故障极难排查
  command -v docker >/dev/null 2>&1 || return 0
  docker rm -f ppanel-master >/dev/null 2>&1 || true
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "ppanel-master"; then
    fail "ppanel-master 容器删除失败，已中止（目录未动）。请手动执行 docker rm -f ppanel-master 后重试"
  fi
  return 0
}

# ============================================================
#  代码获取（主控/被控共用一个仓库：/opt/ppanel）
# ============================================================
pick_repo_url() {  # 国内→gitee；海外（含香港）→github；探测失败默认 gitee（gitee 全球可达）
  if [ -n "$REPO_URL" ]; then GEO_SRC="指定"; REPO_URL_ALT="$REPO_URL_GITEE"; return; fi
  local tz="" c=""
  tz=$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo "")
  case "$tz" in
    Asia/Shanghai|Asia/Chongqing|Asia/Harbin|Asia/Urumqi)
      REPO_URL="$REPO_URL_GITEE"; GEO_SRC="gitee(国内)"; REPO_URL_ALT="$REPO_URL_GITHUB"; return ;;
    Asia/Hong_Kong|Asia/Macau|Asia/Taipei)  # 港澳台按海外走 github
      REPO_URL="$REPO_URL_GITHUB"; GEO_SRC="github(海外)"; REPO_URL_ALT="$REPO_URL_GITEE"; return ;;
  esac
  if command -v curl >/dev/null 2>&1; then
    c=$(curl -fsS --max-time 5 https://ipinfo.io/country 2>/dev/null | tr -d '[:space:]' || true)
  fi
  case "$c" in
    CN)          REPO_URL="$REPO_URL_GITEE";  GEO_SRC="gitee(国内)" ;;
    ""|CN*)      REPO_URL="$REPO_URL_GITEE";  GEO_SRC="gitee(默认)" ;;  # 探测失败默认 gitee
    *)           REPO_URL="$REPO_URL_GITHUB"; GEO_SRC="github(海外)" ;;
  esac
  case "$GEO_SRC" in
    gitee*) REPO_URL_ALT="$REPO_URL_GITHUB" ;;
    *)      REPO_URL_ALT="$REPO_URL_GITEE" ;;
  esac
  return 0
}

ensure_repo() {  # $1=安装后校验的文件路径（缺省主控 main.py）
  local CHECK="${1:-$MASTER_BACKEND/main.py}"
  step "拉取代码"
  if [ -d "$SRC_DIR/.git" ]; then
    MODE_UPGRADE=1
    info "检测到已有代码（$SRC_DIR），更新..."
    # 预清理：恢复上次安装裁剪/本地改动，清残留 merge 状态，避免二次安装 pull 失败
    git -C "$SRC_DIR" merge --abort 2>/dev/null || true
    git -C "$SRC_DIR" checkout -- . 2>/dev/null || true
    if git -C "$SRC_DIR" pull --ff-only 2>&1 | tee /tmp/ppanel-git.log; then
      ok "代码已是最新"
    else
      warn "git pull 失败，硬对齐远程分支..."
      BR=$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo main)
      if git -C "$SRC_DIR" fetch origin > /dev/null 2>&1 \
         && git -C "$SRC_DIR" reset --hard "origin/$BR" > /dev/null 2>&1; then
        ok "已对齐远程分支 $BR"
      else
        warn "无法自动更新，沿用现有代码继续"
      fi
    fi
  else
    MODE_UPGRADE=0
    pick_repo_url
    # 目录已存在但不是 git 仓库（如主控 Docker 快路径只留下 master/.env）：
    # 先把现有内容搬到临时目录，克隆后搬回（cp -an 不覆盖仓库文件），
    # 否则 git clone 会因目录非空失败，走 rm -rf 兜底时连带删掉 master/.env
    KEEP_SRC=""
    if [ -d "$SRC_DIR" ] && [ -n "$(ls -A "$SRC_DIR" 2>/dev/null)" ]; then
      KEEP_SRC=$(mktemp -d /tmp/ppanel-keep.XXXXXX)
      mv "$SRC_DIR"/* "$KEEP_SRC/" 2>/dev/null || true
      mv "$SRC_DIR"/.??* "$KEEP_SRC/" 2>/dev/null || true
      info "已临时保全 $SRC_DIR 既有内容（含配置）"
    fi
    info "代码源：$GEO_SRC -> $REPO_URL"
    if git clone --progress --depth 1 "$REPO_URL" "$SRC_DIR" 2>&1 | tee /tmp/ppanel-git-clone.log; then
      :
    else
      warn "首选源克隆失败，改用备用源：$REPO_URL_ALT"
      [ -d "$SRC_DIR/.git" ] || rm -rf "$SRC_DIR"   # 只清理克隆残留
      git clone --progress --depth 1 "$REPO_URL_ALT" "$SRC_DIR" 2>&1 | tee /tmp/ppanel-git-clone.log \
        || fail "克隆失败，检查网络或用 REPO_URL= 指定仓库地址"
    fi
    if [ -n "$KEEP_SRC" ]; then
      cp -an "$KEEP_SRC/." "$SRC_DIR/" 2>/dev/null || true
      rm -rf "$KEEP_SRC"
      ok "既有配置已恢复（.env 等用户数据保留）"
    fi
  fi
  [ -f "$CHECK" ] || fail "仓库结构异常：找不到 $CHECK"
  ok "代码就绪"
}

# ============================================================
#  主控：服务器直装
# ============================================================
master_install_base() {
  step "1/6 基础依赖（Python / Node）"
  pkg_install curl git ca-certificates
  command -v python3 >/dev/null 2>&1 || pkg_install python3 python3-venv python3-pip
  # Ubuntu/Debian 自带 python3 但常缺 venv/ensurepip（python3-venv 包），缺它建不了虚拟环境
  if [ "$PKG" = "apt" ] && ! python3 -m ensurepip --version >/dev/null 2>&1; then
    info "补装 python3-venv（缺 ensurepip，虚拟环境创建必需）..."
    pkg_install python3-venv
  fi
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    ok "Python $(python3 -V 2>&1 | awk '{print $2}')"
  elif python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    warn "系统 Python $(python3 -V 2>&1 | awk '{print $2}') 低于 3.11，稍后由 uv 自动配置 Python 3.12"
  else
    fail "需要 Python >= 3.11（可由 uv 自动下载），当前 $(python3 -V 2>&1)"
  fi
  master_install_node
}

master_install_node() {  # 前端构建需要 Node >= 18
  NODE_OK=0
  if command -v node >/dev/null 2>&1; then
    NM=$(node -v | sed 's/^v//' | cut -d. -f1)
    [ "$NM" -ge 18 ] 2>/dev/null && NODE_OK=1
  fi
  if [ "$NODE_OK" != 1 ]; then
    info "安装 Node.js（前端构建用）..."
    pkg_install nodejs npm 2>/dev/null || true
    if command -v node >/dev/null 2>&1; then
      NM=$(node -v | sed 's/^v//' | cut -d. -f1)
      [ "$NM" -ge 18 ] 2>/dev/null && NODE_OK=1
    fi
    if [ "$NODE_OK" != 1 ]; then
      info "发行版 Node 过旧，尝试 NodeSource 20.x..."
      if command -v apt-get >/dev/null 2>&1; then
        curl -fsSL --max-time 60 https://deb.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 \
          && apt-get install -y nodejs >/dev/null 2>&1 || true
      else
        curl -fsSL --max-time 60 https://rpm.nodesource.com/setup_20.x | bash - >/dev/null 2>&1 \
          && $PKG install nodejs >/dev/null 2>&1 || true
      fi
    fi
  fi
  command -v node >/dev/null 2>&1 || fail "Node.js 安装失败，请手动安装 Node >= 18 后重试"
  [ "$(node -v | sed 's/^v//' | cut -d. -f1)" -ge 18 ] 2>/dev/null \
    || fail "Node 版本过低（$(node -v)），需要 >= 18"
  ok "Node $(node -v)"
}

bootstrap_uv() {  # $1=backend 目录；成功后 UV_BIN 指向可用 uv，失败为空
  UV_BIN=""
  [ -x "$HOME/.local/bin/uv" ] && UV_BIN="$HOME/.local/bin/uv"
  [ -z "$UV_BIN" ] && [ -x "$1/.venv/bin/uv" ] && UV_BIN="$1/.venv/bin/uv"
  if [ -z "$UV_BIN" ]; then
    info "安装 uv 包管理器（清华 PyPI 镜像）..."
    # uv 创建的 venv 不带 pip：缺 pip 先重建，保证 bootstrap 可用
    [ -x "$1/.venv/bin/pip" ] || python3 -m venv --clear "$1/.venv"
    "$1/.venv/bin/pip" install uv -i "$PYPI_MIRROR" && UV_BIN="$1/.venv/bin/uv" || true
    if [ -z "$UV_BIN" ]; then
      info "pip 安装 uv 失败，尝试官方安装脚本..."
      curl -LsSf --max-time 60 https://astral.sh/uv/install.sh | sh || true
      [ -x "$HOME/.local/bin/uv" ] && UV_BIN="$HOME/.local/bin/uv"
    fi
  fi
}

master_deps() {
  step "2/6 后端依赖（uv 加速）"
  cd "$MASTER_BACKEND"
  export UV_DEFAULT_INDEX="$PYPI_MIRROR" UV_INDEX_URL="$PYPI_MIRROR"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    export UV_PYTHON_PREFERENCE=only-system UV_PYTHON="$(command -v python3)"
  else
    # 系统 Python 过旧：允许 uv 下载托管版 3.12
    unset UV_PYTHON_PREFERENCE
    export UV_PYTHON=3.12
  fi
  bootstrap_uv "$MASTER_BACKEND"
  UV_OK=0
  if [ -n "$UV_BIN" ]; then
    info "uv sync 安装后端依赖..."
    if "$UV_BIN" sync --inexact --no-dev --no-install-project 2>&1 | tee /tmp/ppanel-uv-master.log; then
      UV_OK=1; git checkout -- uv.lock 2>/dev/null || true
    fi
  fi
  if [ "$UV_OK" != 1 ]; then
    warn "uv 不可用，退回 pip 安装..."
    [ -x "$MASTER_BACKEND/.venv/bin/pip" ] || python3 -m venv --clear "$MASTER_BACKEND/.venv"
    "$MASTER_BACKEND/.venv/bin/pip" install --upgrade pip -i "$PYPI_MIRROR"
    "$MASTER_BACKEND/.venv/bin/pip" install . -i "$PYPI_MIRROR"
  fi
  "$MASTER_BACKEND/.venv/bin/python" -c "import fastapi, uvicorn, sqlalchemy" \
    || fail "后端依赖校验失败"
  ok "后端依赖就绪"
}

master_web() {
  step "3/6 构建前端"
  cd "$MASTER_FRONTEND"
  if [ -f dist/index.html ] && [ ! -f src/App.vue ]; then true; fi
  info "npm 安装依赖（${NPM_REGISTRY}）..."
  npm ci --registry="$NPM_REGISTRY" >/tmp/ppanel-npm.log 2>&1 \
    || npm install --registry="$NPM_REGISTRY" >>/tmp/ppanel-npm.log 2>&1 \
    || { tail -8 /tmp/ppanel-npm.log; fail "npm 依赖安装失败"; }
  info "npm run build..."
  npm run build >>/tmp/ppanel-npm.log 2>&1 || { tail -8 /tmp/ppanel-npm.log; fail "前端构建失败"; }
  [ -f dist/index.html ] || fail "构建产物缺失（dist/index.html）"
  ok "前端构建完成（dist/）"
}

master_env() {  # 生成 master/backend/.env；全新安装时打印随机管理员密码
  step "4/6 配置文件"
  NEW_INSTALL=0
  if [ ! -f "$MASTER_BACKEND/.env" ]; then
    NEW_INSTALL=1
    JWT_SECRET="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    ADMIN_PASS="$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)"
    mkdir -p "$MASTER_BACKEND/data"
    cat > "$MASTER_BACKEND/.env" <<EOF
# PPanel 主控配置（由 install.sh 生成）
MASTER_HOST=0.0.0.0
MASTER_PORT=$MASTER_PORT

# 管理员账号（首次启动自动创建）
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASS

# JWT 密钥（已随机生成）
JWT_SECRET=$JWT_SECRET
EOF
    ok "配置文件已生成"
  else
    ok "沿用现有配置文件（$MASTER_BACKEND/.env）"
  fi
}

master_service() {
  step "5/6 注册服务并启动"
  MPORT=$(grep -E '^MASTER_PORT=' "$MASTER_BACKEND/.env" | cut -d= -f2)
  [ -n "$MPORT" ] || MPORT="$MASTER_PORT"
  cat > "/etc/systemd/system/$MASTER_SERVICE.service" <<EOF
[Unit]
Description=PPanel Master (control panel)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$MASTER_BACKEND
ExecStart=$MASTER_BACKEND/.venv/bin/uvicorn main:app --host 0.0.0.0 --port $MPORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$MASTER_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$MASTER_SERVICE"
  sleep 3
  systemctl is-active --quiet "$MASTER_SERVICE" || fail "服务启动失败：journalctl -u $MASTER_SERVICE -n 50"
  open_port "$MPORT"
}

master_summary() {
  IP=$(hostname -I 2>/dev/null | awk '{print $1}'); [ -n "$IP" ] || IP="<服务器IP>"
  MPORT=$(grep -E '^MASTER_PORT=' "$MASTER_BACKEND/.env" | cut -d= -f2); [ -n "$MPORT" ] || MPORT="$MASTER_PORT"
  echo ""
  echo -e "  ${c_g}${c_b}━━━━━━━━━━━━━ 主控安装完成 ━━━━━━━━━━━━━${c_off}"
  echo -e "  面板地址  ${c_b}http://$IP:$MPORT${c_off}"
  if [ "${NEW_INSTALL:-0}" = 1 ]; then
    echo -e "  管理员    ${c_b}admin${c_off} / ${c_b}$(grep -E '^ADMIN_PASSWORD=' "$MASTER_BACKEND/.env" | cut -d= -f2)${c_off}"
    echo -e "  ${c_y}⚠ 密码只显示这一次，请立即登录并修改${c_off}"
  else
    echo -e "  管理员    沿用既有账号（密码见 $MASTER_BACKEND/.env）"
  fi
  echo -e "  ${c_dim}─────────────────────────────────────────────${c_off}"
  echo -e "  下一步    登录后台 → 节点管理 → 添加被控节点"
  echo -e "  常用      systemctl restart $MASTER_SERVICE · journalctl -u $MASTER_SERVICE -f"
  echo -e "  提醒      云服务器请在安全组放行 TCP $MPORT"
  echo -e "  ${c_g}${c_b}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_off}"
}

prune_for_master() {  # 装完主控后移除仓库里用不到的被控/安装器代码（同机双装时保留）
  if systemctl list-unit-files 2>/dev/null | awk '{print $1}' | grep -qx "ppanel-agent.service" \
     || { command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "ppanel-agent"; }; then
    warn "检测到本机装有独立面板（被控），保留完整仓库代码"
    return 0
  fi
  [ -d "$SRC_DIR/agent" ] && rm -rf "$SRC_DIR/agent" && ok "已移除被控代码（仅保留主控面板）"
  [ -d "$SRC_DIR/installer" ] && rm -rf "$SRC_DIR/installer" && ok "已移除安装器目录"
}

action_master() {
  detect_env
  master_install_base
  ensure_repo "$MASTER_BACKEND/main.py"
  master_deps
  master_web
  master_env
  master_service
  prune_for_master
  master_summary
}

# ============================================================
#  Docker 安装（国内在线多源 → 离线静态兜底）
# ============================================================
docker_ce_repo_install() {  # 国内 docker-ce 软件源：阿里云 → 清华，apt/dnf/yum 通吃
  step "使用 docker-ce 国内软件源安装"
  case "$PKG" in
    apt)
      . /etc/os-release
      ID_LC=$(echo "${ID:-debian}" | tr '[:upper:]' '[:lower:]')
      # Ubuntu 用 ubuntu 源；debian/deepin/UOS 等 Debian 系用 debian 源
      case "$ID_LC" in
        ubuntu) FAMILY=ubuntu; CODENAMES=("${VERSION_CODENAME:-jammy}") ;;
        *)      FAMILY=debian;  CODENAMES=("${VERSION_CODENAME:-bookworm}")
                [ "$ID_LC" != "debian" ] && CODENAMES+=("bookworm") ;;  # deepin 等：codename 不在官方池时回退
      esac
      for MIRROR in mirrors.aliyun.com mirrors.tuna.tsinghua.edu.cn; do
        for CN in "${CODENAMES[@]}"; do
          info "尝试源：$MIRROR（$FAMILY/$CN）..."
          curl -fsSL --max-time 20 "https://$MIRROR/docker-ce/linux/$FAMILY/gpg" 2>/dev/null \
            | gpg --dearmor --yes -o /usr/share/keyrings/ppanel-docker.gpg 2>/dev/null || continue
          echo "deb [signed-by=/usr/share/keyrings/ppanel-docker.gpg] https://$MIRROR/docker-ce/linux/$FAMILY $CN stable" \
            > /etc/apt/sources.list.d/ppanel-docker.list
          apt-get update -qq 2>/dev/null || continue
          apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin 2>/dev/null \
            && return 0
        done
      done
      ;;
    *)
      for MIRROR in mirrors.aliyun.com mirrors.tuna.tsinghua.edu.cn; do
        info "尝试源：$MIRROR ..."
        cat > /etc/yum.repos.d/ppanel-docker.repo <<EOF
[ppanel-docker-stable]
name=Docker CE Stable
baseurl=https://$MIRROR/docker-ce/linux/centos/\$releasever/\$basearch/stable
enabled=1
gpgcheck=1
gpgkey=https://$MIRROR/docker-ce/linux/centos/gpg
EOF
        $PKG makecache 2>/dev/null || true
        $PKG install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin 2>/dev/null \
          && return 0
        rm -f /etc/yum.repos.d/ppanel-docker.repo
      done
      ;;
  esac
  return 1
}

docker_offline_install() {  # 离线兜底：优先本地离线包，无包则多源下载静态二进制
  step "Docker 离线安装（静态二进制）"
  ARCH=$(uname -m)
  case "$ARCH" in
    x86_64)  DA=amd64 ;;
    aarch64) DA=aarch64 ;;
    *) fail "不支持的架构：$ARCH（可手动安装 Docker）" ;;
  esac
  DVER="27.5.1"
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

  # ① 本地离线包优先：脚本同目录 / /opt/ppanel / /tmp / /root
  LOCAL_TGZ=""
  for cand in "$SCRIPT_DIR/docker-$DVER.tgz" "$SCRIPT_DIR/docker-"*.tgz "$SCRIPT_DIR/docker.tgz" \
              /opt/ppanel/docker-$DVER.tgz /opt/ppanel/docker-*.tgz /opt/ppanel/docker.tgz \
              /tmp/docker-$DVER.tgz /tmp/docker-*.tgz /tmp/docker.tgz \
              /root/docker-$DVER.tgz /root/docker-*.tgz /root/docker.tgz; do
    for f in $cand; do
      if [ -s "$f" ]; then LOCAL_TGZ="$f"; break 2; fi
    done
  done

  if [ -n "$LOCAL_TGZ" ]; then
    ok "使用本地离线包：$LOCAL_TGZ"
    cp -f "$LOCAL_TGZ" /tmp/ppanel-docker.tgz
  else
  # ② 无本地包 → 多源在线下载（Release 附件 / 官方直链 / 加速代理）
    warn "未找到本地离线包（可预先把 docker-27.5.1.tgz 放到脚本同目录 / /opt/ppanel / /tmp）"
    BASE="https://download.docker.com/linux/static/stable/$DA/docker-$DVER.tgz"
    # 主源为 Release 附件（72MB 离线包已移出 git 树，克隆提速）；官方直链与代理为兜底
    REL_GITEE="https://gitee.com/zhuxiaohuaqn/ppanel/releases/download/v1.0/docker-$DVER.tgz"
    REL_GITHUB="https://github.com/2362400196/ppanel/releases/download/v1.0/docker-$DVER.tgz"
    DL_OK=0
    for u in "$REL_GITEE" "$BASE" "https://ghfast.top/$BASE" "https://gh-proxy.com/$BASE" "$REL_GITHUB"; do
      info "下载离线包：$u"
      if curl -fL --max-time 300 --retry 1 -o /tmp/ppanel-docker.tgz "$u" 2>/dev/null && [ -s /tmp/ppanel-docker.tgz ]; then
        DL_OK=1; break
      fi
    done
    [ "$DL_OK" = 1 ] || fail "离线包下载失败（多源均不可达），请手动下载 docker-$DVER.tgz 放到脚本同目录后重跑"
  fi

  info "解压并安装..."
  tar -xzf /tmp/ppanel-docker.tgz -C /tmp
  cp /tmp/docker-*/* /usr/bin/ 2>/dev/null || true
  rm -rf /tmp/ppanel-docker.tgz /tmp/docker-*/
  cat > /etc/systemd/system/docker.service <<'UNIT'
[Unit]
Description=Docker Application Container Engine
After=network-online.target
Wants=network-online.target

[Service]
Type=notify
ExecStart=/usr/bin/dockerd
ExecReload=/bin/kill -s HUP $MAINPID
Restart=on-failure
RestartSec=5
LimitNOFILE=1048576
Delegate=yes
KillMode=process

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  systemctl enable --now docker >/dev/null 2>&1 || true
  sleep 2
  docker info >/dev/null 2>&1 || fail "离线安装后 Docker 未运行：journalctl -u docker -n 30"
  ok "Docker 离线安装完成：$(docker --version | sed 's/,.*//')"
}

docker_online_cn() {  # 国内在线：阿里云一键脚本一次 → docker-ce 国内源
  info "尝试阿里云一键脚本（get.docker.com --mirror Aliyun）..."
  curl -fsSL --max-time 40 https://get.docker.com | sh -s -- --mirror Aliyun 2>&1 | tee /tmp/ppanel-docker-install.log || true
  systemctl enable --now docker >/dev/null 2>&1 || true
  if docker info >/dev/null 2>&1; then ok "Docker 安装成功（阿里云镜像脚本）"; return 0; fi
  docker_ce_repo_install || return 1
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1
}

install_docker() {
  step "检查 Docker"
  if command -v docker >/dev/null 2>&1; then
    ok "Docker 已存在：$(docker --version | sed 's/,.*//')"
  else
    if is_cn; then
      info "检测到国内网络..."
      docker_online_cn || { warn "在线安装失败，转离线安装..."; docker_offline_install; }
    else
      info "安装 Docker（官方脚本，实时日志如下）..."
      curl -fsSL --max-time 120 https://get.docker.com | sh 2>&1 | tee /tmp/ppanel-docker-install.log || true
      systemctl enable --now docker >/dev/null 2>&1 || true
      docker info >/dev/null 2>&1 || { warn "官方脚本失败，转离线安装..."; docker_offline_install; }
    fi
  fi
  systemctl enable --now docker >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1 || fail "Docker 服务未运行（journalctl -u docker -n 30）"
  ok "Docker 服务运行中：$(docker --version | sed 's/,.*//')"
}

docker_install_compose() {  # compose 插件缺失时补装（master-docker 需要）
  docker compose version >/dev/null 2>&1 && return 0
  command -v docker-compose >/dev/null 2>&1 && return 0
  info "补装 Docker Compose 插件..."
  CV="v2.32.4"
  ARCH=$(uname -m); case "$ARCH" in aarch64) CA=aarch64 ;; *) CA=x86_64 ;; esac
  BASE="https://github.com/docker/compose/releases/download/$CV/docker-compose-linux-$CA"
  mkdir -p /usr/local/lib/docker/cli-plugins
  for u in "$BASE" "https://ghfast.top/$BASE" "https://gh-proxy.com/$BASE"; do
    if curl -fL --max-time 240 -o /usr/local/lib/docker/cli-plugins/docker-compose "$u" 2>/dev/null \
       && [ -s /usr/local/lib/docker/cli-plugins/docker-compose ]; then
      chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
      docker compose version >/dev/null 2>&1 && { ok "Compose 插件就绪"; return 0; }
    fi
  done
  return 1
}

master_docker_migrate_data() {  # 旧版数据目录在仓库树内（backend/data），迁移到仓库外的 /opt/ppanel/master-data
  local OLD="$MASTER_BACKEND/data" NEW="/opt/ppanel/master-data"
  mkdir -p "$NEW"
  if [ -d "$OLD" ] && [ -n "$(ls -A "$OLD" 2>/dev/null)" ]; then
    cp -an "$OLD/." "$NEW/" 2>/dev/null || true
    info "已迁移旧数据目录 -> $NEW（仓库树外，重装/裁剪代码不再连带删数据）"
  fi
}

master_docker_env() {  # master/.env 供 docker-compose 变量替换
  mkdir -p "$MASTER_DIR"
  NEW_INSTALL=0
  if [ ! -f "$MASTER_DIR/.env" ]; then
    NEW_INSTALL=1
    JWT_SECRET="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    ADMIN_PASS="$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)"
    cat > "$MASTER_DIR/.env" <<EOF
# PPanel 主控 Docker 配置（compose 变量）
MASTER_PORT=$MASTER_PORT
ADMIN_USERNAME=admin
ADMIN_PASSWORD=$ADMIN_PASS
JWT_SECRET=$JWT_SECRET
EOF
    ok "Docker 配置已生成（master/.env）"
  else
    ok "沿用现有配置（master/.env）"
  fi
}

master_docker_summary() {
  IP=$(hostname -I 2>/dev/null | awk '{print $1}'); [ -n "$IP" ] || IP="<服务器IP>"
  MPORT=$(grep -E '^MASTER_PORT=' "$MASTER_DIR/.env" | cut -d= -f2); [ -n "$MPORT" ] || MPORT="$MASTER_PORT"
  echo ""
  echo -e "  ${c_g}${c_b}━━━━━━━━━━━ 主控 Docker 安装完成 ━━━━━━━━━━━${c_off}"
  echo -e "  面板地址  ${c_b}http://$IP:$MPORT${c_off}"
  if [ "${NEW_INSTALL:-0}" = 1 ]; then
    echo -e "  管理员    ${c_b}admin${c_off} / ${c_b}$(grep -E '^ADMIN_PASSWORD=' "$MASTER_DIR/.env" | cut -d= -f2)${c_off}"
    echo -e "  ${c_y}⚠ 密码只显示这一次，请立即登录并修改${c_off}"
  else
    echo -e "  管理员    沿用既有账号（密码见 $MASTER_DIR/.env）"
  fi
  echo -e "  ${c_dim}─────────────────────────────────────────────${c_off}"
  echo -e "  数据      宿主机 /opt/ppanel/master-data（SQLite 与附件，仓库树外）"
  echo -e "  常用      cd $MASTER_DIR && $DOCKER_COMPOSE restart · logs -f"
  echo -e "  升级      重跑安装脚本菜单 [1]（自动拉取最新镜像；MASTER_BUILD=1 强制本地构建）"
  echo -e "  提醒      云服务器请在安全组放行 TCP $MPORT"
  echo -e "  ${c_g}${c_b}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_off}"
}

ensure_swap_for_build() {  # 小内存机构建保险：可用内存+swap 不足 2.5G 时临时加 2G swap（vite 构建吃内存）
  local mem_avail swap_total sf
  mem_avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
  swap_total=$(awk '/^SwapTotal:/{print int($2/1024)}' /proc/meminfo)
  [ "$((mem_avail + swap_total))" -ge 2560 ] && return 0
  [ "$swap_total" -ge 1024 ] && return 0
  sf=/swapfile
  if [ -f /swapfile ]; then
    swapon /swapfile 2>/dev/null || true
    return 0
  fi
  sf=/swap-ppanel
  info "内存偏小（可用约 ${mem_avail}MB），创建 2G swap 保障前端构建..."
  dd if=/dev/zero of="$sf" bs=1M count=2048 status=none \
    && chmod 600 "$sf" && mkswap -q "$sf" >/dev/null && swapon "$sf" 2>/dev/null \
    && ok "swap 已启用（临时，重启不保留）" \
    || warn "swap 创建失败，继续尝试构建"
}

ensure_master_image() {  # 优先拉预构建镜像（加速站轮换→直连），拉到后对齐 compose 名；失败由调用方走本地构建兜底
  local MASTER_IMAGE="2362400196/ppanel-master:latest" m src
  if docker image inspect ppanel-master:latest >/dev/null 2>&1; then
    ok "主控镜像已存在：ppanel-master:latest"
    return 0
  fi
  for m in "" docker.1ms.run docker.m.daocloud.io dockerpull.org hub.rat.dev; do
    src="${m:+$m/}$MASTER_IMAGE"
    info "拉取主控镜像：$src ..."
    if timeout 900 docker pull "$src"; then
      [ -n "$m" ] && docker tag "$src" "$MASTER_IMAGE"
      docker tag "$MASTER_IMAGE" ppanel-master:latest
      ok "镜像就绪（预构建，跳过前端编译）"
      return 0
    fi
  done
  return 1
}

ensure_base_images() {  # 预拉基础镜像：国内走加速站限时拉取，避免 docker build 直连 Docker Hub 无限挂起
  if docker image inspect node:20-slim >/dev/null 2>&1 \
     && docker image inspect python:3.12-slim >/dev/null 2>&1; then
    ok "基础镜像已存在，跳过预拉取"
    return 0
  fi
  if ! is_cn; then
    info "拉取基础镜像（Docker Hub）..."
    docker pull node:20-slim || true
    docker pull python:3.12-slim || true
    return 0
  fi
  step "预拉取基础镜像（国内加速站）"
  for img in node:20-slim python:3.12-slim; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      ok "$img 已存在"
      continue
    fi
    PULL_OK=0
    # 官方镜像在加速站的路径为 <mirror>/library/<name>；单源限时 240s，防止挂死
    for m in docker.1ms.run docker.m.daocloud.io dockerpull.org hub.rat.dev; do
      info "拉取 $img <- $m ..."
      if timeout 240 docker pull "$m/library/$img"; then
        docker tag "$m/library/$img" "$img"
        PULL_OK=1
        break
      fi
      warn "$m 不可用，换下一个源..."
    done
    if [ "$PULL_OK" != 1 ]; then
      warn "加速站均失败，直连 Docker Hub（可能较慢）..."
      timeout 600 docker pull "$img" || fail "基础镜像 $img 拉取失败，请检查网络后重跑"
    fi
    ok "$img 就绪"
  done
}

fetch_master_compose() {  # 快路径：只单文件下载 compose.yml（KB 级），避免为拉镜像克隆整仓（含 72MB 历史）
  [ -s "$MASTER_DIR/docker-compose.yml" ] && { ok "编排文件已存在"; return 0; }
  mkdir -p "$MASTER_DIR"
  step "获取编排文件"
  local u
  for u in "https://gitee.com/zhuxiaohuaqn/ppanel/raw/master/master/docker-compose.yml" \
           "https://cdn.jsdelivr.net/gh/2362400196/ppanel@master/master/docker-compose.yml" \
           "https://raw.githubusercontent.com/2362400196/ppanel/master/master/docker-compose.yml"; do
    info "下载 docker-compose.yml <- $u"
    if curl -fsSL --max-time 30 "$u" -o "$MASTER_DIR/docker-compose.yml" 2>/dev/null \
       && grep -q 'services:' "$MASTER_DIR/docker-compose.yml"; then
      ok "编排文件就绪"
      return 0
    fi
  done
  # gitee raw 风控（451）时的 API base64 兜底
  info "尝试 gitee API 兜底..."
  if curl -fsSL --max-time 30 "https://gitee.com/api/v5/repos/zhuxiaohuaqn/ppanel/contents/master/docker-compose.yml?ref=master" 2>/dev/null \
     | sed 's/.*"content":"\([^"]*\)".*/\1/' | base64 -di > "$MASTER_DIR/docker-compose.yml" 2>/dev/null \
     && grep -q 'services:' "$MASTER_DIR/docker-compose.yml" 2>/dev/null; then
    ok "编排文件就绪（gitee API）"
    return 0
  fi
  rm -f "$MASTER_DIR/docker-compose.yml"
  return 1
}

action_master_docker() {
  detect_env
  install_docker
  docker_install_compose || fail "Docker Compose 安装失败，请手动安装 docker-compose 后重跑"
  DOCKER_COMPOSE="docker compose"
  master_docker_migrate_data
  step "镜像"
  if [ "${MASTER_BUILD:-0}" = 1 ]; then
    ensure_repo "$MASTER_BACKEND/main.py"
    ensure_base_images
    ensure_swap_for_build
    master_docker_env
    cd "$MASTER_DIR"
    info "已指定 MASTER_BUILD=1，本地构建并启动..."
    $DOCKER_COMPOSE up -d --build || fail "构建/启动失败：$DOCKER_COMPOSE logs"
  elif ensure_master_image; then
    # 快路径：镜像就绪，只需 compose.yml（已有仓库则 pull 更新，否则单文件下载）
    if [ -d "$SRC_DIR/.git" ]; then
      ensure_repo "$MASTER_BACKEND/main.py"
    else
      fetch_master_compose || { warn "编排文件获取失败，回退克隆整库..."; ensure_repo "$MASTER_BACKEND/main.py"; }
    fi
    master_docker_env
    cd "$MASTER_DIR"
    info "使用预构建镜像，直接启动..."
    $DOCKER_COMPOSE up -d || fail "启动失败：$DOCKER_COMPOSE logs"
  else
    ensure_repo "$MASTER_BACKEND/main.py"
    ensure_base_images
    ensure_swap_for_build
    master_docker_env
    cd "$MASTER_DIR"
    info "预构建镜像不可用，本地构建（首次需编译前端，约 1-3 分钟）..."
    $DOCKER_COMPOSE up -d --build || fail "构建/启动失败：$DOCKER_COMPOSE logs"
  fi
  sleep 3
  # 未就绪只警告不退出：无论何种状态，安装摘要（地址/账号）都必须输出
  if docker ps --filter "name=ppanel-master" --filter "status=running" 2>/dev/null | grep -q ppanel-master; then
    ok "容器运行中"
  else
    warn "容器未处于 running 状态（可能仍在启动）：docker logs ppanel-master 查看"
  fi
  open_port "$MASTER_PORT"
  prune_for_master
  master_docker_summary
}

master_wipe() {  # 主控全新重装清理：systemd 服务 + Docker 容器 + 代码/配置/数据库
  echo -e "  ${c_y}将删除：$SRC_DIR（代码+配置+数据库）、$MASTER_SERVICE 服务、ppanel-master 容器${c_off}"
  confirm "确认完全清除并重新安装主控？" || { warn "已取消"; return 1; }
  systemctl disable --now "$MASTER_SERVICE" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$MASTER_SERVICE.service"; systemctl daemon-reload
  remove_master_container
  # 同机装有被控时保全其运行环境（.venv/.env 在 agent/ 内，删整个仓库会连带清掉，
  # 导致被控 203/EXEC 崩循环）：先搬走 agent/，清完仓库再搬回并重启
  local AGENT_KEEP=0
  if [ -d "$AGENT_APP" ]; then
    AGENT_KEEP=1
    rm -rf /tmp/ppanel-agent-keep   # 上次中断可能残留，防 mv 嵌套进旧目录
    mv "$SRC_DIR/agent" /tmp/ppanel-agent-keep
    info "检测到本机被控，已临时保全 agent 环境..."
  fi
  rm -rf "$SRC_DIR" /opt/ppanel/master-data /tmp/ppanel-uv-sync.log
  if [ "$AGENT_KEEP" = 1 ]; then
    mkdir -p "$SRC_DIR"
    mv /tmp/ppanel-agent-keep "$SRC_DIR/agent"
    systemctl is-enabled --quiet "$AGENT_SERVICE" 2>/dev/null && systemctl restart "$AGENT_SERVICE" 2>/dev/null || true
    ok "被控环境已保全并恢复运行"
  fi
  ok "旧主控已清除"
  return 0
}

action_master_reinstall() {        # 全新重装主控（本机直装）
  detect_env
  master_wipe || return 0
  action_master
}

action_master_uninstall() {  # 卸载主控：停用服务/容器，删代码与配置；数据可选删除
  detect_env
  local HAS_DOCKER=0 HAS_SYS=0
  command -v docker >/dev/null 2>&1 \
    && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "ppanel-master" && HAS_DOCKER=1
  systemctl list-unit-files 2>/dev/null | awk '{print $1}' | grep -qx "$MASTER_SERVICE.service" && HAS_SYS=1
  if [ "$HAS_DOCKER" = 0 ] && [ "$HAS_SYS" = 0 ] && [ ! -d "$MASTER_DIR" ]; then
    warn "未检测到主控安装"; return 0
  fi
  echo -e "  ${c_y}将删除：$MASTER_DIR（compose/.env）${c_off}"
  [ "$HAS_SYS" = 1 ] && echo -e "  ${c_y}        systemd 服务 $MASTER_SERVICE${c_off}"
  [ "$HAS_DOCKER" = 1 ] && echo -e "  ${c_y}        Docker 容器 ppanel-master${c_off}"
  confirm "确认卸载主控？" || { warn "已取消"; return 0; }
  systemctl disable --now "$MASTER_SERVICE" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$MASTER_SERVICE.service"; systemctl daemon-reload
  remove_master_container
  # 代码目录：同机被控保全 agent/ 后清理仓库
  if [ -d "$AGENT_APP" ]; then
    rm -rf /tmp/ppanel-agent-keep
    mv "$SRC_DIR/agent" /tmp/ppanel-agent-keep
    rm -rf "$SRC_DIR"
    mkdir -p "$SRC_DIR"; mv /tmp/ppanel-agent-keep "$SRC_DIR/agent"
    systemctl is-enabled --quiet "$AGENT_SERVICE" 2>/dev/null && systemctl restart "$AGENT_SERVICE" 2>/dev/null || true
    ok "同机被控环境已保全并恢复运行"
  else
    [ -d "$SRC_DIR" ] && rm -rf "$SRC_DIR"
  fi
  if [ -d /opt/ppanel/master-data ]; then
    if confirm "同时删除主控数据 /opt/ppanel/master-data（数据库/账号/附件，不可恢复）？"; then
      rm -rf /opt/ppanel/master-data
      ok "主控数据已删除"
    else
      info "数据已保留（重装主控后自动挂回）"
    fi
  fi
  ok "主控已卸载"
}

action_master_docker_reinstall() { # 全新重装主控（Docker）
  detect_env
  master_wipe || return 0
  action_master_docker
}

# ============================================================
#  被控：独立面板安装 / 管理（原 agent/install.sh 已并入）
# ============================================================
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

agent_install_base() {
  step "1/6 安装基础依赖"
  pkg_install curl git ca-certificates
  if ! command -v python3 >/dev/null 2>&1; then
    case "$PKG" in
      apt) pkg_install python3 python3-venv python3-pip ;;
      *)   pkg_install python3 python3-pip ;;
    esac
  fi
  # Ubuntu/Debian 自带 python3 但常缺 venv/ensurepip（python3-venv 包），缺它建不了虚拟环境
  if [ "$PKG" = "apt" ] && ! python3 -m ensurepip --version >/dev/null 2>&1; then
    info "补装 python3-venv（缺 ensurepip，虚拟环境创建必需）..."
    pkg_install python3-venv
  fi
  PYV=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
    ok "Python $PYV"
  elif python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    warn "系统 Python $PYV 低于 3.11，稍后由 uv 自动配置 Python 3.12"
  else
    fail "需要 Python >= 3.11（可由 uv 自动下载），当前 $PYV"
  fi
}

install_caddy() {
  step "3/6 安装 Caddy（域名自动 HTTPS / Let's Encrypt 证书）"
  if command -v caddy >/dev/null 2>&1; then
    ok "Caddy 已存在：$(caddy version 2>/dev/null | head -1)"
  else
    if [ "$PKG" = "apt" ]; then
      info "安装 Caddy（caddy 官方仓库）..."
      apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null 2>&1 || true
      # gpg 输出文件已存在时会交互式询问 Overwrite?，无 stdin 直接失败 → 先删旧文件 + --yes 强制覆盖
      rm -f /usr/share/keyrings/caddy-stable-archive-keyring.gpg
      curl -fsSL "https://caddyserver.com/api/download-gpg" 2>/dev/null | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null \
        || curl -fsSL "https://dl.cloudsmith.io/public/caddy/stable/gpg.key" 2>/dev/null | gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null || true
      echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
        > /etc/apt/sources.list.d/caddy-stable.list 2>/dev/null || true
      apt-get update -qq >/dev/null 2>&1 || true
      if ! apt-get install -y -qq caddy >/dev/null 2>&1; then
        # 仓库不可达时退回静态二进制（GitHub 直链 / 国内加速）
        info "apt 安装失败，尝试静态二进制..."
        ARCH=$(uname -m); case "$ARCH" in x86_64) CA=amd64 ;; aarch64) CA=arm64 ;; *) CA=amd64 ;; esac
        CADDY_TGZ="https://github.com/caddyserver/caddy/releases/latest/download/caddy_${CA}.tar.gz"
        curl -fsSL --max-time 180 "$CADDY_TGZ" -o /tmp/caddy.tgz 2>/dev/null \
          || curl -fsSL --max-time 180 "https://ghfast.top/$CADDY_TGZ" -o /tmp/caddy.tgz 2>/dev/null \
          || curl -fsSL --max-time 180 "https://gh-proxy.com/$CADDY_TGZ" -o /tmp/caddy.tgz 2>/dev/null \
          || curl -fsSL --max-time 180 "https://github.moeyy.xyz/$CADDY_TGZ" -o /tmp/caddy.tgz 2>/dev/null \
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
      $PKG install caddy || { warn "Caddy 安装失败（不影响面板运行，仅「域名与 SSL」功能不可用）"; return 0; }
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

agent_deps() {
  step "5/6 安装 Python 依赖（uv 加速）"
  cd "$AGENT_APP"
  export UV_DEFAULT_INDEX="$PYPI_MIRROR"
  export UV_INDEX_URL="$PYPI_MIRROR"
  if python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    export UV_PYTHON_PREFERENCE=only-system
    export UV_PYTHON="$(command -v python3)"
  else
    # 系统 Python 过旧：允许 uv 下载托管版 3.12
    unset UV_PYTHON_PREFERENCE
    export UV_PYTHON=3.12
  fi

  bootstrap_uv "$AGENT_APP"
  UV_OK=0
  if [ -n "$UV_BIN" ]; then
    info "uv sync 安装依赖..."
    # --inexact：保留 venv 内 uv 本体；--no-install-project：平铺结构不构建项目本身
    if "$UV_BIN" sync --inexact --no-dev --no-install-project 2>&1 | tee /tmp/ppanel-uv-sync.log; then
      UV_OK=1
      git checkout -- uv.lock 2>/dev/null || true
    elif [ "$PKG" = "apt" ]; then
      warn "疑似缺编译头文件（aarch64 常见），安装工具链后重试..."
      pkg_install libffi-dev python3-dev gcc
      if "$UV_BIN" sync --inexact --no-dev --no-install-project 2>&1 | tee /tmp/ppanel-uv-sync.log; then
        UV_OK=1
        git checkout -- uv.lock 2>/dev/null || true
      fi
    fi
  fi
  if [ "$UV_OK" != 1 ]; then
    warn "uv 不可用，退回 pip 安装（较慢）..."
    [ -x "$AGENT_APP/.venv/bin/pip" ] || python3 -m venv --clear "$AGENT_APP/.venv"
    "$AGENT_APP/.venv/bin/pip" install --upgrade pip -i "$PYPI_MIRROR"
    "$AGENT_APP/.venv/bin/pip" install . -i "$PYPI_MIRROR"
  fi
  "$AGENT_APP/.venv/bin/python" -c "import fastapi, docker, uvicorn" \
    || fail "依赖校验失败（fastapi/docker/uvicorn 导入失败）"
  ok "依赖就绪（校验通过）"
}

agent_service() {
  step "6/6 注册服务并启动"
  mkdir -p /data/inst "$AGENT_APP/data"
  if [ ! -f "$AGENT_APP/.env" ]; then
    NODE_TOKEN="ppnode_$(openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    JWT_SECRET="$(openssl rand -hex 24 2>/dev/null || head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    # 管理员为内部占位（无人工登录），随机化杜绝弱口令撞库；接口均走 X-Node-Token 鉴权
    ADMIN_PASS="$(openssl rand -base64 18 2>/dev/null || head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)"
    cat > "$AGENT_APP/.env" <<EOF
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
    NODE_TOKEN=$(grep -E '^NODE_TOKEN=' "$AGENT_APP/.env" | cut -d= -f2)
    [ -n "$NODE_TOKEN" ] || fail ".env 缺少 NODE_TOKEN，请补填"
    ok "沿用现有配置文件"
  fi

  info "写入 systemd 服务（端口 $AGENT_PORT）..."
  cat > "/etc/systemd/system/$AGENT_SERVICE.service" <<EOF
[Unit]
Description=PPanel Agent (standalone panel + docker node agent)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$AGENT_APP
ExecStart=$AGENT_APP/.venv/bin/uvicorn agent_main:app --host 0.0.0.0 --port $AGENT_PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$AGENT_SERVICE" >/dev/null 2>&1 || true
  systemctl restart "$AGENT_SERVICE"
  sleep 2
  systemctl is-active --quiet "$AGENT_SERVICE" || fail "服务启动失败：journalctl -u $AGENT_SERVICE -n 50"

  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "$AGENT_PORT/tcp" >/dev/null 2>&1 && ok "ufw 已放行 $AGENT_PORT"
  elif command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd --permanent --add-port="$AGENT_PORT/tcp" >/dev/null 2>&1 && firewall-cmd --reload >/dev/null 2>&1 && ok "firewalld 已放行 $AGENT_PORT"
  fi
}

agent_summary() {
  IP=$(hostname -I 2>/dev/null | awk '{print $1}'); [ -n "$IP" ] || IP="<服务器IP>"
  echo ""
  if [ "${MODE_UPGRADE:-0}" = 1 ]; then
    echo -e "  ${c_g}${c_b}━━━━━━━━━━━━━ 升 级 完 成 ━━━━━━━━━━━━━${c_off}"
  else
    echo -e "  ${c_g}${c_b}━━━━━━━━━━━━━ 安 装 完 成 ━━━━━━━━━━━━━${c_off}"
  fi
  echo -e "  独立面板  ${c_b}http://$IP:$AGENT_PORT/panel${c_off}"
  echo -e "  说明      仅供主控与 API 调用（X-Node-Token 鉴权），无需登录"
  echo -e "  节点Token ${c_b}$NODE_TOKEN${c_off}"
  echo -e "  ${c_dim}─────────────────────────────────────────────${c_off}"
  echo -e "  下一步    主控「节点管理」→ 添加节点 → 填入上方 Token 与节点地址"
  echo -e "  常用      systemctl restart $AGENT_SERVICE · journalctl -u $AGENT_SERVICE -f"
  echo -e "  提醒      云服务器请在控制台安全组放行 TCP $AGENT_PORT"
  echo -e "  ${c_g}${c_b}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${c_off}"
}

prune_for_agent() {  # 装完被控后移除仓库里用不到的主控/安装器代码（同机双装时保留）
  if systemctl list-unit-files 2>/dev/null | awk '{print $1}' | grep -qx "ppanel-master.service" \
     || { command -v docker >/dev/null 2>&1 && docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "ppanel-master"; }; then
    warn "检测到本机装有主控面板，保留完整仓库代码"
    return 0
  fi
  [ -d "$SRC_DIR/master" ] && rm -rf "$SRC_DIR/master" && ok "已移除主控代码（仅保留被控面板）"
  [ -d "$SRC_DIR/installer" ] && rm -rf "$SRC_DIR/installer" && ok "已移除安装器目录"
}

action_agent() {
  detect_env
  switch_mirror
  agent_install_base
  install_docker
  install_caddy
  ensure_repo "$AGENT_APP/agent_main.py"
  agent_deps
  agent_service
  prune_for_agent
  agent_summary
}

action_agent_reinstall() {  # 全新重装：清除旧安装（含数据）后重装
  detect_env
  echo -e "  ${c_y}将删除：$SRC_DIR（代码+配置，含同机主控）、/data/inst（全部实例数据）、$AGENT_SERVICE 服务${c_off}"
  confirm "确认完全清除并重新安装？" || { warn "已取消"; return 0; }
  systemctl disable --now "$AGENT_SERVICE" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$AGENT_SERVICE.service"; systemctl daemon-reload
  # 同机主控一并停用并移除（其代码即将被清，不停会留下握着已删文件的半残运行态）
  systemctl disable --now "$MASTER_SERVICE" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$MASTER_SERVICE.service"; systemctl daemon-reload
  remove_master_container
  rm -rf "$SRC_DIR" /data/inst /tmp/ppanel-uv-sync.log
  ok "旧安装已清除"
  switch_mirror
  agent_install_base
  install_docker
  install_caddy
  ensure_repo "$AGENT_APP/agent_main.py"
  agent_deps
  agent_service
  prune_for_agent
  agent_summary
}

action_agent_uninstall() {
  detect_env
  [ -d "$SRC_DIR" ] || [ -f "/etc/systemd/system/$AGENT_SERVICE.service" ] || fail "未检测到已安装的 PPanel 面板"
  echo -e "  ${c_y}卸载将删除：$AGENT_SERVICE 服务、整个 $SRC_DIR（若同机装有主控将一并删除）${c_off}"
  confirm "是否同时删除实例数据 /data/inst？（推荐卸载前备份）" \
    && RM_DATA=1 || RM_DATA=0
  confirm "确认卸载？" || { warn "已取消"; return 0; }
  info "停止服务..."
  systemctl disable --now "$AGENT_SERVICE" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$AGENT_SERVICE.service"; systemctl daemon-reload
  # 同机主控一并停用（其代码即将被清，不停会留下半残运行态）
  systemctl disable --now "$MASTER_SERVICE" >/dev/null 2>&1 || true
  rm -f "/etc/systemd/system/$MASTER_SERVICE.service"; systemctl daemon-reload
  remove_master_container
  rm -rf "$SRC_DIR" /tmp/ppanel-uv-sync.log
  [ "$RM_DATA" = 1 ] && { rm -rf /data/inst; ok "实例数据已删除"; } || ok "实例数据保留于 /data/inst"
  ok "PPanel 独立面板已卸载"
}

action_agent_reset_admin() {
  [ -f "$AGENT_APP/data/ppanel.db" ] || fail "未找到数据库（$AGENT_APP/data/ppanel.db）"
  confirm "删除内置管理员并按 .env 随机密码重建？" || { warn "已取消"; return 0; }
  "$AGENT_APP/.venv/bin/python" - <<PYEOF
import sqlite3
c = sqlite3.connect("$AGENT_APP/data/ppanel.db")
c.execute("DELETE FROM users WHERE username='admin'")
c.commit()
print("  管理员已删除")
PYEOF
  systemctl restart "$AGENT_SERVICE"
  ok "已重建（随机密码见 $AGENT_APP/.env 的 ADMIN_PASSWORD）"
}

# ============================================================
#  状态
# ============================================================
action_status() {
  echo ""
  if systemctl is-active --quiet "$MASTER_SERVICE" 2>/dev/null; then
    ok "主控（直装）：运行中（$MASTER_SERVICE，端口 $(grep -E '^MASTER_PORT=' "$MASTER_BACKEND/.env" 2>/dev/null | cut -d= -f2 || echo "$MASTER_PORT")）"
  else
    warn "主控（直装）：未安装或未运行"
  fi
  if command -v docker >/dev/null 2>&1 && docker ps --filter "name=ppanel-master" --format '{{.Names}} {{.Status}}' 2>/dev/null | grep -q .; then
    ok "主控（Docker）：$(docker ps --filter 'name=ppanel-master' --format '{{.Names}} · {{.Status}}')"
  else
    warn "主控（Docker）：未安装或未运行"
  fi
  if systemctl is-active --quiet "$AGENT_SERVICE" 2>/dev/null; then
    ok "被控（独立面板）：运行中（$AGENT_SERVICE，端口 $AGENT_PORT）"
    [ -f "$AGENT_APP/.env" ] && info "节点 Token：$(grep -E '^NODE_TOKEN=' "$AGENT_APP/.env" 2>/dev/null | cut -d= -f2)"
    info "实时日志：journalctl -u $AGENT_SERVICE -f"
  else
    warn "被控（独立面板）：未安装或未运行"
  fi
  echo ""
}

action_agent_info() {  # 被控安装信息：服务/地址/Token/主控接入参数一览
  banner
  echo ""
  step "被控安装信息"
  if ! systemctl is-active --quiet "$AGENT_SERVICE" 2>/dev/null; then
    warn "被控未安装或未运行（$AGENT_SERVICE），先执行菜单 [2] 安装"
    echo ""
    return 1
  fi
  local IP TOKEN
  IP=$(curl -s --max-time 5 https://api.ip.sb/ip 2>/dev/null | tr -d '[:space:]')
  [ -n "$IP" ] || IP=$(curl -s --max-time 5 https://ifconfig.me 2>/dev/null | tr -d '[:space:]')
  [ -n "$IP" ] || IP=$(hostname -I 2>/dev/null | awk '{print $1}')
  TOKEN=$(grep -E '^NODE_TOKEN=' "$AGENT_APP/.env" 2>/dev/null | cut -d= -f2)
  ok "服务状态：运行中（$AGENT_SERVICE，端口 $AGENT_PORT）"
  ok "面板地址：http://${IP:-<服务器IP>}:$AGENT_PORT/panel"
  info "节点 Token：${TOKEN:-未找到（检查 $AGENT_APP/.env 的 NODE_TOKEN）}"
  info "Caddy：$(systemctl is-active caddy >/dev/null 2>&1 && echo '运行中' || echo '未安装/未运行')"
  info "代码目录：$SRC_DIR"
  info "实时日志：journalctl -u $AGENT_SERVICE -f"
  echo ""
  info "主控接入（节点管理 → 添加节点）："
  info "  节点地址：http://${IP:-<服务器IP>}:$AGENT_PORT"
  info "  节点 Token：${TOKEN:-见上方 .env}"
  echo ""
}

_reset_admin_sql() {  # 输出重置管理员的 Python 片段（$1=新密码 $2=DB绝对路径）；直接写库，无论面板是否改过密码都生效
  cat <<PYEOF
import bcrypt, sqlite3, sys
new_pwd = sys.argv[1]
h = bcrypt.hashpw(new_pwd.encode(), bcrypt.gensalt()).decode()
db = sqlite3.connect(sys.argv[2])
try:
    db.execute("UPDATE users SET password_hash=? WHERE role='admin'", (h,))
    if db.total_changes == 0:
        import os
        uname = sys.argv[3] if len(sys.argv) > 3 else "admin"
        db.execute("INSERT INTO users (username, password_hash, role) VALUES (?, ?, 'admin')", (uname, h))
    db.commit()
    print("RESET_OK rows=", db.total_changes)
finally:
    db.close()
PYEOF
}

action_master_reset_admin() {  # 重置主控管理员密码：Docker 容器内或直装 venv 直接写库，随机新密码并同步 .env
  banner
  echo ""
  step "重置 主控管理员密码"
  local NEWPASS
  NEWPASS="$(openssl rand -base64 18 2>/dev/null | tr -dc 'A-Za-z0-9' | head -c 14)"
  [ -n "$NEWPASS" ] || NEWPASS="$(head -c 18 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 14)"

  if command -v docker >/dev/null 2>&1 && docker ps --filter "name=ppanel-master" --filter "status=running" --format '{{.Names}}' 2>/dev/null | grep -q ppanel-master; then
    # Docker 模式：容器内执行（bcrypt 与 DB 都在容器里，DB 走挂载卷真实落盘）
    local DB_IN_CTR="/srv/master/backend/data/master.db"
    [ "$(docker exec ppanel-master sh -c "[ -f '$DB_IN_CTR' ] && echo 1")" = "1" ] \
      || fail "容器内未找到数据库 $DB_IN_CTR"
    _reset_admin_sql > /tmp/ppanel-reset-admin.py
    docker cp /tmp/ppanel-reset-admin.py ppanel-master:/tmp/reset-admin.py
    docker exec ppanel-master python /tmp/reset-admin.py "$NEWPASS" "$DB_IN_CTR" admin \
      | grep -q RESET_OK || fail "重置失败：docker logs ppanel-master"
    rm -f /tmp/ppanel-reset-admin.py
    # 同步 .env，保持重建容器时配置一致
    if [ -f "$MASTER_DIR/.env" ] && grep -q '^ADMIN_PASSWORD=' "$MASTER_DIR/.env"; then
      sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$NEWPASS|" "$MASTER_DIR/.env"
    fi
    echo ""
    echo -e "  ${c_g}✔ 主控管理员密码已重置（Docker）${c_off}"
    echo -e "  账号      ${c_b}admin${c_off} / ${c_b}$NEWPASS${c_off}"
    echo -e "  ${c_y}⚠ 密码只显示这一次，请立即登录并修改${c_off}"
    echo ""
    return 0
  fi

  if systemctl is-active --quiet "$MASTER_SERVICE" 2>/dev/null; then
    # 直装模式：venv python 写库
    local VENV_PY="$MASTER_BACKEND/.venv/bin/python" DB="$MASTER_BACKEND/data/master.db"
    [ -x "$VENV_PY" ] || fail "直装主控虚拟环境不存在：$VENV_PY"
    [ -f "$DB" ] || fail "直装主控数据库不存在：$DB"
    _reset_admin_sql > /tmp/ppanel-reset-admin.py
    "$VENV_PY" /tmp/ppanel-reset-admin.py "$NEWPASS" "$DB" admin | grep -q RESET_OK \
      || fail "重置失败，检查数据库权限"
    rm -f /tmp/ppanel-reset-admin.py
    if [ -f "$MASTER_BACKEND/.env" ] && grep -q '^ADMIN_PASSWORD=' "$MASTER_BACKEND/.env"; then
      sed -i "s|^ADMIN_PASSWORD=.*|ADMIN_PASSWORD=$NEWPASS|" "$MASTER_BACKEND/.env"
    fi
    echo ""
    echo -e "  ${c_g}✔ 主控管理员密码已重置（直装）${c_off}"
    echo -e "  账号      ${c_b}admin${c_off} / ${c_b}$NEWPASS${c_off}"
    echo -e "  ${c_y}⚠ 密码只显示这一次，请立即登录并修改${c_off}"
    echo ""
    return 0
  fi

  warn "未检测到运行中的主控（Docker 容器或 systemd 服务），无需重置"
  echo ""
  return 1
}

# ============================================================
#  菜单与入口
# ============================================================
menu() {
  banner
  echo ""
  echo -e "  ${c_b}[1]${c_off} 安装 主控面板               ${c_dim}Docker 部署 · 自动拉取预构建镜像${c_off}"
  echo -e "  ${c_b}[2]${c_off} 安装 独立面板（被控）       ${c_dim}9100 端口 · 已有安装自动升级${c_off}"
  echo -e "  ${c_b}[3]${c_off} 卸载 主控面板               ${c_dim}数据可选保留${c_off}"
  echo -e "  ${c_b}[4]${c_off} 卸载 独立面板（被控）       ${c_dim}实例数据可选保留${c_off}"
  echo -e "  ${c_b}[0]${c_off} 退出"
  echo ""
  echo -e "  ${c_dim}更多子命令：bash install.sh status · agent-info · master-reset-admin${c_off}"
  echo -e "  ${c_dim}主控本机直装（systemd）：bash install.sh master${c_off}"
  echo ""
  read -rp "  请选择 [0-4]: " c
  echo ""
  case "$c" in
    1) action_master_docker ;;
    2) action_agent ;;
    3) action_master_uninstall ;;
    4) action_agent_uninstall ;;
    0) exit 0 ;;
    *) warn "无效选择"; exit 1 ;;
  esac
}

case "${1:-menu}" in
  menu)              menu ;;
  agent)             banner; action_agent ;;
  agent-reinstall)   banner; action_agent_reinstall ;;
  agent-uninstall)   banner; action_agent_uninstall ;;
  agent-reset-admin) banner; action_agent_reset_admin ;;
  master)            banner; action_master ;;
  master-docker)     banner; action_master_docker ;;
  master-reinstall)  banner; action_master_reinstall ;;
  master-docker-reinstall) banner; action_master_docker_reinstall ;;
  master-uninstall)  banner; action_master_uninstall ;;
  status)            banner; action_status ;;
  agent-info)        banner; action_agent_info ;;
  master-reset-admin) banner; action_master_reset_admin ;;
  *) echo -e "用法：bash install.sh [menu|agent|agent-reinstall|agent-uninstall|agent-reset-admin|agent-info|master|master-docker|master-reinstall|master-docker-reinstall|master-uninstall|master-reset-admin|status]"; exit 1 ;;
esac
