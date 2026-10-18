# Exact pre-r65 ready function; decorators excluded for isolated regression.
def ready() -> dict[str, object]:
    """Return readiness only when migrations and runtime dependencies work."""
    failures: list[str] = []
    try:
        from .history_store import _connect

        with _connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT to_regclass('platform.schema_migrations'),
                       to_regclass('platform.users'),
                       to_regclass('platform.conversations'),
                       to_regclass('platform.messages')
                """
            )
            if any(value is None for value in cur.fetchone()):
                failures.append("database_migrations")
            else:
                required = os.getenv("MIGRATIONS_REQUIRED_VERSION", "005_history_write.sql")
                cur.execute(
                    "SELECT 1 FROM platform.schema_migrations WHERE version = %s",
                    (required,),
                )
                if cur.fetchone() is None:
                    failures.append("database_migrations")
    except Exception:
        failures.append("database")

    try:
        import redis

        client = redis.from_url(os.getenv("REDIS_URL", ""), socket_timeout=2)
        client.ping()
        client.close()
    except Exception:
        failures.append("redis")

    if failures:
        raise HTTPException(status_code=503, detail="dependencies not ready")
    return {"status": "ready", "database": "ready", "redis": "ready"}
