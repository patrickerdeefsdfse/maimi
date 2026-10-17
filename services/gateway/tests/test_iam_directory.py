"""Offline IAM §3 contract tests. No real endpoints, users, or credentials."""
import asyncio
import base64
from dataclasses import replace
import hashlib
import hmac
import json
from pathlib import Path
import sys
import time
import threading
import uuid

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))
from app.iam_directory import (
    AccountAmbiguous, AccountDisabled, AccountUnknown, DirectoryConfig,
    DirectoryProtocolError, DirectoryUnavailable, IamDirectoryClient,
    JsonDirectoryStore, resolve_account, resolve_account_from_snapshot, sync_directory,
    check_snapshot, watch_directory,
)


def org(code="DEPT-A", status=1):
    return {"idt_org__org_code": code, "idt_org__name": "Synthetic " + code,
            "idt_org__status": status, "idt_org__sup_org_code": "", "idt_org__name_path": "/" + code}


def account(no="app-a", work_no="LOCAL-FIXTURE-A", status=1, codes=None):
    return {"app_account__account_no": no, "idt_user__work_no": work_no,
            "idt_user__user_name": "Synthetic User", "app_account__status": status,
            "orgs": [{"idt_org__org_code": code} for code in (codes if codes is not None else ["DEPT-A"])]}


@pytest.fixture
def config(tmp_path):
    secret = tmp_path / "synthetic-secret.txt"
    secret.write_text("synthetic-secret\n", encoding="utf-8")
    return DirectoryConfig(
        org_url="https://iam.invalid/esc-idm/api/v1/org/listAll",
        account_url="https://iam.invalid/esc-idm/api/v1/account/listAll",
        api_key="synthetic-key", api_secret_file=secret,
        snapshot_file=tmp_path / "state" / "directory.json", page_size=2,
    )


def mock_transport(orgs=None, accounts=None, mutate=None):
    calls = []
    dataset = {"org": orgs if orgs is not None else [org()],
               "account": accounts if accounts is not None else [account()]}

    def handler(request):
        kind = request.url.path.split("/")[-2]
        assert request.method == "POST"
        assert request.headers["content-type"] == "application/json"
        data = json.loads(request.content)
        assert set(data) == {"size", "page"}  # No unverified incremental merge.
        assert isinstance(data["size"], str) and isinstance(data["page"], str)
        size, page = int(data["size"]), int(data["page"])
        calls.append(request)
        rows = dataset[kind]
        body = {"code": "0", "msg": "操作成功", "timestamp": "1789650000000",
                "data": {"total": str(len(rows)), "size": size, "page": page,
                         "list": rows[(page - 1) * size:page * size]}}
        if mutate:
            return mutate(kind, page, body, request)
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler), calls


def fetch(config, **kwargs):
    transport, calls = mock_transport(**kwargs)
    return asyncio.run(IamDirectoryClient(config, transport=transport).fetch_snapshot()), calls


def decode_segment(part):
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


def test_pagination_signs_each_request_and_maps_account_to_distinct_work_number(config):
    rows = [account("app-a", "LOCAL-FIXTURE-A", codes=["DEPT-A", "DEPT-C", "DEPT-B"]),
            account("app-b", "LOCAL-FIXTURE-B"), account("app-c", "LOCAL-FIXTURE-C")]
    snapshot, calls = fetch(config, orgs=[org(), org("DEPT-B", "1"), org("DEPT-C", 0)], accounts=rows)
    assert len(calls) == 4
    jwt_ids = []
    for request in calls:
        token = request.headers["authorization"].removeprefix("Bearer ")
        header, claims, signature = token.split(".")
        assert json.loads(decode_segment(header)) == {"alg": "HS256", "typ": "JWT"}
        payload = json.loads(decode_segment(claims))
        assert set(payload) == {"iss", "iat", "jti"}
        assert payload["iss"] == "synthetic-key"
        assert type(payload["iat"]) is int and abs(payload["iat"] - time.time()) < 10
        assert uuid.UUID(payload["jti"]).version == 4
        jwt_ids.append(payload["jti"])
        expected = hmac.new(b"synthetic-secret", f"{header}.{claims}".encode(), hashlib.sha256).digest()
        assert hmac.compare_digest(decode_segment(signature), expected)
    assert len(set(jwt_ids)) == len(calls)
    actor = resolve_account_from_snapshot(snapshot, "app-a")
    assert actor == {"account_no": "app-a", "user_no": "LOCAL-FIXTURE-A",
                     "display_name": "Synthetic User", "organization_codes": ["DEPT-A", "DEPT-B"],
                     "organization_names": {"DEPT-A": "Synthetic DEPT-A", "DEPT-B": "Synthetic DEPT-B"}}
    for wrong in ("LOCAL-FIXTURE-A", "APP-A", " app-a", "app-a "):
        with pytest.raises(AccountUnknown):
            resolve_account_from_snapshot(snapshot, wrong)


