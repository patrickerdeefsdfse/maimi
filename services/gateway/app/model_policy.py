from __future__ import annotations

import json
import os
import threading
import time
from typing import Any


# Platform routes are deliberately stable aliases. The model service resolves
# them to an upstream provider/model and can change that mapping independently.
DEFAULT_CATALOG = {
    "tokenkey": {
        "deepseek-flash-v4.1",
        "agent-default",
        "caoxi-default",
        "liuqinghuai-default",
        "liuqinghuai-ocr",
    }
}


_catalog_lock = threading.Lock()
_catalog_cache: tuple[float, dict[str, set[str]]] | None = None


def _env_catalog() -> dict[str, set[str]]:
    raw = os.getenv("MODEL_CATALOG_JSON", "").strip()
    if not raw:
        return DEFAULT_CATALOG
    try:
        parsed = json.loads(raw)
        value = {
            str(provider): {str(model) for model in models}
            for provider, models in parsed.items()
            if isinstance(models, list)
        }
        return value or DEFAULT_CATALOG
    except (TypeError, ValueError):
        return DEFAULT_CATALOG


def _db_catalog() -> dict[str, set[str]] | None:
    """Read enabled provider/model pairs without exposing secret metadata."""
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        return None
    try:
        import psycopg

        with psycopg.connect(url) as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT c.provider_key, c.model_key
                  FROM platform.model_catalog c
                  JOIN platform.model_providers p
                    ON p.provider_key = c.provider_key
                   AND p.enabled = TRUE
                 WHERE c.enabled = TRUE
                """
            )
            rows = cur.fetchall()
    except Exception:
        return None
    result: dict[str, set[str]] = {}
    for provider, model in rows:
        result.setdefault(str(provider), set()).add(str(model))
    return result


def _catalog() -> dict[str, set[str]]:
    global _catalog_cache
    try:
        ttl = max(0.0, float(os.getenv("MODEL_CATALOG_CACHE_SECONDS", "15")))
    except ValueError:
        ttl = 15.0
    now = time.monotonic()
    with _catalog_lock:
        if _catalog_cache is not None and now - _catalog_cache[0] < ttl:
            return _catalog_cache[1]
        db_catalog = _db_catalog()
        catalog = _env_catalog() if db_catalog is None else db_catalog
        _catalog_cache = (now, catalog)
        return catalog


def is_allowed(provider_key: str, model: str) -> bool:
    return model in _catalog().get(provider_key, set())


def invalidate_cache() -> None:
    global _catalog_cache
    with _catalog_lock:
        _catalog_cache = None


def list_catalog() -> list[dict[str, Any]]:
    """Return model aliases for a picker; never return secret_ref/base_url."""
    catalog = _catalog()
    return [
        {"provider_key": provider, "model_key": model}
        for provider in sorted(catalog)
        for model in sorted(catalog[provider])
    ]
