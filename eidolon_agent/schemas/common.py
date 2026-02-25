"""统一 API 返回格式."""

from typing import Any, Generic, TypeVar

from pydantic import BaseModel

T = TypeVar("T")


class ApiResponse(BaseModel, Generic[T]):
    """标准接口返回：code, message, data."""

    code: int = 200
    message: str = "success"
    data: T | None = None

    @classmethod
    def ok(cls, data: T | None = None, message: str = "success") -> "ApiResponse[T]":
        return cls(code=200, message=message, data=data)

    @classmethod
    def fail(cls, message: str, code: int = 400, data: Any = None) -> "ApiResponse[None]":
        return cls(code=code, message=message, data=data)
