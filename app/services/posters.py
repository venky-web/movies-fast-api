"""Poster uploads: size and content checks, then file storage (plan 5.13)."""

import logging
import re
import uuid
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from starlette.concurrency import run_in_threadpool

from app.core.errors import ApiErrorCode, AppError, not_found
from app.schemas.movie import MovieRecord
from app.services.locks import CATALOG_LOCK

if TYPE_CHECKING:
    from app.services.admin import AdminContext  # admin imports this module

log = logging.getLogger(__name__)

MAX_POSTER_BYTES = 2 * 1024 * 1024
CHUNK_BYTES = 64 * 1024
MEDIA_TYPES = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp", "svg": "image/svg+xml"}
# Only names this module generates. Reset never deletes any other file.
UPLOAD_NAME = re.compile(r"[a-z0-9][a-z0-9-]*-[0-9a-f]{8}\.(?:png|jpg|webp|svg)")


class Upload(Protocol):
    """The part of `UploadFile` that the service needs (services don't import FastAPI)."""

    @property
    def content_type(self) -> str | None: ...

    async def read(self, size: int = -1) -> bytes: ...


async def read_limited(file: Upload) -> bytes:
    data = bytearray()
    while chunk := await file.read(CHUNK_BYTES):
        data += chunk
        if len(data) > MAX_POSTER_BYTES:
            raise AppError(413, ApiErrorCode.PAYLOAD_TOO_LARGE, "A poster can be at most 2 MB")
    return bytes(data)


def detect_extension(data: bytes) -> str | None:
    """The type according to the first bytes. The file name and the declared type can say anything."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    text = data.removeprefix(b"\xef\xbb\xbf").lstrip()
    if text.startswith(b"<svg") or (text.startswith(b"<?xml") and b"<svg" in text):
        return "svg"
    return None


def checked_extension(data: bytes, content_type: str | None) -> str:
    extension = detect_extension(data)
    declared = (content_type or "").split(";")[0].strip().lower()
    if extension is None or MEDIA_TYPES[extension] != declared:
        raise AppError(
            415,
            ApiErrorCode.UNSUPPORTED_MEDIA_TYPE,
            "A poster must be a PNG, JPEG, WebP or SVG file with a matching content type",
        )
    return extension


def posters_dir(uploads_dir: Path) -> Path:
    return uploads_dir / "posters"


def _write(folder: Path, name: str, data: bytes) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)


def _delete(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:  # on Windows a file that is being served cannot be deleted
        log.warning("Could not delete %s", path, exc_info=True)


def _clear(folder: Path) -> None:
    if not folder.is_dir():
        return
    for path in folder.iterdir():
        if path.is_file() and UPLOAD_NAME.fullmatch(path.name):
            _delete(path)


async def clear_uploads(uploads_dir: Path) -> None:
    await run_in_threadpool(_clear, posters_dir(uploads_dir))


async def upload_poster(movie_id: str, file: Upload, ctx: "AdminContext", uploads_dir: Path) -> MovieRecord:
    if await ctx.repos.movies.get(movie_id) is None:
        raise not_found("Movie", movie_id)
    data = await read_limited(file)
    extension = checked_extension(data, file.content_type)
    folder = posters_dir(uploads_dir)

    async with ctx.locks.get(CATALOG_LOCK):
        # Re-read: the movie may have been edited or deleted while the file was being read.
        movie = await ctx.repos.movies.get(movie_id)
        if movie is None:
            raise not_found("Movie", movie_id)
        name = f"{movie_id}-{uuid.uuid4().hex[:8]}.{extension}"
        await run_in_threadpool(_write, folder, name, data)
        updated = movie.model_copy(update={"uploaded_poster": name, "poster_url": f"/uploads/posters/{name}"})
        await ctx.repos.movies.save(updated)
        if movie.uploaded_poster:
            # Only after the commit: after a rollback the movie would still point at this file.
            await ctx.after_commit(partial(run_in_threadpool, _delete, folder / movie.uploaded_poster))
    return updated
