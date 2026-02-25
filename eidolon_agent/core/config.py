"""全局配置：使用 pydantic-settings 从 .env 与环境变量读取."""

from functools import lru_cache
from typing import Any

from dotenv import find_dotenv, load_dotenv
from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 项目启动时（首次 import 本模块）自动加载 .env 并构建全局配置
load_dotenv(find_dotenv())


class Settings(BaseSettings):
    """应用配置，优先从 .env 加载，可被环境变量覆盖."""

    model_config = SettingsConfigDict(
        env_file=find_dotenv(),
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    # LLM（OpenAI 兼容 API）
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""
    llm_temperature: float = 0.6

    # 数据库
    database_url: str = "postgresql+asyncpg://user:password@localhost:5432/eidolon"

    # Mem0 记忆：API Key（必填则用云版 MemoryClient；OSS 版会注入到 llm.config）
    mem0_api_key: str = ""
    # Mem0 扩展配置（可选 JSON 字符串，如 vector_store、embedder 等）
    mem0_config: str = "{}"

    # 应用
    app_env: str = "development"
    log_level: str = "INFO"

    @field_validator("mem0_config", mode="before")
    @classmethod
    def parse_mem0_config(cls, v: Any) -> str:
        if isinstance(v, dict):
            import json
            return json.dumps(v)
        return str(v) if v else "{}"

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() == "development"


@lru_cache
def get_settings() -> Settings:
    """获取单例配置（推荐在应用内通过依赖注入使用）."""
    return Settings()


# 模块级便捷引用
settings = get_settings()