@pytest.mark.parametrize("status", [-1, 0, 2, 3, 4, 5, 7, 8, "0", "2", "unknown", None, True, 1.0])
def test_only_documented_enabled_status_can_resolve(config, status):
    snapshot, _ = fetch(config, accounts=[account(status=status)])
    with pytest.raises(AccountDisabled):
        resolve_account_from_snapshot(snapshot, "app-a")


def test_enabled_string_status_and_empty_directory(config):
    snapshot, _ = fetch(config, accounts=[account(status="1")])
    assert resolve_account_from_snapshot(snapshot, "app-a")["user_no"] == "LOCAL-FIXTURE-A"
    empty, calls = fetch(config, orgs=[], accounts=[])
    assert len(calls) == 2 and empty["organizations"] == [] and empty["accounts"] == []


def test_duplicate_account_mapping_rejected_and_previous_generation_kept(config):
    transport, _ = mock_transport()
    initial = asyncio.run(sync_directory(config, transport=transport))
    before = config.snapshot_file.read_bytes()
    transport, _ = mock_transport(accounts=[account(), account(work_no="OTHER-WORK-NO")])
    with pytest.raises(AccountAmbiguous):
        asyncio.run(sync_directory(config, transport=transport))
    assert config.snapshot_file.read_bytes() == before
    assert JsonDirectoryStore(config.snapshot_file).load()["generation"] == initial["generation"]
    initial["accounts"].append(dict(initial["accounts"][0], user_no="OTHER"))
    with pytest.raises(AccountAmbiguous):
        resolve_account_from_snapshot(initial, "app-a")


@pytest.mark.parametrize("fault", ["timeout", "http", "code", "numeric-code", "msg", "page", "total", "short", "json", "data-string"])
def test_failed_last_account_page_never_publishes_partial_snapshot(config, fault):
    good, _ = mock_transport()
    asyncio.run(sync_directory(config, transport=good))
    before = config.snapshot_file.read_bytes()

    def mutate(kind, page, body, request):
        if kind == "account" and page == 2:
            if fault == "timeout":
                raise httpx.ReadTimeout("sensitive upstream diagnostic", request=request)
            if fault == "http":
                return httpx.Response(503, text="sensitive upstream diagnostic")
            if fault == "json":
                return httpx.Response(200, text="not-json")
            if fault == "data-string":
                body["data"] = json.dumps(body["data"])
            elif fault in {"code", "numeric-code", "msg"}:
                body["msg" if fault == "msg" else "code"] = 0 if fault == "numeric-code" else "denied"
            elif fault == "page":
                body["data"]["page"] = 1
            elif fault == "total":
                body["data"]["total"] = "4"
            elif fault == "short":
                body["data"]["list"] = []
        return httpx.Response(200, json=body)

    transport, _ = mock_transport(accounts=[account(), account("app-b", "B"), account("app-c", "C")], mutate=mutate)
    with pytest.raises(DirectoryUnavailable) as failure:
        asyncio.run(sync_directory(config, transport=transport))
    assert "sensitive" not in str(failure.value)
    assert config.snapshot_file.read_bytes() == before
    assert list(config.snapshot_file.parent.glob(".iam-directory-*")) == []


def test_revocation_replaces_old_directory_atomically(config, monkeypatch):
    transport, _ = mock_transport()
    asyncio.run(sync_directory(config, transport=transport))
    monkeypatch.setenv("IAM_DIRECTORY_SNAPSHOT_FILE", str(config.snapshot_file))
    assert asyncio.run(resolve_account("app-a"))["user_no"] == "LOCAL-FIXTURE-A"
    transport, _ = mock_transport(accounts=[account(status=0)])
    asyncio.run(sync_directory(config, transport=transport))
    with pytest.raises(AccountDisabled):
        asyncio.run(resolve_account("app-a"))


