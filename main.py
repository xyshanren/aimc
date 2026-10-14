"""AIMC - AI Model Compass 主入口

启动命令：
    uvicorn main:app --host 0.0.0.0 --port 18080
"""

from gateway.app import create_app

app = create_app()

if __name__ == "__main__":
    import uvicorn
    from config import settings

    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
    )
