"""API v1 路由."""

from fastapi import APIRouter

from eidolon_agent.api.v1 import agent, chat

api_router = APIRouter(prefix="/v1", tags=["v1"])
api_router.include_router(chat.router, prefix="/chat", tags=["chat"])
api_router.include_router(agent.router, prefix="/agents", tags=["agents"])
