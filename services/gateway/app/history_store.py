from __future__ import annotations

import os
from typing import Any


def record_chat_turn(
    *,
    user_no: str,
    organization_code: str,
    client_conversation_id: str,
    input_text: str,
    assistant_text: str,
    provider_key: str,
    model: str,
    route_mode: str = "auto",
    project_key: str | None = None,
    plugin_key: str | None = None,
) -> str:
    """Persist one completed chat turn and return the internal conversation id.

    The client supplied conversation id is deliberately stored separately from
    the UUID primary key. This keeps browser/session ids stable while retaining
    database-native foreign keys for messages and future agent runs.
    """
    user_no = user_no.strip()
    organization_code = organization_code.strip()
    client_conversation_id = client_conversation_id.strip()
    if not user_no or not client_conversation_id:
        raise ValueError("user_no and client_conversation_id are required")
    if client_conversation_id in {".", ".."} or any(
        char in client_conversation_id for char in "/\\"
    ):
        raise ValueError("client_conversation_id must be path-safe")

    from psycopg.types.json import Jsonb

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO platform.users (external_user_no, display_name)
            VALUES (%s, %s)
            ON CONFLICT (external_user_no) DO UPDATE
              SET display_name = CASE
                    WHEN EXCLUDED.display_name <> ''
                    THEN EXCLUDED.display_name
                    ELSE platform.users.display_name
                  END
            RETURNING id
            """,
            (user_no, ""),
        )
        user_id = cur.fetchone()["id"]

        organization_id = None
        if organization_code:
            cur.execute(
                """
                INSERT INTO platform.organizations (code, name)
                VALUES (%s, %s)
                ON CONFLICT (code) DO UPDATE
                  SET name = CASE
                        WHEN EXCLUDED.name <> ''
                        THEN EXCLUDED.name
                        ELSE platform.organizations.name
                      END
                RETURNING id
                """,
                (organization_code, organization_code),
            )
            organization_id = cur.fetchone()["id"]

        project_id = None
        if organization_id and project_key:
            cur.execute(
                """
                SELECT id
                  FROM platform.projects
                 WHERE organization_id = %s
                   AND project_key = %s
                   AND enabled = TRUE
                """,
                (organization_id, project_key.strip()),
            )
            project = cur.fetchone()
            project_id = project["id"] if project else None
        if project_key and project_id is None:
            raise ValueError("project scope is unavailable")

        cur.execute(
            """
            INSERT INTO platform.conversations
                (client_conversation_id, organization_id, user_id,
                 provider_key, model, route_mode, project_id, plugin_key,
                 title, last_activity_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, now())
            ON CONFLICT (user_id, client_conversation_id)
                WHERE client_conversation_id IS NOT NULL DO UPDATE
              SET provider_key = EXCLUDED.provider_key,
                  model = EXCLUDED.model,
                  last_activity_at = now(),
                  updated_at = now()
              WHERE (
                  platform.conversations.project_id IS NULL
                  AND COALESCE(platform.conversations.plugin_key, '') = ''
                  AND platform.conversations.route_mode IN ('auto','dsh')
                  AND EXCLUDED.project_id IS NULL AND COALESCE(EXCLUDED.plugin_key, '') = ''
                  AND EXCLUDED.route_mode IN ('auto','dsh')
              ) OR (
                  platform.conversations.organization_id IS NOT DISTINCT FROM EXCLUDED.organization_id
                  AND platform.conversations.project_id IS NOT DISTINCT FROM EXCLUDED.project_id
                  AND platform.conversations.plugin_key IS NOT DISTINCT FROM EXCLUDED.plugin_key
                  AND platform.conversations.route_mode = EXCLUDED.route_mode
              )
            RETURNING id
            """,
            (
                client_conversation_id,
                organization_id,
                user_id,
                provider_key,
                model,
                route_mode,
                project_id,
                plugin_key,
                input_text.strip()[:500],
            ),
        )
        persisted = cur.fetchone()
        if persisted is None:
            raise ValueError("conversation scope is immutable")
        conversation_id = persisted["id"]

        if input_text:
            cur.execute(
                """
                INSERT INTO platform.messages (conversation_id, role, content)
                VALUES (%s, 'user', %s)
                """,
                (conversation_id, Jsonb({"text": input_text})),
            )
        if assistant_text:
            cur.execute(
                """
                INSERT INTO platform.messages (conversation_id, role, content)
                VALUES (%s, 'assistant', %s)
                """,
                (conversation_id, Jsonb({"text": assistant_text})),
            )
        return str(conversation_id)


def _connect():
    import psycopg
    from psycopg.rows import dict_row

    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(url, row_factory=dict_row)


def assert_personal_conversation(*, user_no: str, client_conversation_id: str) -> None:
    """Check before invoking a personal runtime; never reuse enterprise state."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("""SELECT c.project_id,c.plugin_key,c.route_mode FROM platform.conversations c
                       JOIN platform.users u ON u.id=c.user_id
                       WHERE u.external_user_no=%s AND c.client_conversation_id=%s""",
                    (user_no, client_conversation_id))
        row = cur.fetchone()
        if row and (row["project_id"] is not None or row["plugin_key"]
                    or row["route_mode"] not in {"auto", "dsh"}):
            raise ValueError("conversation scope is immutable")


def _organization_scope(personal_across_organizations: bool) -> str:
    if personal_across_organizations:
        # Only ordinary personal conversations travel with the stable employee
        # owner. Enterprise/project/plugin rows still require the current org;
        # an empty or unresolved primary org grants no enterprise history.
        return """(o.code = %s OR
            (c.project_id IS NULL AND COALESCE(c.plugin_key, '') = ''
             AND c.route_mode IN ('auto', 'dsh')))"""
    return "(%s = '' OR o.code = %s)"


def list_conversations(*, user_no: str, organization_code: str, limit: int = 100,
                       personal_across_organizations: bool = False) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 200))
    query = """
        SELECT COALESCE(c.client_conversation_id, c.id::text) AS conversation_id,
               c.title,
               CASE WHEN c.route_mode = 'enterprise' OR c.project_id IS NOT NULL
                    THEN 'enterprise_capability' ELSE 'dsh_chat' END AS route,
               p.project_key, p.display_name AS project_name,
               c.last_activity_at, c.expires_at
          FROM platform.conversations c
          JOIN platform.users u ON u.id = c.user_id
          LEFT JOIN platform.organizations o ON o.id = c.organization_id
          LEFT JOIN platform.projects p ON p.id = c.project_id
         WHERE u.external_user_no = %s
           AND {organization_scope}
           AND c.archived_at IS NULL
         ORDER BY c.last_activity_at DESC
         LIMIT %s
    """.format(organization_scope=_organization_scope(personal_across_organizations))
    scope_params = (organization_code,) if personal_across_organizations else (organization_code, organization_code)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(query, (user_no, *scope_params, limit))
        return [dict(row) for row in cur.fetchall()]


def read_conversation(*, user_no: str, organization_code: str, conversation_id: str,
                      personal_across_organizations: bool = False) -> dict[str, Any] | None:
    query = """
        SELECT COALESCE(c.client_conversation_id, c.id::text) AS conversation_id,
               c.id::text AS _internal_id, c.title, c.route_mode,
               c.plugin_key, p.project_key, p.display_name AS project_name,
               c.provider_key, c.model, c.last_activity_at, c.expires_at,
               c.created_at
          FROM platform.conversations c
          JOIN platform.users u ON u.id = c.user_id
          LEFT JOIN platform.organizations o ON o.id = c.organization_id
          LEFT JOIN platform.projects p ON p.id = c.project_id
         WHERE (c.client_conversation_id = %s OR c.id::text = %s)
           AND u.external_user_no = %s
           AND {organization_scope}
           AND c.archived_at IS NULL
    """.format(organization_scope=_organization_scope(personal_across_organizations))
    scope_params = (organization_code,) if personal_across_organizations else (organization_code, organization_code)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            query,
            (conversation_id, conversation_id, user_no, *scope_params),
        )
        row = cur.fetchone()
        if row is None:
            return None
        conversation = dict(row)
        internal_id = conversation.pop("_internal_id")
        cur.execute(
            """
            SELECT id::text AS message_id, role, content, created_at
              FROM platform.messages
             WHERE conversation_id = %s::uuid
             ORDER BY created_at ASC, id ASC
            """,
            (internal_id,),
        )
        conversation["messages"] = [dict(message) for message in cur.fetchall()]
        return conversation


def purge_expired(*, batch_size: int = 500) -> int:
    """Delete expired conversations; the caller records the cleanup audit event."""
    query = """
        WITH candidates AS (
            SELECT c.id
              FROM platform.conversations c
              LEFT JOIN platform.conversation_retention_overrides r
                ON r.conversation_id = c.id
             WHERE c.archived_at IS NULL
               AND COALESCE(r.legal_hold, FALSE) = FALSE
               AND COALESCE(r.cleanup_enabled, TRUE) = TRUE
               AND c.expires_at IS NOT NULL
               AND c.expires_at <= now()
             ORDER BY c.expires_at
             LIMIT %s
             FOR UPDATE SKIP LOCKED
        ), deleted AS (
            DELETE FROM platform.conversations c
             USING candidates x
             WHERE c.id = x.id
             RETURNING c.id
        )
        SELECT count(*)::int AS deleted_count FROM deleted
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(query, (max(1, min(batch_size, 5000)),))
        return int(cur.fetchone()["deleted_count"])
