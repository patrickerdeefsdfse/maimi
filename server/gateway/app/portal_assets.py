"""Version and compress the five public portal stylesheets and scripts."""
import gzip
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

from fastapi.responses import Response


APP = Path(__file__).parent
PLUGIN = "/maimi/plugins/assets/"
MONITOR = "/maimi/monitor/assets/"
FILES = {**{PLUGIN + name: APP / "plugin_admin_assets" / name
            for name in ("app.css", "app.js", "entry.css")},
         **{MONITOR + name: APP / "observability" / "cluster-assets" / name
            for name in ("app.css", "app.js")}}


@lru_cache(maxsize=len(FILES))
def representations(url):
    data = FILES[url].read_bytes()
    return sha256(data).hexdigest()[:24], data, gzip.compress(data, compresslevel=6, mtime=0)


def version_links(html):
    for url in FILES:
        target = '"' + url + '"'
        if target in html:
            version, _, _ = representations(url)
            html = html.replace(target, f'"{url}?v={version}"')
    return html


def accepts_gzip(header):
    preferences = {}
    for coding in header.lower().split(","):
        token, _, parameter = coding.strip().partition(";")
        weight = parameter.strip().removeprefix("q=").strip() if parameter else "1"
        try:
            preferences[token.strip()] = 0 < float(weight) <= 1
        except ValueError:
            preferences[token.strip()] = False
    return preferences.get("gzip", preferences.get("*", False))


def asset_response(request, url, media_type, headers):
    version, raw, compressed = representations(url)
    supplied_version = request.query_params.get("v")
    policy = ("public, max-age=31536000, immutable" if supplied_version == version
              else "no-store" if supplied_version is not None else "public, no-cache")
    etag = f'W/"{version}"'
    # The monitor data API disables caching. These immutable public assets share
    # its security headers, but browsers honor its legacy Pragma independently.
    headers = {key: value for key, value in headers.items() if key.lower() != "pragma"}
    headers = {**headers, "Cache-Control": policy, "Vary": "Accept-Encoding", "ETag": etag}
    validators = [value.strip().removeprefix("W/") for value in request.headers.get("if-none-match", "").split(",")]
    if "*" in validators or etag[2:] in validators:
        return Response(status_code=304, headers=headers)
    if accepts_gzip(request.headers.get("accept-encoding", "")):
        headers["Content-Encoding"] = "gzip"
        return Response(compressed, media_type=media_type, headers=headers)
    return Response(raw, media_type=media_type, headers=headers)
