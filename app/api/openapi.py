"""What `/docs` says: the API and tag descriptions, operation ids, and the schema fix-ups FastAPI can't do."""

import copy
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.core.errors import ERROR_DOCS, ApiErrorCode
from app.middleware.simulation import is_simulated

HTTP_METHODS = ("get", "post", "put", "patch", "delete")

DESCRIPTION = """\
A FastAPI practice backend for the Movies MFE. It rebuilds the Angular app's fake backend and extends it.

* **Errors** are always an `ApiError`: `{code, message, details?}`. Branch on `code`, not on `message`.
  Invalid input is **400** `VALIDATION` (not FastAPI's usual 422). An unknown path is 404 `NOT_FOUND`,
  a wrong method 405 `METHOD_NOT_ALLOWED`.
* **Identity** is mocked: pass a `userId` from `GET /api/users`. There are no passwords.
* **Admin** routes need the `X-Admin-Key` header: click *Authorize*. The development key is `dev-admin-key`.
* **Times** are UTC, like `2026-11-19T12:00:00.000Z`. Calendar days are days in the theater's time zone.
* Every response has `X-Request-ID` and `X-Process-Time` headers. Send your own `X-Request-ID` to trace one.
* **Demo tools:** `PUT /api/admin/settings` adds latency and random 503s to the simulated routes (every `/api`
  route except admin, posters and events).
* `GET /health` (not listed here) reports the storage backend.
"""

REALTIME = """\
Live updates, from one server process.

**Server-Sent Events: `GET /api/events`**

| event | data |
|---|---|
| `booking-created` | `BookingDetails` |
| `booking-cancelled` | `BookingDetails` |
| `demo-reset` | `{}`: reload everything |

Each event has an increasing `id`. The stream starts with `retry: 3000`, sends `: ping` comments when idle,
and ends after `maxEvents` events or at server shutdown. Swagger UI can't show a stream that stays open:
use `curl -N`, or set `maxEvents`. It carries every user's bookings, which is only acceptable with mock users.

**WebSocket: `/ws/showtimes/{showtimeId}?userId=`** (WebSocket routes don't appear in OpenAPI)

Server to client, JSON with a `type`:

| type | fields | when |
|---|---|---|
| `snapshot` | `showtimeId, rows, seatsPerRow, bookedSeats, heldSeats` | on connect, layout change, reset |
| `seats-booked` | `seats` | seats became taken |
| `seats-released` | `seats` | seats became free |
| `error` | `code, message` | an invalid message (the socket stays open), or just before 4404 / 4410 |

Client to server: `{"type": "hold" | "release", "seats": ["A1"]}`. These are validated, but holds are not
supported yet, so the answer is an `error`.

| close code | meaning |
|---|---|
| 1001 | the server is shutting down |
| 1013 | the client fell too far behind |
| 4404 | unknown showtime |
| 4410 | the showtime is gone |
"""

TAGS: list[dict[str, Any]] = [
    {"name": "meta", "description": "Service health: `GET /health`, outside the schema and the simulation."},
    {"name": "movies", "description": "The catalogue, and the showtimes of each movie."},
    {"name": "showtimes", "description": "One showtime with its live seat data."},
    {"name": "users", "description": "The mock profiles. Their ids are the `userId` of the booking routes."},
    {"name": "posters", "description": "Generated SVG posters. Uploaded ones are served under `/uploads`."},
    {"name": "bookings", "description": "Book, list and cancel, with the rules of the Angular fake backend."},
    {"name": "admin", "description": "Manage the catalogue and the demo data. Needs `X-Admin-Key`."},
    {"name": "realtime", "description": REALTIME},
]


def operation_id(route: APIRoute) -> str:
    """`bookings-create_booking`. Routes added without a tag (tests do) still get an id."""
    return f"{route.tags[0] if route.tags else 'untagged'}-{route.name}"


def install_openapi(app: FastAPI) -> None:
    """Wraps `app.openapi()` and keeps its cache: the fix-ups run once per generated schema."""
    generate = app.openapi
    fixed: dict[str, Any] | None = None

    def openapi() -> dict[str, Any]:
        nonlocal fixed
        schema = generate()
        if schema is not fixed:  # newly generated (the first call, or routes were added since)
            # A copy: parts of it are the very dicts from `responses=` on the module-level routers.
            fixed = copy.deepcopy(schema)
            fix_schema(fixed)
            app.openapi_schema = fixed
        return app.openapi_schema or schema

    app.openapi = openapi  # type: ignore[method-assign]


