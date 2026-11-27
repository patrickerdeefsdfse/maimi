CREATE TABLE IF NOT EXISTS platform.plugin_releases (
    plugin_key varchar(160) NOT NULL,
    version varchar(80) NOT NULL,
    kind text NOT NULL CHECK (kind IN ('skill','native')),
    sha256 char(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    archive bytea NOT NULL CHECK (octet_length(archive) BETWEEN 1 AND 4194304),
    instructions text,
    state text NOT NULL DEFAULT 'draft' CHECK (state IN ('draft','published','withdrawn')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (plugin_key,version),
    FOREIGN KEY (plugin_key,version) REFERENCES platform.enterprise_plugin_catalog(plugin_key,version),
    CHECK ((kind='skill') = (instructions IS NOT NULL))
);
CREATE TABLE IF NOT EXISTS platform.plugin_receipts (
    employee varchar(100) NOT NULL,
    client text NOT NULL CHECK (client IN ('web','desktop')),
    plugin_key varchar(160) NOT NULL,
    version varchar(80) NOT NULL,
    status text NOT NULL CHECK(status IN ('installed','removed','failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY(employee,client,plugin_key,version),
    FOREIGN KEY(plugin_key,version) REFERENCES platform.plugin_releases(plugin_key,version)
);
CREATE TABLE IF NOT EXISTS platform.plugin_release_events (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    actor varchar(100) NOT NULL,
    employee varchar(100),
    plugin_key varchar(160) NOT NULL,
    version varchar(80) NOT NULL,
    action text NOT NULL,
    client text,
    status text
);
CREATE INDEX IF NOT EXISTS plugin_release_events_employee ON platform.plugin_release_events(employee,id DESC);
GRANT SELECT, INSERT, UPDATE ON platform.plugin_releases, platform.plugin_receipts TO maimi_app;
GRANT SELECT, INSERT ON platform.plugin_release_events TO maimi_app;
GRANT USAGE ON SEQUENCE platform.plugin_release_events_id_seq TO maimi_app;
