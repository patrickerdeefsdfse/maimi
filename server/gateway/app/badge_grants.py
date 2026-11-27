"""Default audience for the reviewed badge release; per-employee decisions take priority."""
BADGE_KEY = 'employee-badges'
BADGE_VERSION = '1.0.0'
BADGE_KIND = 'skill'


def default_badge_grant(key, version, kind, state, catalog_enabled):
    return (key == BADGE_KEY and version == BADGE_VERSION and kind == BADGE_KIND
            and state == 'published' and bool(catalog_enabled))


# Only these source-controlled release constants are interpolated. Caller-supplied
# employee numbers and filters remain parameters. r and p are release and catalog.
DEFAULT_BADGE_SQL = (
    f"(r.plugin_key='{BADGE_KEY}' AND r.version='{BADGE_VERSION}' AND r.kind='{BADGE_KIND}'"
    " AND r.state='published' AND p.enabled)"
)

# The same effective grant gates actions, result reads and job-history rows.
# An explicit false row must remain distinct from a missing grant.
BADGE_GRANT_QUERY = f"""SELECT 1
    FROM platform.plugin_releases r JOIN platform.enterprise_plugin_catalog p USING(plugin_key,version)
    LEFT JOIN platform.enterprise_plugin_grants g
      ON g.plugin_key=r.plugin_key AND g.version=r.version AND g.user_no=%s
    WHERE r.plugin_key='{BADGE_KEY}' AND r.kind='{BADGE_KIND}' AND r.state='published'
      AND p.enabled AND COALESCE(g.enabled, {DEFAULT_BADGE_SQL})"""
