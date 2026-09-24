#!/bin/sh
# 等待 docker 就绪后确认 agent 启动（WSL 侧健康检查）
i=0
while [ $i -lt 20 ]; do
  s=$(systemctl is-active docker)
  [ "$s" = "active" ] && break
  sleep 3
  i=$((i + 1))
done
echo "docker=$(systemctl is-active docker) agent=$(systemctl is-active ppanel-agent)"
curl -s -m 4 http://localhost:9100/agent/ping
echo
