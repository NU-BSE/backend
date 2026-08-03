from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.extra = extra or {}


def error_body(exc: ApiError) -> dict[str, Any]:
    return {
        "ok": False,
        "code": exc.code,
        "message": exc.message,
        # The mobile client reads `detail` first (src/auth/emailAuth.ts).
        "detail": exc.message,
        "retryable": exc.retryable,
        **exc.extra,
    }


async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=error_body(exc))


async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "ok": False,
            "code": "VALIDATION_ERROR",
            "message": "Request validation failed",
            "detail": exc.errors(),
            "retryable": False,
        },
    )


async def http_exception_handler(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "code": "HTTP_ERROR",
            "message": message,
            "detail": message,
            "retryable": False,
        },
    )


async def unhandled_error_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={
            "ok": False,
            "code": "INTERNAL",
            "message": "Internal server error",
            "detail": "Internal server error",
            "retryable": True,
        },
    )
