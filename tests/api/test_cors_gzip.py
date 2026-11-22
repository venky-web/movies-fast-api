import pytest
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

from app.schemas.demo import DemoSettings

ORIGIN = "http://localhost:4200"


def preflight(client, method="POST", origin=ORIGIN, headers="content-type,x-admin-key,x-request-id"):
    return client.options(
        "/api/bookings",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": method,
            "Access-Control-Request-Headers": headers,
        },
    )


class TestCors:
    @pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
    def test_preflight_from_the_angular_app(self, client, method):
        response = preflight(client, method)

        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == ORIGIN

    def test_preflight_from_elsewhere_is_refused(self, client):
        response = preflight(client, origin="http://evil.example")

        assert response.status_code == 400
        assert "Access-Control-Allow-Origin" not in response.headers

    def test_preflight_is_not_simulated(self, app, client):
        app.state.demo_settings = DemoSettings(latency_ms=0, failure_rate=1)

        assert preflight(client).status_code == 200

    def test_responses_expose_the_custom_headers(self, client):
        response = client.get("/api/movies", headers={"Origin": ORIGIN})

        exposed = set(response.headers["Access-Control-Expose-Headers"].split(", "))
        assert exposed == {"X-Total-Count", "Location", "X-Request-ID", "X-Process-Time", "ETag"}

    @pytest.mark.parametrize(
        ("path", "failure_rate", "status"),
        [("/api/movies", 1, 503), ("/api/nope", 0, 404), ("/api/admin/settings", 0, 401)],
    )
    def test_errors_keep_the_cors_headers(self, app, client, path, failure_rate, status):
        app.state.demo_settings = DemoSettings(latency_ms=0, failure_rate=failure_rate)

        response = client.get(path, headers={"Origin": ORIGIN})

        assert response.status_code == status
        assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
        assert response.headers["X-Request-ID"]  # CORS is outside the request context


class TestGZip:
    def test_large_responses_are_compressed(self, client):
        response = client.get("/api/movies", headers={"Accept-Encoding": "gzip"})

        assert response.headers["content-encoding"] == "gzip"
        assert len(response.json()) == 12

    def test_small_responses_are_not(self, client):
        assert "content-encoding" not in client.get("/health", headers={"Accept-Encoding": "gzip"}).headers

    def test_event_streams_are_not(self, app):
        """Pitfall 18: the pinned Starlette must skip `text/event-stream`, or Phase 6's SSE would lag."""

        @app.get("/stream")
        async def stream():
            async def body():
                yield b"data: " + b"x" * 5000 + b"\n\n"

            return StreamingResponse(body(), media_type="text/event-stream")

        with TestClient(app) as client:
            response = client.get("/stream", headers={"Accept-Encoding": "gzip"})

        assert "content-encoding" not in response.headers
