import pytest

from app.services.posters import MAX_POSTER_BYTES
from tests.conftest import ADMIN_KEY

URL = "/api/admin/movies/m-deep-field/poster"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake image data"
JPEG = b"\xff\xd8\xff\xe0" + b"fake image data"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 "
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"/>'


@pytest.fixture
def admin(client):
    client.headers["X-Admin-Key"] = ADMIN_KEY
    return client


def upload(client, data, content_type="image/png", filename="poster.png", url=URL):
    return client.post(url, files={"file": (filename, data, content_type)})


def stored_files(settings):
    return sorted(p.name for p in (settings.uploads_dir / "posters").iterdir())


@pytest.mark.parametrize(
    ("data", "content_type", "extension"),
    [
        pytest.param(PNG, "image/png", "png", id="png"),
        pytest.param(JPEG, "image/jpeg", "jpg", id="jpeg"),
        pytest.param(WEBP, "image/webp", "webp", id="webp"),
        pytest.param(SVG, "image/svg+xml", "svg", id="svg"),
        pytest.param(b'<?xml version="1.0"?>\n' + SVG, "image/svg+xml", "svg", id="svg-with-prolog"),
    ],
)
def test_an_upload_becomes_the_poster_url_and_is_served(admin, data, content_type, extension):
    response = upload(admin, data, content_type)

    assert response.status_code == 200, response.text
    url = admin.get("/api/movies/m-deep-field").json()["posterUrl"]
    assert url == response.json()["posterUrl"]
    assert url.startswith("/uploads/posters/m-deep-field-") and url.endswith(f".{extension}")
    served = admin.get(url)
    assert (served.content, served.headers["content-type"].split(";")[0]) == (data, content_type)


def test_the_client_file_name_is_never_used(admin, settings):
    assert upload(admin, PNG, filename="../../../evil.exe").status_code == 200

    [name] = stored_files(settings)
    assert name.startswith("m-deep-field-") and name.endswith(".png")


def test_exactly_2_mb_is_allowed(admin):
    assert upload(admin, PNG.ljust(MAX_POSTER_BYTES, b"\0")).status_code == 200


@pytest.mark.parametrize("size", [MAX_POSTER_BYTES + 1, 3 * 1024 * 1024])
def test_too_large_is_413(admin, settings, size):
    response = upload(admin, PNG.ljust(size, b"\0"))

    assert (response.status_code, response.json()["code"]) == (413, "PAYLOAD_TOO_LARGE")
    assert stored_files(settings) == []


@pytest.mark.parametrize(
    ("data", "content_type"),
    [
        pytest.param(b"just text", "image/png", id="text-named-png"),
        pytest.param(b"", "image/png", id="empty"),
        pytest.param(PNG, "image/jpeg", id="declared-type-differs"),
        pytest.param(PNG, "application/octet-stream", id="no-real-type"),
        pytest.param(b'<?xml version="1.0"?><note/>', "image/svg+xml", id="xml-that-is-not-svg"),
    ],
)
def test_wrong_type_is_415(admin, settings, data, content_type):
    response = upload(admin, data, content_type)

    assert (response.status_code, response.json()["code"]) == (415, "UNSUPPORTED_MEDIA_TYPE")
    assert stored_files(settings) == []


def test_a_new_upload_deletes_the_previous_file(admin, settings):
    first = upload(admin, PNG).json()["posterUrl"]
    second = upload(admin, JPEG, "image/jpeg").json()["posterUrl"]

    assert stored_files(settings) == [second.rsplit("/", 1)[1]]
    assert admin.get(first).json()["code"] == "NOT_FOUND"


def test_editing_the_movie_keeps_the_uploaded_poster(admin):
    url = upload(admin, PNG).json()["posterUrl"]

    after = admin.patch("/api/admin/movies/m-deep-field", json={"title": "Deeper Field"}).json()

    assert after["posterUrl"] == url


def test_unknown_movie_and_missing_file(admin):
    assert upload(admin, PNG, url="/api/admin/movies/nope/poster").status_code == 404
    assert admin.post(URL).json()["message"].startswith("file: ")


def test_reset_deletes_uploads_but_no_other_files(admin, settings):
    upload(admin, PNG)
    unrelated = settings.uploads_dir / "posters" / "keep-me.txt"
    unrelated.write_text("not ours")

    assert admin.post("/api/admin/reset").status_code == 204

    assert stored_files(settings) == ["keep-me.txt"]
    assert admin.get("/api/movies/m-deep-field").json()["posterUrl"] == "/api/posters/m-deep-field.svg"


@pytest.mark.parametrize("path", ["..%2Fsecret.txt", "..%5Csecret.txt", "posters/%2e%2e/%2e%2e/secret.txt"])
def test_uploads_mount_does_not_serve_files_outside_its_folder(client, settings, path):
    (settings.uploads_dir.parent / "secret.txt").write_text("secret")

    response = client.get(f"/uploads/{path}")

    assert response.status_code == 404
    assert "secret\n" not in response.text
