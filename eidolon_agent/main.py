"""FastAPI 入口：挂载 v1 路由、生命周期（DB/LLM 关闭）. 运行: uvicorn eidolon_agent.main:app."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from eidolon_agent.api.v1 import api_router
from eidolon_agent.core.database import init_db
from eidolon_agent.core.llm_client import close_llm_client
from eidolon_agent.utils.logging import setup_logging


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时配置日志、建表；关闭时释放 httpx/LLM 客户端."""
    setup_logging()
    await init_db()
    yield
    await close_llm_client()


app = FastAPI(
    title="Eidolon Agent",
    description="数字生命智能体平台：多用户、记忆与进化",
    version="0.1.0",
    lifespan=lifespan,
)

app.include_router(api_router, prefix="/api")


@app.get("/health")
async def health():
    """健康检查."""
    return {"status": "ok"}
