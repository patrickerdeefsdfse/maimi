# Developer monitor regression tests

Run from the repository root with the gateway Python dependencies and `pytest` installed:

```sh
python -m pytest services/gateway/tests/observability -q
```

The suite contains 48 monitor, collector-adapter, and lifecycle cases plus 31 usage-producer cases. It imports the actual `services/gateway/app` modules, including IAM cookie authentication, the personal model proxy, Office terminal handling, and committed project-task handling. HTTP transports, identities, database replies, clocks, and collector observations are synthetic. No company server, Docker daemon, model account, employee document, or credential is required.

`helpers.py` constructs collector samples directly from synthetic constants. It does not load captured host metadata. SQLite state exists only in memory or test-owned temporary directories. The tests cover denied users and forged identity shapes, revocation, metadata-only validation, missing and stale observations, owner binding, resource trends, source loss, token provenance, stream cancellation, first-terminal deduplication, nonblocking aggregation, and startup/shutdown isolation.

These are local contract and integration checks. Passing does not verify company IAM, deployed collection, real Office conversion, browser rendering, or production capacity.