def fix_schema(schema: dict[str, Any]) -> None:
    for path, item in schema["paths"].items():
        for method in HTTP_METHODS:
            if method in item:
                _fix_operation(path, item[method])
    components = schema.get("components", {}).get("schemas", {})
    # They described the 422 that this API never sends.
    components.pop("HTTPValidationError", None)
    components.pop("ValidationError", None)
    for component in components.values():
        _apply_required(component)
    _drop_impossible_null_defaults(schema)


def _fix_operation(path: str, operation: dict[str, Any]) -> None:
    responses = operation["responses"]
    codes: set[ApiErrorCode] = set()

    # FastAPI adds 422 wherever there is any parameter, also where none of them can be invalid.
    if responses.pop("422", None) is not None:
        parameters = operation.get("parameters", [])
        if "requestBody" in operation or any(_can_be_invalid(p) for p in parameters):
            codes.add(ApiErrorCode.VALIDATION)
    if "requestBody" in operation:
        codes.add(ApiErrorCode.BAD_REQUEST)  # an unreadable body
    codes.add(ApiErrorCode.INTERNAL)
    if is_simulated(path):
        codes.add(ApiErrorCode.SIMULATED_FAILURE)

    # What the route declared with `error_responses(...)`.
    for status in [status for status in responses if int(status) >= 400]:
        for media in responses.pop(status).get("content", {}).values():
            codes.update(ApiErrorCode(code) for code in media.get("examples", {}))

    for status, documented in error_documentation(codes).items():
        responses[status] = documented
    operation["responses"] = dict(sorted(responses.items()))

    for parameter in operation.get("parameters", []):
        field = parameter.get("schema", {})
        if field.pop("x-required", False):
            parameter["required"] = True
        _not_nullable(field)  # optional means left out: a URL can't send null


def _can_be_invalid(parameter: dict[str, Any]) -> bool:
    """A path parameter or an optional free string accepts anything."""
    schema = parameter.get("schema", {})
    options = [option for option in schema.get("anyOf", [schema]) if option.get("type") != "null"]
    free_string = all(
        option.get("type") == "string"
        and not option.keys() & {"enum", "format", "pattern", "minLength", "maxLength"}
        for option in options
    )
    if parameter["in"] == "path":
        return not free_string
    return parameter.get("required", False) or not free_string


def error_documentation(codes: set[ApiErrorCode]) -> dict[str, dict[str, Any]]:
    """The error responses are written here in full, so that they are always JSON: FastAPI would use the
    route's own media type, which is `text/event-stream` for the event stream."""
    responses: dict[str, dict[str, Any]] = {}
    for code in ApiErrorCode:  # enum order, so the schema is stable
        if code not in codes:
            continue
        doc = ERROR_DOCS[code]
        response = responses.setdefault(
            str(doc.status),
            {
                "description": "",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ApiError"}, "examples": {}}
                },
            },
        )
        response["description"] += f"{'<br>' if response['description'] else ''}`{code}`: {doc.when}"
        response["content"]["application/json"]["examples"][code.value] = {
            "summary": f"{code}: {doc.when}",
            "value": doc.example(code),
        }
        if code is ApiErrorCode.UNAUTHORIZED:
            response["headers"] = {"WWW-Authenticate": {"schema": {"type": "string", "const": "APIKey"}}}
    return responses


def _apply_required(component: dict[str, Any]) -> None:
    for name, field in component.get("properties", {}).items():
        if field.pop("x-required", False):
            component.setdefault("required", []).append(name)
            _not_nullable(field)


def _not_nullable(field: dict[str, Any]) -> None:
    options = [option for option in field.get("anyOf", []) if option.get("type") != "null"]
    if len(options) == 1:
        del field["anyOf"]
        field.update(options[0])


def _drop_impossible_null_defaults(node: Any) -> None:
    """`SkipJsonSchema[None]` removes null from the type but leaves `default: null`."""
    if isinstance(node, dict):
        if "default" in node and node["default"] is None:
            nullable = any(option.get("type") == "null" for option in node.get("anyOf", []))
            if not nullable:
                del node["default"]
        for value in node.values():
            _drop_impossible_null_defaults(value)
    elif isinstance(node, list):
        for value in node:
            _drop_impossible_null_defaults(value)
