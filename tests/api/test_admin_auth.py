"""The admin key is checked before the request body is read (a dependency alone runs after it)."""

import pytest


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("path", "content_type"),
    [
        ("/api/admin/movies", "application/json"),
        ("/api/admin/movies/m-deep-field/poster", "multipart/form-data; boundary=b"),
    ],
)
async def test_without_a_key_the_body_is_never_read(app, path, content_type):
    reads = 0
    sent = []

    async def receive():
        nonlocal reads
        reads += 1
        return {"type": "http.request", "body": b"not a valid body", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"test"), (b"content-type", content_type.encode())],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
    }
    # Raw ASGI, because TestClient and httpx always hand over the whole body.
    async with app.router.lifespan_context(app):
        await app(scope, receive, send)

    assert (sent[0]["status"], reads) == (401, 0)
