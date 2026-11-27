from __future__ import annotations

from typing import Any
from uuid import UUID


class AccessDenied(PermissionError):
    """The authenticated actor is not allowed to use an enterprise resource."""


def _connect():
    from .history_store import _connect as connect

    return connect()


def list_projects(*, user_no: str, organization_code: str) -> list[dict[str, Any]]:
    """Return enabled projects for which the SSO user has membership."""
    user_no = user_no.strip()
    organization_code = organization_code.strip()
    if not user_no or not organization_code:
        return []
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.project_key, p.display_name, p.description,
                   p.default_provider_key, p.default_model_key,
                   pm.role_key
              FROM platform.project_members pm
              JOIN platform.projects p ON p.id = pm.project_id
              JOIN platform.organizations o ON o.id = p.organization_id
              JOIN platform.users u ON u.id = pm.user_id
             WHERE u.external_user_no = %s
               AND o.code = %s
               AND p.enabled = TRUE
             ORDER BY p.display_name, p.project_key
            """,
            (user_no, organization_code),
        )
        return [dict(row) for row in cur.fetchall()]


def authorize_enterprise_context(
    *,
    user_no: str,
    organization_code: str,
    project_key: str | None,
    plugin_key: str | None,
    file_ids: list[str],
) -> dict[str, Any]:
    """Validate project, plugin and file ownership before enterprise routing.

    The browser may select a project, but it cannot grant itself membership.
    Ordinary DSH conversations do not call this function.
    """
    project_key = (project_key or "").strip()
    plugin_key = (plugin_key or "").strip()
    file_ids = [value.strip() for value in file_ids]
    if not project_key:
        raise AccessDenied("project context is required for enterprise access")
    if not user_no.strip() or not organization_code.strip():
        raise AccessDenied("authenticated organization context is required")

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.id::text AS project_id, p.project_key,
                   p.display_name AS project_name, pm.role_key
              FROM platform.project_members pm
              JOIN platform.projects p ON p.id = pm.project_id
              JOIN platform.organizations o ON o.id = p.organization_id
              JOIN platform.users u ON u.id = pm.user_id
             WHERE u.external_user_no = %s
               AND o.code = %s
               AND p.project_key = %s
               AND p.enabled = TRUE
            """,
            (user_no.strip(), organization_code.strip(), project_key),
        )
        project = cur.fetchone()
        if project is None:
            raise AccessDenied("project access denied")

        if plugin_key:
            cur.execute(
                """
                SELECT 1
                  FROM platform.project_plugins pp
                  JOIN platform.plugins pl ON pl.id = pp.plugin_id
                 WHERE pp.project_id = %s::uuid
                   AND pl.plugin_key = %s
                   AND pp.enabled = TRUE
                   AND pl.enabled = TRUE
                """,
                (project["project_id"], plugin_key),
            )
            if cur.fetchone() is None:
                raise AccessDenied("plugin access denied")

        for file_id in file_ids:
            try:
                normalized_file_id = str(UUID(file_id))
            except (ValueError, AttributeError):
                raise AccessDenied("file access denied") from None
            try:
                cur.execute(
                    """
                    SELECT 1
                      FROM platform.documents d
                      JOIN platform.project_knowledge_bases pk
                        ON pk.document_id = d.id
                     WHERE pk.project_id = %s::uuid
                       AND d.id = %s::uuid
                       AND d.organization_id = (
                           SELECT organization_id FROM platform.projects
                            WHERE id = %s::uuid
                       )
                    UNION ALL
                    SELECT 1
                      FROM platform.artifacts a
                      JOIN platform.users u ON u.id = a.owner_user_id
                     WHERE a.id = %s::uuid
                       AND a.organization_id = (
                           SELECT organization_id FROM platform.projects
                            WHERE id = %s::uuid
                       )
                       AND u.external_user_no = %s
                    LIMIT 1
                    """,
                    (
                        project["project_id"],
                        normalized_file_id,
                        project["project_id"],
                        normalized_file_id,
                        project["project_id"],
                        user_no.strip(),
                    ),
                )
            except (ValueError, TypeError):
                raise AccessDenied("file access denied") from None
            if cur.fetchone() is None:
                raise AccessDenied("file access denied")

    return dict(project)
