import logging
import re

import pytest
from fastapi.testclient import TestClient

from app.core.logging import LOG_FORMAT, OneLineFormatter, RequestIdFilter, configure_logging

ORIGIN = "http://localhost:4200"


def test_a_sent_request_id_is_echoed(client):
    response = client.get("/api/movies", headers={"X-Request-ID": "trace-123.A_b"})

    assert response.headers["X-Request-ID"] == "trace-123.A_b"


def test_a_missing_request_id_is_generated_per_request(client):
    first, second = (client.get("/api/movies").headers["X-Request-ID"] for _ in range(2))

    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert first != second


@pytest.mark.parametrize("sent", ["", "x" * 65, "has space", "semi;colon", b"caf\xe9"])
def test_an_unsafe_request_id_is_replaced(client, sent):
    response = client.get("/api/movies", headers={"X-Request-ID": sent})

    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["X-Request-ID"])


@pytest.mark.parametrize("path", ["/api/movies", "/api/nope", "/api/admin/settings", "/health"])
def test_every_response_has_id_and_process_time(client, path):
    response = client.get(path)

    assert response.headers["X-Request-ID"]
    assert float(response.headers["X-Process-Time"]) >= 0


class TestUnhandledErrors:
    @pytest.fixture
    def failing(self, app):
        @app.get("/api/boom")
        async def boom():
            raise RuntimeError("kaboom")

        return app

    def test_500_has_the_id_in_body_and_header_and_keeps_cors(self, failing):
        with TestClient(failing, raise_server_exceptions=False) as client:
            response = client.get("/api/boom", headers={"X-Request-ID": "trace-500", "Origin": ORIGIN})

        assert response.status_code == 500
        assert response.json() == {
            "code": "INTERNAL",
            "message": "Something went wrong on the server",
            "details": {"requestId": "trace-500"},
        }
        assert response.headers["X-Request-ID"] == "trace-500"
        # Starlette's own 500 response would be sent outside CORS, and a browser could not read it.
        assert response.headers["Access-Control-Allow-Origin"] == ORIGIN

    def test_500_is_logged_once_with_the_id_and_traceback(self, failing, caplog):
        with TestClient(failing, raise_server_exceptions=False) as client:
            client.get("/api/boom", headers={"X-Request-ID": "trace-500"})

        [record] = [r for r in caplog.records if r.name == "app.core.errors"]
        assert record.request_id == "trace-500"
        assert record.exc_info and "kaboom" in str(record.exc_info[1])

    def test_the_exception_still_reaches_the_test_client(self, failing):
        with TestClient(failing) as client, pytest.raises(RuntimeError, match="kaboom"):
            client.get("/api/boom")


class TestLogging:
    def test_app_records_carry_the_request_id(self, client, caplog):
        caplog.handler.addFilter(RequestIdFilter())
        with caplog.at_level(logging.INFO, logger="app"):
            client.get("/api/movies", headers={"X-Request-ID": "trace-log"})

        assert "trace-log" in {r.request_id for r in caplog.records if r.name == "app.audit"}

    def test_a_record_outside_a_request_gets_a_dash(self):
        record = logging.makeLogRecord({"msg": "startup"})

        RequestIdFilter().filter(record)

        assert record.request_id == "-"

    def test_newlines_in_a_message_cannot_forge_a_line(self):
        record = logging.makeLogRecord({"msg": "GET /a\r\nINFO forged", "request_id": "r1", "name": "app.x"})

        assert "\n" not in OneLineFormatter(LOG_FORMAT).format(record)

    def test_configuring_twice_keeps_one_handler(self):
        configure_logging()
        configure_logging()

        assert [h.get_name() for h in logging.getLogger("app").handlers].count("app-console") == 1
        assert logging.getLogger("app").isEnabledFor(logging.INFO)  # the audit log was invisible without this
