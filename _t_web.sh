#!/usr/bin/env bash
cd /opt/ppanel/master
docker build --target web --no-cache --progress=plain -t ppanel-webtest . 2>&1 | grep -E 'npm (ci|install|error|warn)|added|error|vite|ERROR' | head -40
