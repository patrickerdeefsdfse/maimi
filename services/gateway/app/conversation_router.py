from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reason: str
    enterprise_context_required: bool


def decide_route(
    *,
    route_mode: str = "auto",
    project_key: str | None = None,
    plugin_key: str | None = None,
    file_ids: list[str] | None = None,
) -> RouteDecision:
    """Resolve automatic routing while keeping explicit enterprise context safe."""
    if route_mode not in {"auto", "dsh", "enterprise"}:
        raise ValueError("route_mode must be auto, dsh, or enterprise")
    # Attachments have their own scope. A personal attachment must not silently
    # select an enterprise task; only an explicit project/plugin can do that.
    has_enterprise_context = bool(project_key or plugin_key)
    if route_mode == "enterprise" and not has_enterprise_context:
        raise ValueError("enterprise route requires an explicit project or plugin")
    if has_enterprise_context or route_mode == "enterprise":
        return RouteDecision(
            route="enterprise_capability",
            reason=("user-selected enterprise route" if route_mode == "enterprise"
                    else "explicit enterprise project or plugin"),
            enterprise_context_required=True,
        )
    return RouteDecision(
        route="dsh_chat",
        reason=("user-selected DSH route" if route_mode == "dsh"
                else "automatic ordinary conversation default"),
        enterprise_context_required=False,
    )
