"""The dashboard shell — a zero-build static SPA served from package data (CP-5).

Three files, no bundler, no build step (a program non-goal): the shell itself is unauthenticated
(it contains no data); every API and SSE call it makes carries the operator's JWT.
"""

from importlib.resources import files

from fastapi import APIRouter
from fastapi.responses import Response

router = APIRouter(tags=["dashboard"])

_STATIC = files("podium.dashboard") / "static"
_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "text/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}


def _serve(name: str) -> Response:
    return Response(
        content=(_STATIC / name).read_text(encoding="utf-8"),
        media_type=_CONTENT_TYPES[name],
    )


@router.get("/dashboard", include_in_schema=False)
async def shell() -> Response:
    return _serve("index.html")


@router.get("/dashboard/app.js", include_in_schema=False)
async def app_js() -> Response:
    return _serve("app.js")


@router.get("/dashboard/style.css", include_in_schema=False)
async def style_css() -> Response:
    return _serve("style.css")


__all__ = ["router"]
