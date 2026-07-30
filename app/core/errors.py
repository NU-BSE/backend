from typing import Any

from fastapi import Request
from fastapi.responses import ORJSONResponse


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


async def api_error_handler(_request: Request, exc: ApiError) -> ORJSONResponse:
    return ORJSONResponse(
        status_code=exc.status_code,
        content={
            "ok": False,
            "code": exc.code,
            "retryable": exc.retryable,
            "message": exc.message,
            **exc.extra,
        },
    )
