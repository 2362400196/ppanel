#!/usr/bin/env bash
cp /mnt/d/ppanel/master/Dockerfile /opt/ppanel/master/Dockerfile
cd /opt/ppanel/master
echo "===== web stage ====="
docker build --target web -t ppanel-webtest . 2>&1 | grep -E 'pnpm|npm|vite|ERROR|Done|error' | head -25
echo "WEB_EXIT=${PIPESTATUS[0]}"
if [ "$WEB_EXIT" = 0 ]; then
  echo "===== full build ====="
  docker build -t ppanel-master-test . 2>&1 | tail -8
  echo "BUILD_EXIT=${PIPESTATUS[0]}"
fi