def test_snapshot_staleness_and_publication_failure_preserve_last_good(config, monkeypatch):
    snapshot, _ = fetch(config)
    store = JsonDirectoryStore(config.snapshot_file)
    store.publish(snapshot)
    before = config.snapshot_file.read_bytes()
    monkeypatch.setattr("app.iam_directory.os.replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(DirectoryUnavailable):
        store.publish(dict(snapshot, generation=str(uuid.uuid4())))
    assert config.snapshot_file.read_bytes() == before
    assert list(config.snapshot_file.parent.glob(".iam-directory-*")) == []
    monkeypatch.setattr("app.iam_directory.time.time", lambda: snapshot["fetched_at"] + 301)
    with pytest.raises(DirectoryUnavailable):
        store.load()
    monkeypatch.setattr("app.iam_directory.time.time", lambda: snapshot["fetched_at"] - 1)
    with pytest.raises(DirectoryUnavailable):
        store.load()


@pytest.mark.parametrize("bad_kind", ["missing-work-no", "unknown-org", "duplicate-org", "bad-orgs"])
def test_invalid_directory_does_not_publish(config, bad_kind):
    orgs, accounts = [org()], [account()]
    if bad_kind == "missing-work-no":
        accounts[0]["idt_user__work_no"] = None
    elif bad_kind == "unknown-org":
        accounts[0]["orgs"] = [{"idt_org__org_code": "UNKNOWN"}]
    elif bad_kind == "duplicate-org":
        orgs.append(org())
    else:
        accounts[0]["orgs"] = "DEPT-A"
    transport, _ = mock_transport(orgs=orgs, accounts=accounts)
    with pytest.raises(DirectoryProtocolError):
        asyncio.run(sync_directory(config, transport=transport))
    assert not config.snapshot_file.exists()


def test_http_requires_explicit_local_configuration_and_no_default_company_url(config):
    with pytest.raises(DirectoryUnavailable):
        DirectoryConfig.from_env({})
    with pytest.raises(DirectoryUnavailable):
        replace(config, org_url="http://mock-company:8081/esc-idm/api/v1/org/listAll")
    assert replace(config, org_url="http://mock-company:8081/esc-idm/api/v1/org/listAll", allow_http=True)


def test_no_retry_or_redirect_reuses_single_request_token(config):
    transport, calls = mock_transport(mutate=lambda *args: httpx.Response(302, headers={"location": "https://other.invalid/"}))
    with pytest.raises(DirectoryUnavailable):
        asyncio.run(sync_directory(config, transport=transport))
    assert len(calls) == 1


def test_page_bound_and_record_bound_prevent_unbounded_fetch(config):
    rows = [account(), account("app-b", "B"), account("app-c", "C")]
    for bounded in (replace(config, max_pages=1), replace(config, max_records=2)):
        with pytest.raises(DirectoryProtocolError):
            fetch(bounded, accounts=rows)


def test_missing_secret_fails_before_any_request(config):
    config.api_secret_file.unlink()
    transport, calls = mock_transport()
    with pytest.raises(DirectoryUnavailable):
        asyncio.run(sync_directory(config, transport=transport))
    assert calls == []


def test_watch_recovers_from_initial_failure_without_publishing_partial_snapshot(config, monkeypatch, capsys):
    good, _ = mock_transport()
    real_sync = sync_directory
    attempted = []
    waits = []

    async def scenario():
        stop = asyncio.Event()

        async def sync(candidate):
            attempted.append(candidate)
            if len(attempted) == 1:
                assert not candidate.snapshot_file.exists()
                raise DirectoryUnavailable("must not print credentials or rows")
            await real_sync(candidate, transport=good)
            stop.set()

        async def immediate_wait(awaitable, timeout):
            waits.append(timeout)
            awaitable.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr("app.iam_directory.sync_directory", sync)
        monkeypatch.setattr("app.iam_directory.asyncio.wait_for", immediate_wait)
        await watch_directory(config, interval_seconds=120, stop_event=stop)

    asyncio.run(scenario())
    assert len(attempted) == 2 and waits == [120, 120]
    assert JsonDirectoryStore(config.snapshot_file).load()["accounts"][0]["account_no"] == "app-a"
    output = capsys.readouterr().out
    assert "IAM_DIRECTORY_SYNC: FAIL (DirectoryUnavailable)" in output
    assert "IAM_DIRECTORY_SYNC: PASS" in output
    assert "credentials" not in output and "app-a" not in output


@pytest.mark.parametrize("interval", [0, 4, 3601, "nope", True])
def test_watch_rejects_unbounded_or_invalid_interval_before_network(config, interval):
    with pytest.raises(DirectoryUnavailable):
        asyncio.run(watch_directory(config, interval_seconds=interval))


def test_health_check_needs_no_credentials_or_network_and_never_changes_snapshot(config, monkeypatch):
    snapshot, _ = fetch(config)
    JsonDirectoryStore(config.snapshot_file).publish(snapshot)
    monkeypatch.setenv("IAM_DIRECTORY_SNAPSHOT_FILE", str(config.snapshot_file))
    monkeypatch.setenv("IAM_DIRECTORY_SNAPSHOT_MAX_AGE_SECONDS", "300")
    before = config.snapshot_file.read_bytes()
    config.api_secret_file.unlink()
    assert check_snapshot()["generation"] == snapshot["generation"]
    assert config.snapshot_file.read_bytes() == before
    monkeypatch.setattr("app.iam_directory.time.time", lambda: snapshot["fetched_at"] + 301)
    with pytest.raises(DirectoryUnavailable):
        check_snapshot()
    assert config.snapshot_file.read_bytes() == before


@pytest.mark.parametrize("field", ["account", "work", "org", "membership", "parent", "disabled-work"])
@pytest.mark.parametrize("padding", ["before", "after"])
def test_identity_whitespace_never_merges_accounts_or_organizations(config, field, padding):
    def padded(value):
        return " " + value if padding == "before" else value + " "

    orgs, accounts = [org()], [account()]
    if field == "account":
        accounts[0]["app_account__account_no"] = padded("app-a")
    elif field in {"work", "disabled-work"}:
        accounts[0]["idt_user__work_no"] = padded("LOCAL-FIXTURE-A")
        if field == "disabled-work":
            accounts[0]["app_account__status"] = 0
    elif field == "org":
        orgs[0]["idt_org__org_code"] = padded("DEPT-A")
    elif field == "parent":
        orgs[0]["idt_org__sup_org_code"] = padded("PARENT")
    else:
        accounts[0]["orgs"][0]["idt_org__org_code"] = padded("DEPT-A")
    transport, _ = mock_transport(orgs=orgs, accounts=accounts)
    with pytest.raises(DirectoryProtocolError, match="identity whitespace"):
        asyncio.run(sync_directory(config, transport=transport))
    assert not config.snapshot_file.exists()


def test_display_name_whitespace_is_preserved_and_snapshot_identity_guard_is_defensive(config):
    source = account()
    source["idt_user__user_name"] = "  Synthetic User  "
    snapshot, _ = fetch(config, accounts=[source])
    assert resolve_account_from_snapshot(snapshot, "app-a")["display_name"] == "  Synthetic User  "
    snapshot["accounts"][0]["user_no"] += " "
    with pytest.raises(DirectoryProtocolError, match="identity whitespace"):
        resolve_account_from_snapshot(snapshot, "app-a")


def test_entire_snapshot_resolution_runs_outside_the_event_loop(config, monkeypatch):
    transport, _ = mock_transport()
    asyncio.run(sync_directory(config, transport=transport))
    monkeypatch.setenv("IAM_DIRECTORY_SNAPSHOT_FILE", str(config.snapshot_file))
    event_loop_thread = threading.get_ident()
    real_resolve = resolve_account_from_snapshot
    called = []

    def guarded_resolve(snapshot, account_no):
        called.append(threading.get_ident())
        assert threading.get_ident() != event_loop_thread
        return real_resolve(snapshot, account_no)

    monkeypatch.setattr("app.iam_directory.resolve_account_from_snapshot", guarded_resolve)
    assert asyncio.run(resolve_account("app-a"))["user_no"] == "LOCAL-FIXTURE-A"
    assert len(called) == 1
