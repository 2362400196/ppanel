#!/bin/sh
# PPanel Agent 重启脚本（WSL /root/restart-agent.sh）
pkill -f "uvicorn agent_main" 2>/dev/null
sleep 1
cd /root/ppanel-agent || exit 1
nohup .venv/bin/uvicorn agent_main:app --host 0.0.0.0 --port 9100 > /tmp/ppanel-agent.log 2>&1 &
sleep 3
tail -3 /tmp/ppanel-agent.log
