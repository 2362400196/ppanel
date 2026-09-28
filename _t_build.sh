#!/usr/bin/env bash
cp /mnt/d/ppanel/master/Dockerfile /opt/ppanel/master/Dockerfile
cd /opt/ppanel/master
docker build -t ppanel-master-test . 2>&1 | tail -30
echo "BUILD_EXIT=${PIPESTATUS[0]}"
