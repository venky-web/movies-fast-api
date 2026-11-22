"""One error format for the whole API: `ApiError {code, message, details?}` (plan sections 3.2 and 3.3)."""

import logging
import math
from collections.abc import Mapping
from enum import StrEnum
from typing import Any, NamedTuple

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic.json_schema import SkipJsonSchema
from pydantic_core import PydanticCustomError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.schemas.base import CamelModel

log = logging.getLogger(__name__)

# Error type used by our own validators. The handler shows their message as-is (no field prefix).
BUSINESS_RULE = "business_rule"
LOCATION_PREFIXES = {"body", "query", "path", "header", "cookie"}
# Real bodies are 3 levels deep. An echo of deeper input is cut: at about 100 levels the serialiser gives up,
# which turned a 211-byte request into a 500.
MAX_ECHO_DEPTH = 16


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
    """The body of every error response."""

    code: ApiErrorCode
    message: str
    details: dict[str, Any] | SkipJsonSchema[None] = None  # left out when empty, never null


class ErrorDoc(NamedTuple):
    status: int
    when: str
    message: str
    details: dict[str, Any] | None = None

    def example(self, code: ApiErrorCode) -> dict[str, Any]:
        body = ApiError(code=code, message=self.message, details=self.details)
        return body.model_dump(mode="json", exclude_none=True)


# The one catalogue of errors behind `/docs`. A code has exactly one status.
ERROR_DOCS: dict[ApiErrorCode, ErrorDoc] = {
    ApiErrorCode.BAD_REQUEST: ErrorDoc(
        400, "The body could not be read, or a needed `userId` is missing", "The userId is required"
    ),
    ApiErrorCode.VALIDATION: ErrorDoc(
        400,
        "The input is invalid or breaks a rule",
        "Select between 1 and 6 seats",
        {
            "errors": [
                {
                    "type": BUSINESS_RULE,
                    "loc": ["body", "seats"],
                    "msg": "Select between 1 and 6 seats",
                    "input": [],
                }
            ]
        },
    ),
    ApiErrorCode.NOT_FOUND: ErrorDoc(
        404, "It does not exist, or is not visible to you", 'Movie "m-nope" not found'
    ),
    ApiErrorCode.SEATS_TAKEN: ErrorDoc(
        409,
        "Some seats are already booked",
        "Seats no longer available: A1, A2",
        {"takenSeats": ["A1", "A2"]},
    ),
    ApiErrorCode.SHOWTIME_STARTED: ErrorDoc(
        409, "The showtime has started", "This showtime has already started"
    ),
    ApiErrorCode.ALREADY_CANCELLED: ErrorDoc(
        409, "It is already cancelled", "This booking is already cancelled"
    ),
    ApiErrorCode.SIMULATED_FAILURE: ErrorDoc(
        503, "The demo `failureRate` struck", "Simulated server failure (see demo tools)"
    ),
    ApiErrorCode.UNAUTHORIZED: ErrorDoc(
        401, "The admin key is missing or wrong", "A valid X-Admin-Key header is required"
    ),
    ApiErrorCode.METHOD_NOT_ALLOWED: ErrorDoc(
        405, "The path exists, the method doesn't", "Method Not Allowed"
    ),
    ApiErrorCode.ALREADY_EXISTS: ErrorDoc(409, "The id is in use", 'Movie "m-deep-field" already exists'),
    ApiErrorCode.MOVIE_HAS_BOOKINGS: ErrorDoc(
        409,
        "Upcoming showtimes of the movie have confirmed bookings",
        'Movie "m-deep-field" has bookings for upcoming showtimes',
    ),
    ApiErrorCode.SHOWTIME_HAS_BOOKINGS: ErrorDoc(
        409, "Bookings reference the showtime", 'Showtime "st-m-deep-field-20350601-1900" has bookings'
    ),
    ApiErrorCode.LAYOUT_CONFLICT: ErrorDoc(
        409,
        "Booked seats would fall outside the layout",
        "Booked seats would fall outside the layout: H1",
        {"outsideSeats": ["H1"]},
    ),
    ApiErrorCode.PAYLOAD_TOO_LARGE: ErrorDoc(413, "The file is over 2 MB", "A poster can be at most 2 MB"),
    ApiErrorCode.UNSUPPORTED_MEDIA_TYPE: ErrorDoc(
        415,
        "Not a PNG, JPEG, WebP or SVG, or the content type doesn't match",
        "A poster must be a PNG, JPEG, WebP or SVG file with a matching content type",
    ),
    ApiErrorCode.INTERNAL: ErrorDoc(
        500,
        "An unexpected error",
        "Something went wrong on the server",
        {"requestId": "5f1c0a9e2b7d4c3a8e6f1b2d3c4a5e6f"},
    ),
}


def error_responses(*codes: ApiErrorCode) -> dict[int | str, dict[str, Any]]:
    """For `responses=`: the route's own errors. `app/api/openapi.py` adds the ones every route can have."""
    responses: dict[int | str, dict[str, Any]] = {}
    for code in codes:
        doc = ERROR_DOCS[code]
        response = responses.setdefault(
            doc.status,
            {"model": ApiError, "description": doc.when, "content": {"application/json": {"examples": {}}}},
        )
        response["content"]["application/json"]["examples"][code.value] = {"value": doc.example(code)}
    return responses


NOT_FOUND_RESPONSE = error_responses(ApiErrorCode.NOT_FOUND)
UNAUTHORIZED_RESPONSE = error_responses(ApiErrorCode.UNAUTHORIZED)


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
    if error.get("type") == "json_invalid":  # its location is a character position, not a field
        return f"Request body: {error['msg']}"
    path = format_location(tuple(error.get("loc", ())))
    return f"{path}: {error['msg']}" if path else f"Request body: {error['msg']}"


def json_safe(value: Any, depth: int = 0) -> Any:
    """Errors echo the client's input, which need not be JSON-serialisable.

    `jsonable_encoder` is not enough: it decodes bytes as UTF-8, keeps `inf` and keeps lone surrogates,
    and each of those makes the error response itself fail with a 500.
    """
    if depth > MAX_ECHO_DEPTH:
        return "<nested too deeply>"
    match value:
        case dict():
            return {json_safe(str(k)): json_safe(v, depth + 1) for k, v in value.items()}
        case list() | tuple():
            return [json_safe(v, depth + 1) for v in value]
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
