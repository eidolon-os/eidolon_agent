"""全局异常定义（后续步骤中在 API 与服务层使用）."""

from typing import Any


class EidolonException(Exception):
    """业务异常基类."""

    def __init__(self, message: str, code: int = 400, details: Any = None):
        self.message = message
        self.code = code
        self.details = details
        super().__init__(message)
