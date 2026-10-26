"""Loopback-only policy shared by mdspeak's local HTTP services."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from starlette.responses import PlainTextResponse, Response

LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
MAX_TEXT_CHARS = 50_000


def _local_origin(origin: str) -> bool:
    parsed = urlsplit(origin)
    return parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS


async def guard_local_request(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    """Reject non-loopback browser targets and prevent local audio caching.

    Binding Uvicorn to loopback remains the primary boundary; these checks make
    accidental proxying and cross-origin browser requests fail closed.
    """
    if request.url.hostname not in LOCAL_HOSTS:
        return PlainTextResponse("mdspeak accepts loopback requests only", status_code=403)
    origin = request.headers.get("origin")
    if origin and request.method not in {"GET", "HEAD", "OPTIONS"} and not _local_origin(origin):
        return PlainTextResponse("mdspeak rejects non-local origins", status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


def require_text_size(text: str) -> None:
    if len(text) > MAX_TEXT_CHARS:
        raise HTTPException(
            413, f"text exceeds the {MAX_TEXT_CHARS:,}-character local request limit"
        )
