"""Pure ASGI middleware: the server-side version of the fake backend's latency and failure settings."""

from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.errors import ApiErrorCode, error_response

API_PREFIX = "/api/"
EXCLUDED = ("/api/admin", "/api/posters", "/api/events")


def is_simulated(path: str) -> bool:
    if not path.startswith(API_PREFIX):
        return False
    return not any(path == base or path.startswith(f"{base}/") for base in EXCLUDED)


class SimulationMiddleware:
    """Middleware cannot use `Depends`, so it reads the settings, RNG and sleep function from `app.state`."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_simulated(scope["path"]):
            state = scope["app"].state
            demo = state.demo_settings  # read per request, so a change applies immediately
            if demo.latency_ms:
                await state.sleep(demo.latency_ms / 1000)
            if demo.failure_rate > 0 and state.rng.random() < demo.failure_rate:
                response = error_response(
                    503, ApiErrorCode.SIMULATED_FAILURE, "Simulated server failure (see demo tools)"
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
