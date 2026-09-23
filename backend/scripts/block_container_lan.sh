#!/usr/bin/env bash
# ============================================================
# PPanel 容器内网隔离脚本（安全红线 3 配套）
#
# 作用：
#   1. 阻断容器（docker 网段）主动访问宿主机所在内网网段
#   2. 阻断容器主动访问宿主机本身（保留已建立连接的回包）
#
# 用法（在宿主机以 root 执行）：
#   sudo bash block_container_lan.sh                     # 默认阻断 192.168.0.0/16
#   sudo bash block_container_lan.sh 10.8.0.0/16         # 指定要阻断的内网网段
#   sudo bash block_container_lan.sh clean               # 移除本脚本添加的规则
#
# 说明：
#   - 规则加在 DOCKER-USER 链（容器出入流量的官方过滤点）与 INPUT 链
#   - 已建立连接（ESTABLISHED,RELATED）不受影响，避免打断现有会话
#   - 规则带 ppanel-block 注释，clean 只删自己加的
# ============================================================
set -euo pipefail

COMMENT="ppanel-block"
LAN_CIDR="${1:-192.168.0.0/16}"
DOCKER_SUBNET="${DOCKER_SUBNET:-172.16.0.0/12}"

if [[ "${1:-}" == "clean" ]]; then
  echo "移除 PPanel 隔离规则 ..."
  while iptables -D DOCKER-USER -s "$DOCKER_SUBNET" -j DROP -m comment --comment "$COMMENT" 2>/dev/null; do :; done
  while iptables -D INPUT -s "$DOCKER_SUBNET" -j DROP -m comment --comment "$COMMENT" 2>/dev/null; do :; done
  while iptables -D INPUT -s "$DOCKER_SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT -m comment --comment "$COMMENT" 2>/dev/null; do :; done
  echo "完成"
  exit 0
fi

echo "阻断容器网段 $DOCKER_SUBNET 访问内网 $LAN_CIDR 与宿主机本身 ..."

# 1) 容器 -> 指定内网网段：丢弃
if ! iptables -C DOCKER-USER -s "$DOCKER_SUBNET" -d "$LAN_CIDR" -j DROP -m comment --comment "$COMMENT" 2>/dev/null; then
  iptables -I DOCKER-USER 1 -s "$DOCKER_SUBNET" -d "$LAN_CIDR" -j DROP -m comment --comment "$COMMENT"
fi

# 2) 容器 -> 宿主机：放行回包后丢弃主动访问
if ! iptables -C INPUT -s "$DOCKER_SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT -m comment --comment "$COMMENT" 2>/dev/null; then
  iptables -I INPUT 1 -s "$DOCKER_SUBNET" -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT -m comment --comment "$COMMENT"
fi
if ! iptables -C INPUT -s "$DOCKER_SUBNET" -j DROP -m comment --comment "$COMMENT" 2>/dev/null; then
  iptables -I INPUT 2 -s "$DOCKER_SUBNET" -j DROP -m comment --comment "$COMMENT"
fi

echo "完成。当前规则："
iptables -S DOCKER-USER | grep "$COMMENT" || true
iptables -S INPUT | grep "$COMMENT" || true
