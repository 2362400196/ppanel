#!/usr/bin/env bash
cp /mnt/d/ppanel/master/Dockerfile /opt/ppanel/master/Dockerfile
cd /opt/ppanel/master
echo "===== web stage ====="
if docker build --target web -t ppanel-webtest . > /tmp/pp-web.log 2>&1; then
  echo "WEB_OK"
  grep -E 'pnpm|Packages:|Done:|vite' /tmp/pp-web.log | head -15
  echo "===== full build ====="
  if docker build -t ppanel-master-test . > /tmp/pp-full.log 2>&1; then
    echo "FULL_OK"
    tail -5 /tmp/pp-full.log
  else
    echo "FULL_FAIL"
    tail -20 /tmp/pp-full.log
  fi
else
  echo "WEB_FAIL"
  grep -E 'npm error|Internal Error|Error:|ERROR' /tmp/pp-web.log | head -15
fi
