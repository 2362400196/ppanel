import uvicorn

from app.config import settings

if __name__ == "__main__":
    uvicorn.run("main:app", host=settings.host, port=settings.port)
