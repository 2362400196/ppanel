#!/usr/bin/env bash
# ============================================================
#  构建被控 Agent 的 Linux 发布包（预编译二进制 + 面板静态资源）
#
#  产物：
#    dist/ppanel-agent-linux-amd64.tar.gz   （binary + app/panel_ui + VERSION）
#    dist/SHA256SUMS                        （install.sh 下载后校验用）
#
#  用法：
#    bash scripts/build-release.sh            # 自动判断本机环境
#    ARCH=arm64 bash scripts/build-release.sh  # 交叉编 arm64（需 cross/zig 支持）
#
#  两种构建宿主：
#    1. Linux   —— 原生 cargo build（musl 目标已装则出静态二进制）
#    2. Windows —— 需要 zig + cargo-zigbuild（Git Bash 下运行）
#        export PATH="/d/ppanel-build/cargo-root/bin:/d/ppanel-build/zig-x86_64-windows-0.14.1:$PATH"
#        export ZIG_LOCAL_CACHE_DIR=D:/ppanel-build/zc ZIG_GLOBAL_CACHE_DIR=D:/ppanel-build/zg
# ============================================================
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."   # -> agent/backend

ARCH="${ARCH:-amd64}"                    # amd64 | arm64（install.sh 的命名口径）
case "$ARCH" in
  amd64) TRIPLE=x86_64-unknown-linux-musl ;;
  arm64) TRIPLE=aarch64-unknown-linux-musl ;;
  *) echo "不支持的 ARCH=$ARCH（可选 amd64 / arm64）" >&2; exit 1 ;;
esac

DIST="$PWD/dist"
mkdir -p "$DIST"
# 暂存目录放系统临时区：避免在项目目录内做递归删除（部分环境会用废纸篓代理拦截 rm）
STAGE=$(mktemp -d /tmp/ppanel-agent-stage.XXXXXX)

# ---------- 1. 编译 ----------
case "$(uname -s)" in
  Linux)
    if rustup target list --installed 2>/dev/null | grep -qx "$TRIPLE"; then
      echo "==> 原生编译（$TRIPLE，静态）"
      cargo build --release --target "$TRIPLE"
      BIN="target/$TRIPLE/release/ppanel-agent"
    else
      echo "==> 原生编译（本机默认目标，动态链接 glibc）"
      cargo build --release
      BIN="target/release/ppanel-agent"
    fi
    ;;
  MINGW*|MSYS*|CYGWIN*)
    command -v cargo-zigbuild >/dev/null 2>&1 \
      || { echo "缺少 cargo-zigbuild：cargo install cargo-zigbuild --locked" >&2; exit 1; }
    command -v zig >/dev/null 2>&1 \
      || { echo "缺少 zig：解压后把目录加入 PATH" >&2; exit 1; }
    echo "==> 交叉编译（$TRIPLE，zig 作为 C 编译器）"
    cargo zigbuild --release --target "$TRIPLE"
    BIN="target/$TRIPLE/release/ppanel-agent"   # Linux 目标产物无 .exe 后缀
    ;;
  *)
    echo "不支持在 $(uname -s) 上构建 Linux 包" >&2; exit 1 ;;
esac

# CARGO_TARGET_DIR 被外部指定时，产物不在 target/ 下
if [ ! -f "$BIN" ] && [ -n "${CARGO_TARGET_DIR:-}" ]; then
  BIN="${CARGO_TARGET_DIR}/${BIN#target/}"
fi
[ -f "$BIN" ] || { echo "未找到构建产物：$BIN" >&2; exit 1; }

# ---------- 2. 组装 ----------
cp "$BIN" "$STAGE/ppanel-agent"
chmod +x "$STAGE/ppanel-agent"
# 面板资源按「可执行文件同级的 app/panel_ui」布局，二进制启动时会自动找到
cp -r app/panel_ui "$STAGE/app_panel_ui_tmp"
mkdir -p "$STAGE/app" && mv "$STAGE/app_panel_ui_tmp" "$STAGE/app/panel_ui"
VER="$(grep -E '^version' Cargo.toml | head -1 | cut -d'"' -f2)"
printf 'ppanel-agent %s (%s, built %s)\n' \
  "$VER" "$TRIPLE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$STAGE/VERSION"

# ---------- 3. 打包 ----------
OUT="$DIST/ppanel-agent-linux-$ARCH.tar.gz"
tar czf "$OUT" -C "$STAGE" .
rm -rf "$STAGE"

cd "$DIST"
sha256sum "ppanel-agent-linux-$ARCH.tar.gz" > SHA256SUMS

echo ""
echo "==> 产物"
ls -lh "$OUT" | awk '{print "    "$9"  "$5}'
echo "    sha256: $(cut -d' ' -f1 SHA256SUMS)"
echo ""
echo "安装（全新 / 原地替换 Python 版）："
echo "    PPANEL_AGENT_IMPL=rust bash installer/install.sh agent"
echo "本地包安装（未发布到 Release 时）："
echo "    AGENT_RS_TARBALL=$OUT bash installer/install.sh agent"
