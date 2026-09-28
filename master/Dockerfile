# PPanel 主控镜像：多阶段构建（前端 node 构建 → Python 运行）
# 目录约定：镜像内 /srv/master/backend（工作目录）+ /srv/master/frontend/dist（静态资源）

# ---------- 阶段一：前端构建 ----------
FROM node:20-slim AS web
WORKDIR /web
# npm 10 装大依赖树会间歇性触发内部崩溃（Exit handler never called，且进程以 0 退出留下残缺树）。
# 首选 pnpm（corepack 自带，无此 bug）；pnpm 不可用时退回 npm 并校验 vite 就位，问题拦在安装步。
ENV npm_config_registry=https://registry.npmmirror.com \
    COREPACK_INTEGRITY_KEYS=0 \
    NPM_CONFIG_AUDIT=false \
    NPM_CONFIG_FUND=false \
    NPM_CONFIG_MAXSOCKETS=3
COPY frontend/package.json frontend/package-lock.json ./
RUN corepack enable && corepack prepare pnpm@9 --activate; \
    { pnpm import && pnpm install; } \
    || { npm ci --loglevel=error || npm install --loglevel=error; }; \
    test -x node_modules/.bin/vite \
    || { rm -rf node_modules && npm install --loglevel=error && test -x node_modules/.bin/vite; }
COPY frontend/ ./
RUN npm run build

# ---------- 阶段二：运行镜像 ----------
FROM python:3.12-slim
WORKDIR /srv/master/backend
COPY backend/ ./
COPY --from=web /web/dist /srv/master/frontend/dist

# pip 优先清华源（全球可达），失败回退官方 PyPI
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
        "bcrypt>=5.0.0" "cryptography>=50.0.1" "fastapi>=0.141.1" "httpx>=0.28.1" \
        "pillow>=12.3.0" "pyjwt>=2.14.0" "python-multipart>=0.0.32" "qrcode>=8.2" \
        "sqlalchemy>=2.0.54" "uvicorn[standard]>=0.53.0" "websockets>=13.0" \
    || pip install --no-cache-dir \
        "bcrypt>=5.0.0" "cryptography>=50.0.1" "fastapi>=0.141.1" "httpx>=0.28.1" \
        "pillow>=12.3.0" "pyjwt>=2.14.0" "python-multipart>=0.0.32" "qrcode>=8.2" \
        "sqlalchemy>=2.0.54" "uvicorn[standard]>=0.53.0" "websockets>=13.0"

ENV MASTER_HOST=0.0.0.0 \
    MASTER_PORT=8001 \
    PYTHONUNBUFFERED=1
EXPOSE 8001

CMD ["python", "-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
