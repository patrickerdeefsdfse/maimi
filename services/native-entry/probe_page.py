"""Static native-page URL checks only; does not execute browser JavaScript."""
from __future__ import annotations

from html.parser import HTMLParser
import json
import re
from urllib.parse import parse_qsl, urljoin, urlsplit

MAX_HTML_BYTES = 512 * 1024
MAX_URLS = 100
MAX_URL_LENGTH = 8192
SECRET_QUERY_KEYS = {'token', 'api_key', 'access_token', 'refresh_token'}
BOOT_ASSIGNMENT = re.compile(
    r'''(?:window|globalThis)(?:\.__DSH_BOOT__|\[["']__DSH_BOOT__["']\])\s*=\s*''')
ASSET_LINK_RELS = {'stylesheet', 'modulepreload', 'preload'}


class PageCheckError(ValueError):
    """Fixed diagnostic code only; never contains HTML, URL or JSON values."""


def _require(condition: object, code: str) -> None:
    if not condition:
        raise PageCheckError(code)


def _clean_url(value: object, *, allow_empty: bool = False) -> str:
    _require(isinstance(value, str), 'PAGE_URL_TYPE')
    _require(len(value) <= MAX_URL_LENGTH and (bool(value) or allow_empty), 'PAGE_URL_LENGTH')
    _require(not any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 or ch == '\\'
                     for ch in value), 'PAGE_URL_CHARACTERS')
    return value


def _authority(parsed) -> tuple[str, str, int]:
    _require(parsed.scheme == 'https' and bool(parsed.hostname), 'PAGE_URL_SCHEME')
    _require(parsed.username is None and parsed.password is None, 'PAGE_URL_CREDENTIALS')
    return parsed.scheme, parsed.hostname.casefold(), parsed.port or 443


def _resolved(value: object, public_root: str, public_authority, *, base: bool = False):
    raw = _clean_url(value, allow_empty=base)
    result = urlsplit(urljoin(public_root, raw))
    _require(_authority(result) == public_authority, 'PAGE_URL_ORIGIN')
    if not result.path:
        result = result._replace(path='/')
    _require(not any(key.casefold() in SECRET_QUERY_KEYS
                     for key, _ in parse_qsl(result.query, keep_blank_values=True)), 'PAGE_URL_SECRET_QUERY')
    _require(result.path.startswith('/') and not result.path.startswith('//'), 'PAGE_URL_PATH')
    if base:
        _require(result.path == '/' and not result.query and not result.fragment, 'PAGE_BASE_NOT_ROOT')
    return result


class _PageResources(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.assets: list[str] = []
        self.bases: list[str] = []
        self.inline_scripts: list[str] = []
        self._inline: list[str] | None = None

    @staticmethod
    def _attribute(attrs, name):
        found = [value for key, value in attrs if key == name]
        _require(len(found) <= 1, 'PAGE_DUPLICATE_URL_ATTRIBUTE')
        if found:
            _require(isinstance(found[0], str), 'PAGE_URL_TYPE')
        return found[0] if found else None

    def handle_starttag(self, tag, attrs) -> None:
        if tag == 'script':
            src = self._attribute(attrs, 'src')
            if src is None:
                self._inline = []
            else:
                self.assets.append(src)
        elif tag == 'link':
            rel = self._attribute(attrs, 'rel')
            href = self._attribute(attrs, 'href')
            if rel and ASSET_LINK_RELS.intersection(rel.casefold().split()) and href is not None:
                self.assets.append(href)
        elif tag == 'base':
            href = self._attribute(attrs, 'href')
            if href is not None:
                self.bases.append(href)
        _require(len(self.assets) + len(self.bases) <= MAX_URLS, 'PAGE_URL_LIMIT')

    def handle_endtag(self, tag) -> None:
        if tag == 'script' and self._inline is not None:
            self.inline_scripts.append(''.join(self._inline))
            self._inline = None

    def handle_data(self, data) -> None:
        if self._inline is not None:
            self._inline.append(data)


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, 'PAGE_BOOT_DUPLICATE_KEY')
        result[key] = value
    return result


def _non_json_number(_value):
    raise PageCheckError('PAGE_BOOT_JSON')


def _module_urls(scripts: list[str]) -> list[str]:
    matches = [(script, match) for script in scripts for match in BOOT_ASSIGNMENT.finditer(script)]
    _require(len(matches) == 1, 'PAGE_BOOT_ASSIGNMENT_COUNT')
    script, match = matches[0]
    decoder = json.JSONDecoder(object_pairs_hook=_json_object, parse_constant=_non_json_number)
    graph, end = decoder.raw_decode(script, match.end())
    _require(not script[end:].lstrip() or script[end:].lstrip().startswith(';'), 'PAGE_BOOT_JSON_SUFFIX')
    _require(isinstance(graph, dict) and isinstance(graph.get('entries'), list), 'PAGE_BOOT_GRAPH')
    entries = graph['entries']
    _require(all(isinstance(item, dict) for item in entries), 'PAGE_BOOT_ENTRIES')
    batches = graph.get('batches', [])
    _require(isinstance(batches, list) and all(isinstance(item, dict) for item in batches), 'PAGE_BOOT_BATCHES')
    urls = [item['url'] for item in batches if 'url' in item]
    if not urls:
        urls = [item['url'] for item in entries if 'url' in item]
    _require(bool(urls), 'PAGE_MODULE_URL_MISSING')
    _require(len(urls) <= MAX_URLS, 'PAGE_URL_LIMIT')
    return urls


def validated_page_paths(html: str, public_origin: str) -> list[str]:
    """Return deduplicated same-origin asset/module ``/path?query`` strings.

    The verified renderer embeds JSON in its single inline __DSH_BOOT__
    assignment. Batch URLs take precedence over per-entry fallback URLs.
    A root-only base element may be present; this check targets a root entry.
    Passing proves URL shape only, not CSP, resource execution or navigation.
    """
    try:
        _require(isinstance(html, str), 'PAGE_HTML_TYPE')
        _require(len(html.encode('utf-8')) <= MAX_HTML_BYTES, 'PAGE_HTML_LIMIT')
        origin = urlsplit(_clean_url(public_origin))
        _require(origin.path in ('', '/') and not origin.query and not origin.fragment, 'PAGE_PUBLIC_ORIGIN')
        authority = _authority(origin)
        public_root = origin._replace(path='/', query='', fragment='').geturl()
        parser = _PageResources()
        parser.feed(html)
        parser.close()
        urls = parser.assets + _module_urls(parser.inline_scripts)
        _require(len(urls) + len(parser.bases) <= MAX_URLS, 'PAGE_URL_LIMIT')
        for base in parser.bases:
            _resolved(base, public_root, authority, base=True)
        result = []
        for value in urls:
            parsed = _resolved(value, public_root, authority)
            path = parsed.path + ('?' + parsed.query if parsed.query else '')
            if path not in result:
                result.append(path)
        return result
    except PageCheckError:
        raise
    except Exception:
        # json/HTML/url parser errors must not echo source text or URLs.
        raise PageCheckError('PAGE_PARSE_INVALID') from None
