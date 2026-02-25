"""异步数据库引擎与会话：SQLAlchemy 2.0 + asyncpg."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from eidolon_agent.core.config import settings
from eidolon_agent.models.base import Base

engine = create_async_engine(
    settings.database_url,
    echo=settings.is_development,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

async_session_factory = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autocommit=False,
    autoflush=False,
)


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """依赖注入：获取异步会话，请求结束后关闭."""
    async with async_session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()


async def init_db() -> None:
    """创建所有表（开发/测试用；生产建议用迁移）. 在应用启动时可选调用."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # 可选：无模板时插入默认性格基因（便于本地试用）
    from sqlalchemy import select
    from eidolon_agent.models.agent import AgentTemplate
    async with async_session_factory() as session:
        r = await session.execute(select(AgentTemplate).limit(1))
        if r.scalar_one_or_none() is None:
            session.add(AgentTemplate(
                name="默认助手",
                base_prompt="你是一个友善的数字生命助手，具有记忆与成长能力。请用简洁、自然的语气与用户交流。",
                description="默认性格模板",
            ))
            await session.commit()
