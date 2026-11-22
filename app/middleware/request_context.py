"""Pure ASGI middleware: a request id and the processing time for every HTTP response."""

import re
import time
import uuid

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.errors import internal_error_response
from app.core.logging import request_id_var

# A client's id is echoed into headers and logs, so only harmless characters and a sane length are accepted.
VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


class RequestContextMiddleware:
    """Also answers unhandled exceptions with the 500 `ApiError`.

    Starlette runs the app's `Exception` handler in `ServerErrorMiddleware`, outside every user middleware, so
    its response would have no CORS and no request id headers. Responding here sends the 500 back through
    CORS and GZip. The exception is raised again, so the handler still logs it and tests still see it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        incoming = Headers(scope=scope).get("x-request-id", "")
        request_id = incoming if VALID_REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id  # `request.state.request_id`
        response_started = False

        async def send_with_headers(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                headers["X-Process-Time"] = f"{(time.perf_counter() - started) * 1000:.1f}"
            await send(message)

        token = request_id_var.set(request_id)
        try:
            await self.app(scope, receive, send_with_headers)
        except Exception:
            if not response_started:
                await internal_error_response(request_id)(scope, receive, send_with_headers)
            raise
        finally:
            request_id_var.reset(token)
