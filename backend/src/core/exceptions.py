"""全局异常定义与处理器。"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from loguru import logger


class AppException(Exception):
    """业务异常基类。"""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class NotFoundError(AppException):
    """资源不存在。"""

    def __init__(self, message: str = "资源不存在") -> None:
        super().__init__(code=404, message=message)


class PermissionDeniedError(AppException):
    """越权访问（知识库权限隔离）。"""

    def __init__(self, message: str = "无权访问该资源") -> None:
        super().__init__(code=403, message=message)


def register_exception_handlers(app: FastAPI) -> None:
    """注册全局异常处理器：生产环境仅返回用户友好提示，不泄露堆栈。"""

    @app.exception_handler(AppException)
    async def app_exception_handler(_: Request, exc: AppException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.code, content={"code": exc.code, "message": exc.message}
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(_: Request, exc: Exception) -> JSONResponse:
        logger.exception(f"未处理异常：{exc}")
        return JSONResponse(
            status_code=500, content={"code": 500, "message": "服务器内部错误"}
        )
