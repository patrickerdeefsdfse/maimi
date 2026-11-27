"""Serve the portal theme and optional deployment-supplied artwork."""
import os
from hashlib import sha256
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import Response

router = APIRouter()
THEME = Path(__file__).with_name("portal_theme")
PREFIX = "/maimi/web/cosmos/"
LANDING_HTML = (THEME / "landing_html.html").read_text(encoding="utf-8")
WAITING_HTML = (THEME / "waiting_html.html").read_text(encoding="utf-8")
PROGRAMS = {
    "706865ac8e46fffe.css": "text/css",
    "706865ac8e46fffe.js": "text/javascript",
    "df995358f7975ea84a3b.css": "text/css",
    "135c51a9bb2b6b3db615.js": "text/javascript",
}
ARTWORK = {
    "17c9ab51caed8424dbae-nebula.png": ("background.png", "background.png", "image/png"),
    "nebula-nov5.png": ("background-alt.png", "background.png", "image/png"),
    "17432e1a34777091a186-maimi.png": ("logo.png", "logo.png", "image/png"),
    "8b6e086edfbca846bb00-maimi-nebula.jpg": ("wallpaper.jpg", "background.jpg", "image/jpeg"),
}


def artwork(name, override, fallback, mime):
    directory = os.environ.get("MAIMI_PORTAL_ASSETS")
    if directory:
        for approved_name in (override, name):
            path = Path(directory) / approved_name
            if path.is_file() and 0 < path.stat().st_size <= 16 * 1024 * 1024:
                data = path.read_bytes()
                signature = bytes.fromhex("89504e470d0a1a0a" if mime == "image/png" else "ffd8ff")
                if data.startswith(signature):
                    return data
    return (THEME / fallback).read_bytes()


ASSETS = {PREFIX + name: ((THEME / name).read_bytes(), mime) for name, mime in PROGRAMS.items()}
ASSETS.update({PREFIX + name: (artwork(name, *settings), settings[2]) for name, settings in ARTWORK.items()})
PUBLIC_PATHS = frozenset(ASSETS)
ETAGS = {url: '"' + sha256(body).hexdigest() + '"' for url, (body, _) in ASSETS.items()}


@router.api_route("/maimi/web/cosmos/{name}", methods=["GET", "HEAD"])
async def cosmic_asset(request: Request, name: str):
    url = request.url.path
    item = ASSETS.get(url)
    if item is None or request.url.query:
        return Response(status_code=404, headers={"Cache-Control": "no-store"})
    body, mime = item
    headers = {
        "Cache-Control": "public, no-cache" if name in ARTWORK else "public, max-age=31536000, immutable",
        "ETag": ETAGS[url],
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    }
    validators = {value.strip().removeprefix("W/") for value in request.headers.get("if-none-match", "").split(",")}
    if "*" in validators or ETAGS[url] in validators:
        return Response(status_code=304, headers=headers)
    headers["Content-Length"] = str(len(body))
    return Response(body if request.method == "GET" else b"", media_type=mime, headers=headers)
