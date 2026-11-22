"""One error format for the whole API: `ApiError {code, message, details?}` (plan sections 3.2 and 3.3)."""

import logging
import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic_core import PydanticCustomError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.base import CamelModel

log = logging.getLogger(__name__)

# Error type used by our own validators. The handler shows their message as-is (no field prefix).
BUSINESS_RULE = "business_rule"
LOCATION_PREFIXES = {"body", "query", "path", "header", "cookie"}


class ApiErrorCode(StrEnum):
    BAD_REQUEST = "BAD_REQUEST"
    VALIDATION = "VALIDATION"
    NOT_FOUND = "NOT_FOUND"
    SEATS_TAKEN = "SEATS_TAKEN"
    SHOWTIME_STARTED = "SHOWTIME_STARTED"
    ALREADY_CANCELLED = "ALREADY_CANCELLED"
    SIMULATED_FAILURE = "SIMULATED_FAILURE"
    UNAUTHORIZED = "UNAUTHORIZED"
    METHOD_NOT_ALLOWED = "METHOD_NOT_ALLOWED"
    ALREADY_EXISTS = "ALREADY_EXISTS"
    MOVIE_HAS_BOOKINGS = "MOVIE_HAS_BOOKINGS"
    SHOWTIME_HAS_BOOKINGS = "SHOWTIME_HAS_BOOKINGS"
    LAYOUT_CONFLICT = "LAYOUT_CONFLICT"
    PAYLOAD_TOO_LARGE = "PAYLOAD_TOO_LARGE"
    UNSUPPORTED_MEDIA_TYPE = "UNSUPPORTED_MEDIA_TYPE"
    INTERNAL = "INTERNAL"
    # NETWORK exists only on the client (Angular `toApiError`); the server never sends it.


class ApiError(CamelModel):
    code: ApiErrorCode
    message: str
    details: dict[str, Any] | None = None


class AppError(Exception):
    """Raised by services and dependencies; turned into an `ApiError` response by `app_error_handler`."""

    def __init__(
        self,
        status: int,
        code: ApiErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details
        self.headers = headers


def not_found(kind: str, item_id: str) -> AppError:
    return AppError(404, ApiErrorCode.NOT_FOUND, f'{kind} "{item_id}" not found')


def seats_taken(seats: list[str]) -> AppError:
    """Raised by the booking service's check, and by the SQL repository when the unique constraint fires."""
    return AppError(
        409, ApiErrorCode.SEATS_TAKEN, f"Seats no longer available: {', '.join(seats)}", {"takenSeats": seats}
    )


def business_rule(message: str) -> PydanticCustomError:
    """A validation error whose message is shown to the client unchanged (e.g. "Seats must not repeat")."""
    return PydanticCustomError(BUSINESS_RULE, message)


def error_response(
    status: int,
    code: ApiErrorCode,
    message: str,
    details: dict[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    body = ApiError(code=code, message=message, details=details or None)
    return JSONResponse(body.model_dump(mode="json", exclude_none=True), status_code=status, headers=headers)


def format_location(loc: tuple[int | str, ...]) -> str:
    parts = list(loc[1:] if loc and loc[0] in LOCATION_PREFIXES else loc)
    path = ""
    for part in parts:
        path += f"[{part}]" if isinstance(part, int) else (f".{part}" if path else str(part))
    return path


def validation_message(error: dict[str, Any]) -> str:
    if error.get("type") == BUSINESS_RULE:
        return str(error["msg"])
    path = format_location(tuple(error.get("loc", ())))
    return f"{path}: {error['msg']}" if path else f"Request body: {error['msg']}"


def json_safe(value: Any) -> Any:
    """Errors echo the client's input, which need not be JSON-serialisable.

    `jsonable_encoder` is not enough: it decodes bytes as UTF-8, keeps `inf` and keeps lone surrogates,
    and each of those makes the error response itself fail with a 500.
    """
    match value:
        case dict():
            return {json_safe(str(k)): json_safe(v) for k, v in value.items()}
        case list() | tuple():
            return [json_safe(v) for v in value]
        case str():
            return value.encode("utf-8", "backslashreplace").decode("utf-8")
        case bytes():
            return f"<{len(value)} bytes>"
        case float() if not math.isfinite(value):
            return str(value)
        case int() | float() | None:
            return value
        case _:
            return str(value)


def json_safe_errors(errors: list[Any]) -> list[dict[str, Any]]:
    return [json_safe({k: v for k, v in error.items() if k != "url"}) for error in errors]


async def app_error_handler(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, AppError)
    return error_response(exc.status, exc.code, exc.message, exc.details, exc.headers)


async def validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """FastAPI answers 422 by default; the fake-backend contract says 400 `VALIDATION`."""
    assert isinstance(exc, RequestValidationError)
    errors = list(exc.errors())
    message = validation_message(errors[0]) if errors else "Invalid request"
    return error_response(400, ApiErrorCode.VALIDATION, message, {"errors": json_safe_errors(errors)})


async def http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Starlette's own errors: no matching route (404), wrong method (405), others."""
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code == 404:
        message = (
            exc.detail
            if exc.detail != "Not Found"
            else f"No endpoint for {request.method} {request.url.path}"
        )
        return error_response(404, ApiErrorCode.NOT_FOUND, message, headers=exc.headers)
    if exc.status_code == 405:
        return error_response(405, ApiErrorCode.METHOD_NOT_ALLOWED, str(exc.detail), headers=exc.headers)
    return error_response(exc.status_code, ApiErrorCode.BAD_REQUEST, str(exc.detail), headers=exc.headers)


def internal_error_response(request_id: str | None) -> JSONResponse:
    return error_response(
        500,
        ApiErrorCode.INTERNAL,
        "Something went wrong on the server",
        {"requestId": request_id} if request_id else None,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Logs the error. `RequestContextMiddleware` has normally sent the 500 already; this response is then
    only the fallback for errors raised outside that middleware."""
    request_id = getattr(request.state, "request_id", None)
    log.exception(
        "Unhandled error on %s %s", request.method, request.url.path, extra={"request_id": request_id}
    )
    return internal_error_response(request_id)


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
