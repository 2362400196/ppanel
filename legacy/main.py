import uvicorn

from app.config import settings

if __name__ == "__main__":
    # 安全默认只监听本机；对外提供服务时改 PANEL_HOST 并配置好认证
    uvicorn.run("app.main:app", host=settings.host, port=settings.port)
