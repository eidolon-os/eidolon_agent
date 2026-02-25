"""pytest 配置与公共 fixture."""

import asyncio

import pytest


@pytest.fixture(scope="session")
def event_loop_policy():
    """使用默认事件循环策略，保证 pytest-asyncio 与 asyncio 兼容."""
    return asyncio.DefaultEventLoopPolicy()
