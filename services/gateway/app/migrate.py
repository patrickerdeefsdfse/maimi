"""Apply PostgreSQL migrations exactly once and record their checksums.

The postgres image's ``docker-entrypoint-initdb.d`` hook only runs for a new
volume.  This small one-shot service is therefore the source of truth for
upgrades on an existing volume as well.
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path

import psycopg


logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger("megmeet.migrations")


def _migration_files() -> list[Path]:
    root = Path(os.getenv("MIGRATIONS_DIR", "/app/migrations"))
    if not root.is_dir():
        raise RuntimeError(f"migration directory is unavailable: {root}")
    return sorted(path for path in root.glob("*.sql") if path.is_file())


def run() -> int:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    files = _migration_files()
    if not files:
        raise RuntimeError("no SQL migrations found")

    with psycopg.connect(url) as conn:
        # Serialize deploys from two Compose invocations.  The lock is held
        # only for this process and is released automatically on disconnect.
        conn.execute("SELECT pg_advisory_lock(hashtext('megmeet.schema_migrations'))")
        conn.execute("CREATE SCHEMA IF NOT EXISTS platform")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS platform.schema_migrations (
                version TEXT PRIMARY KEY,
                checksum CHAR(64) NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )
            """
        )

        applied = {
            row[0]: row[1]
            for row in conn.execute(
                "SELECT version, checksum FROM platform.schema_migrations"
            )
        }
        for path in files:
            version = path.name
            sql = path.read_text(encoding="utf-8")
            checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
            previous = applied.get(version)
            if previous is not None:
                if previous != checksum:
                    raise RuntimeError(f"migration checksum mismatch: {version}")
                logger.info("migration already applied: %s", version)
                continue

            logger.info("applying migration: %s", version)
            with conn.transaction():
                conn.execute(sql)
                conn.execute(
                    "INSERT INTO platform.schema_migrations (version, checksum) VALUES (%s, %s)",
                    (version, checksum),
                )
            applied[version] = checksum

    logger.info("migration check complete: applied=%d total=%d", len(applied), len(files))
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
