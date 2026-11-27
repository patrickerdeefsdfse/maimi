"""IAM §3 full directory synchronization and account-to-employee resolution.

This module does not provide an HTTP route or start a scheduler. All endpoints
must be configured by the operator. It never contacts IAM during import.
"""
from __future__ import annotations

import asyncio
import argparse
import base64
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import signal
import ssl
import tempfile
import time
from typing import Any, Mapping
from urllib.parse import urlsplit
import uuid

import httpx


class DirectoryError(RuntimeError):
    """A sanitized failure; never includes credentials or directory rows."""


class DirectoryUnavailable(DirectoryError):
    pass


class DirectoryProtocolError(DirectoryUnavailable):
    pass


class AccountUnknown(DirectoryError):
    pass


class AccountDisabled(DirectoryError):
    pass


class AccountAmbiguous(DirectoryError):
    pass


def _integer(value: Any, *, minimum: int = 0) -> int:
    # bool is an int subclass; accepting True as status=1 would enable users.
    if type(value) is int:
        result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        result = int(value)
    else:
        raise DirectoryProtocolError("directory integer field invalid")
    if result < minimum:
        raise DirectoryProtocolError("directory integer field out of range")
    return result


def _text(value: Any, *, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise DirectoryProtocolError("directory text field invalid")
    # Identity keys are exact, case-sensitive strings, never stripped/coerced.
    if len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise DirectoryProtocolError("directory text field invalid")
    return value


def _active(value: Any) -> bool:
    return (type(value) is int and value == 1) or (type(value) is str and value == "1")


def _identity(value: Any, *, required: bool = True) -> str:
    result = _text(value, required=required)
    # Other platform layers trim identity keys; reject rather than silently merge
    # two upstream keys that would collapse to the same history/workspace owner.
    if result != result.strip():
        raise DirectoryProtocolError("directory identity whitespace invalid")
    return result


def _flag(value: str) -> bool:
    if value.lower() not in {"true", "false"}:
        raise DirectoryUnavailable("directory boolean configuration invalid")
    return value.lower() == "true"


@dataclass(frozen=True)
class DirectoryConfig:
    org_url: str
    account_url: str
    api_key: str
    api_secret_file: Path
    snapshot_file: Path
    page_size: int = 1000
    max_pages: int = 1000
    max_records: int = 100000
    timeout_seconds: float = 10.0
    allow_http: bool = False
    ca_file: str | None = None

    def __post_init__(self) -> None:
        for endpoint in (self.org_url, self.account_url):
            parsed = urlsplit(endpoint)
            if (parsed.scheme not in ({"https", "http"} if self.allow_http else {"https"})
                    or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise DirectoryUnavailable("directory endpoint configuration invalid")
        if not self.api_key or any(c.isspace() for c in self.api_key):
            raise DirectoryUnavailable("directory API issuer configuration missing or invalid")
        if (type(self.page_size) is not int or not 1 <= self.page_size <= 10000
                or type(self.max_pages) is not int or not 1 <= self.max_pages <= 10000
                or type(self.max_records) is not int or self.max_records < 1
                or not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0):
            raise DirectoryUnavailable("directory bounds configuration invalid")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> DirectoryConfig:
        env = os.environ if environ is None else environ
        names = ("ORG_URL", "ACCOUNT_URL", "API_KEY", "API_SECRET_FILE", "SNAPSHOT_FILE")
        values = [env.get("IAM_DIRECTORY_" + name, "") for name in names]
        if not all(values):
            raise DirectoryUnavailable("directory synchronization configuration incomplete")
        try:
            return cls(
                org_url=values[0], account_url=values[1], api_key=values[2],
                api_secret_file=Path(values[3]), snapshot_file=Path(values[4]),
                page_size=int(env.get("IAM_DIRECTORY_PAGE_SIZE", "1000")),
                max_pages=int(env.get("IAM_DIRECTORY_MAX_PAGES", "1000")),
                max_records=int(env.get("IAM_DIRECTORY_MAX_RECORDS", "100000")),
                timeout_seconds=float(env.get("IAM_DIRECTORY_TIMEOUT_SECONDS", "10")),
                allow_http=_flag(env.get("IAM_DIRECTORY_ALLOW_HTTP", "false")),
                ca_file=env.get("IAM_DIRECTORY_CA_FILE") or None,
            )
        except (ValueError, TypeError):
            raise DirectoryUnavailable("directory synchronization configuration invalid") from None


def _secret(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65536:
            raise DirectoryUnavailable("directory secret file invalid")
        value = path.read_bytes().rstrip(b"\r\n")
    except OSError:
        raise DirectoryUnavailable("directory secret file unavailable") from None
    if not value:
        raise DirectoryUnavailable("directory secret file empty")
    return value


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _authorization(api_key: str, secret: bytes) -> str:
    # java-jwt withIssuedAt maps to the registered iat claim, not issuedAt.
    # IAM permits a token for one minute and ONE request. Never cache this token.
    header = _b64(b'{"alg":"HS256","typ":"JWT"}')
    payload = _b64(json.dumps({
        "iss": api_key, "iat": int(time.time()), "jti": str(uuid.uuid4()),
    }, separators=(",", ":")).encode("utf-8"))
    unsigned = f"{header}.{payload}"
    signature = _b64(hmac.new(secret, unsigned.encode("ascii"), hashlib.sha256).digest())
    return f"Bearer {unsigned}.{signature}"


def _normalize(org_rows: list[dict], account_rows: list[dict]) -> tuple[list[dict], list[dict]]:
    organizations: dict[str, dict] = {}
    for row in org_rows:
        code = _identity(row.get("idt_org__org_code"))
        if code in organizations:
            raise DirectoryProtocolError("duplicate organization code")
        organizations[code] = {
            "code": code,
            "name": _text(row.get("idt_org__name")),
            "parent_code": _identity(row.get("idt_org__sup_org_code"), required=False),
            "name_path": _text(row.get("idt_org__name_path")),
            "active": _active(row.get("idt_org__status")),
        }
    accounts: dict[str, dict] = {}
    for row in account_rows:
        account_no = _identity(row.get("app_account__account_no"))
        if account_no in accounts:
            # A conflict must never pick the first row or publish a partial map.
            raise AccountAmbiguous("duplicate application account mapping")
        enabled = _active(row.get("app_account__status"))
        user_no = _identity(row.get("idt_user__work_no"), required=enabled)
        orgs = row.get("orgs", [])
        if not isinstance(orgs, list) or any(not isinstance(org, dict) for org in orgs):
            raise DirectoryProtocolError("account organizations invalid")
        codes = sorted({_identity(org.get("idt_org__org_code")) for org in orgs})
        if enabled and any(code not in organizations for code in codes):
            raise DirectoryProtocolError("active account organization unresolved")
        accounts[account_no] = {
            "account_no": account_no, "user_no": user_no,
            "display_name": _text(row.get("idt_user__user_name")),
            "active": enabled, "organization_codes": codes,
        }
    return list(organizations.values()), list(accounts.values())


class IamDirectoryClient:
    def __init__(self, config: DirectoryConfig, *, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport

    async def _pages(self, client: httpx.AsyncClient, url: str, secret: bytes) -> tuple[list[dict], list[int]]:
        records: list[dict] = []
        timestamps: list[int] = []
        total: int | None = None
        size = self.config.page_size
        for page in range(1, self.config.max_pages + 1):
            try:
                response = await client.post(
                    url, headers={"Authorization": _authorization(self.config.api_key, secret)},
                    json={"size": str(size), "page": str(page)},
                )
                if response.status_code != 200:
                    raise DirectoryUnavailable("directory HTTP request failed")
                body = response.json()
            except (httpx.HTTPError, ValueError):
                raise DirectoryUnavailable("directory request or response unavailable") from None
            if not isinstance(body, dict) or body.get("code") != "0" or body.get("msg") != "操作成功":
                raise DirectoryProtocolError("directory upstream operation rejected")
            data = body.get("data")
            if not isinstance(data, dict):
                raise DirectoryProtocolError("directory data envelope invalid")
            current_total = _integer(data.get("total"))
            if (current_total > self.config.max_records
                    or _integer(data.get("page"), minimum=1) != page
                    or _integer(data.get("size"), minimum=1) != size
                    or (total is not None and current_total != total)):
                raise DirectoryProtocolError("directory pagination inconsistent or limit exceeded")
            total = current_total
            rows = data.get("list")
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise DirectoryProtocolError("directory list invalid")
            expected = min(size, total - len(records))
            if len(rows) != expected:
                raise DirectoryProtocolError("directory page incomplete")
            timestamps.append(_integer(body.get("timestamp"), minimum=1))
            records.extend(rows)
            if len(records) == total:
                return records, timestamps
        raise DirectoryProtocolError("directory page limit exceeded")

    async def fetch_snapshot(self) -> dict[str, Any]:
        secret = _secret(self.config.api_secret_file)
        try:
            verify: bool | ssl.SSLContext = (
                ssl.create_default_context(cafile=self.config.ca_file) if self.config.ca_file else True
            )
            async with httpx.AsyncClient(
                transport=self.transport, timeout=self.config.timeout_seconds,
                verify=verify, trust_env=False, follow_redirects=False,
            ) as client:
                orgs, org_times = await self._pages(client, self.config.org_url, secret)
                accounts, account_times = await self._pages(client, self.config.account_url, secret)
        except (OSError, ssl.SSLError):
            raise DirectoryUnavailable("directory TLS or transport configuration failed") from None
        normalized_orgs, normalized_accounts = _normalize(orgs, accounts)
        return {
            "schema_version": 1, "generation": str(uuid.uuid4()),
            "fetched_at": int(time.time()),
            "source_timestamps": {"organizations": org_times, "accounts": account_times},
            "organizations": normalized_orgs, "accounts": normalized_accounts,
        }


def _validate_snapshot(snapshot: Any) -> None:
    if not isinstance(snapshot, dict) or type(snapshot.get("schema_version")) is not int or snapshot["schema_version"] != 1:
        raise DirectoryProtocolError("directory snapshot version invalid")
    if type(snapshot.get("fetched_at")) is not int:
        raise DirectoryProtocolError("directory snapshot time invalid")
    _integer(snapshot["fetched_at"], minimum=1)
    try:
        uuid.UUID(snapshot["generation"])
    except (KeyError, ValueError, TypeError, AttributeError):
        raise DirectoryProtocolError("directory snapshot generation invalid") from None
    organizations = snapshot.get("organizations")
    accounts = snapshot.get("accounts")
    if not isinstance(organizations, list) or not isinstance(accounts, list):
        raise DirectoryProtocolError("directory snapshot lists invalid")
    codes: set[str] = set()
    for org in organizations:
        if not isinstance(org, dict) or type(org.get("active")) is not bool:
            raise DirectoryProtocolError("directory snapshot organization invalid")
        code = _identity(org.get("code"))
        _identity(org.get("parent_code"), required=False)
        _text(org.get("name"))
        if code in codes:
            raise DirectoryProtocolError("directory snapshot organization ambiguous")
        codes.add(code)
    account_nos: set[str] = set()
    for account in accounts:
        if not isinstance(account, dict) or type(account.get("active")) is not bool:
            raise DirectoryProtocolError("directory snapshot account invalid")
        account_no = _identity(account.get("account_no"))
        _identity(account.get("user_no"), required=account["active"])
        _text(account.get("display_name"))
        memberships = account.get("organization_codes")
        if not isinstance(memberships, list):
            raise DirectoryProtocolError("directory snapshot memberships invalid")
        for code in memberships:
            _identity(code)
            if account["active"] and code not in codes:
                raise DirectoryProtocolError("directory snapshot organization unresolved")
        if account_no in account_nos:
            raise AccountAmbiguous("directory snapshot application account ambiguous")
        account_nos.add(account_no)


class JsonDirectoryStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def publish(self, snapshot: dict[str, Any]) -> None:
        _validate_snapshot(snapshot)
        temporary: str | None = None
        try:
            if self.path.is_symlink():
                raise DirectoryUnavailable("directory snapshot target invalid")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".iam-directory-", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(snapshot, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            # Same-filesystem replace: readers see either complete generation.
            os.replace(temporary, self.path)
            temporary = None
        except (OSError, ValueError, TypeError):
            raise DirectoryUnavailable("directory snapshot publication failed") from None
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)

    def load(self, *, max_age_seconds: int = 300) -> dict[str, Any]:
        if type(max_age_seconds) is not int or max_age_seconds < 1:
            raise DirectoryUnavailable("directory snapshot age configuration invalid")
        try:
            if self.path.is_symlink() or self.path.stat().st_size > 64 * 1024 * 1024:
                raise DirectoryUnavailable("directory snapshot file invalid")
            snapshot = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeError):
            raise DirectoryUnavailable("directory snapshot unavailable") from None
        _validate_snapshot(snapshot)
        age = time.time() - snapshot["fetched_at"]
        if age < 0 or age > max_age_seconds:
            raise DirectoryUnavailable("directory snapshot expired or clock invalid")
        return snapshot


def resolve_account_from_snapshot(snapshot: dict[str, Any], account_no: str) -> dict[str, Any]:
    """Resolve exact OAuth attributes.username; caller must enforce freshness."""
    _validate_snapshot(snapshot)
    if not isinstance(account_no, str) or not account_no:
        raise AccountUnknown("application account not found")
    matches = [account for account in snapshot["accounts"] if account["account_no"] == account_no]
    if not matches:
        raise AccountUnknown("application account not found")
    account = matches[0]
    if not account["active"]:
        raise AccountDisabled("application account inactive")
    organizations = {org["code"]: org for org in snapshot["organizations"] if org["active"]}
    codes = sorted({code for code in account["organization_codes"] if code in organizations})
    return {
        "user_no": account["user_no"], "account_no": account["account_no"],
        "display_name": account["display_name"], "organization_codes": codes,
        "organization_names": {code: organizations[code]["name"] for code in codes},
    }


async def resolve_account(account_no: str) -> dict[str, Any]:
    """OAuth/session adapter: read a fresh, validated snapshot without networking."""
    path = os.getenv("IAM_DIRECTORY_SNAPSHOT_FILE", "")
    try:
        age = int(os.getenv("IAM_DIRECTORY_SNAPSHOT_MAX_AGE_SECONDS", "300"))
    except ValueError:
        raise DirectoryUnavailable("directory snapshot age configuration invalid") from None
    if not path:
        raise DirectoryUnavailable("directory snapshot configuration missing")
    def load_and_resolve() -> dict[str, Any]:
        snapshot = JsonDirectoryStore(path).load(max_age_seconds=age)
        return resolve_account_from_snapshot(snapshot, account_no)

    # File I/O, JSON parsing, validation and lookup all belong off the ASGI loop.
    return await asyncio.to_thread(load_and_resolve)


async def sync_directory(config: DirectoryConfig | None = None, *, transport: httpx.AsyncBaseTransport | None = None) -> dict[str, Any]:
    config = config or DirectoryConfig.from_env()
    snapshot = await IamDirectoryClient(config, transport=transport).fetch_snapshot()
    await asyncio.to_thread(JsonDirectoryStore(config.snapshot_file).publish, snapshot)
    return snapshot


def check_snapshot() -> dict[str, Any]:
    """Read-only health check; needs neither IAM endpoints nor a signing key."""
    path = os.getenv("IAM_DIRECTORY_SNAPSHOT_FILE", "")
    try:
        age = int(os.getenv("IAM_DIRECTORY_SNAPSHOT_MAX_AGE_SECONDS", "300"))
    except ValueError:
        raise DirectoryUnavailable("directory snapshot age configuration invalid") from None
    if not path:
        raise DirectoryUnavailable("directory snapshot configuration missing")
    return JsonDirectoryStore(path).load(max_age_seconds=age)


def _sync_interval(value: Any) -> int:
    try:
        interval = int(value)
    except (ValueError, TypeError):
        raise DirectoryUnavailable("directory synchronization interval invalid") from None
    if type(value) is bool or not 5 <= interval <= 3600:
        raise DirectoryUnavailable("directory synchronization interval out of bounds")
    return interval


async def watch_directory(
    config: DirectoryConfig, *, interval_seconds: int | None = None,
    stop_event: asyncio.Event | None = None,
) -> None:
    """One writer, sequential full syncs; bounded delay after both success/failure."""
    interval = _sync_interval(interval_seconds if interval_seconds is not None
                              else os.getenv("IAM_DIRECTORY_SYNC_INTERVAL_SECONDS", "120"))
    stop = stop_event or asyncio.Event()
    while not stop.is_set():
        try:
            await sync_directory(config)
            print("IAM_DIRECTORY_SYNC: PASS", flush=True)
        except DirectoryError as exc:
            # Do not publish an empty/partial snapshot or log upstream data.
            print(f"IAM_DIRECTORY_SYNC: FAIL ({type(exc).__name__})", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except asyncio.TimeoutError:
            pass


async def _main(args: argparse.Namespace) -> int:
    try:
        if args.check:
            await asyncio.to_thread(check_snapshot)
            print("IAM_DIRECTORY_CHECK: PASS")
        elif args.watch:
            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for name in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(name, stop.set)
                except (NotImplementedError, RuntimeError):
                    pass  # Windows tests; Linux worker supports signal handlers.
            await watch_directory(DirectoryConfig.from_env(), stop_event=stop)
        else:
            await sync_directory()
            print("IAM_DIRECTORY_SYNC: PASS")
    except DirectoryError as exc:
        print(f"IAM_DIRECTORY_OPERATION: FAIL ({type(exc).__name__})")
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="IAM full directory synchronization")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--watch", action="store_true", help="periodically synchronize")
    mode.add_argument("--check", action="store_true", help="read-only snapshot freshness check")
    raise SystemExit(asyncio.run(_main(parser.parse_args())))
