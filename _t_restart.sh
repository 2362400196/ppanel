#!/usr/bin/env bash
export SSHPASS='lx5CgaxlSz9euMbd'
sshpass -e ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 root@64.90.1.52 '
systemctl restart ppanel-agent
sleep 3
echo "agent: $(systemctl is-active ppanel-agent)"
echo "9100探活: $(curl -s -o /dev/null -w "%{http_code}" --max-time 5 http://127.0.0.1:9100/ 2>/dev/null)"
'
