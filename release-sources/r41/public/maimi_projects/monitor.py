"""Fixed-path bridge to the existing tonggong service, with no UI redirect.

Origin and TLS policy belong to server configuration. A vCenter password is
accepted only by the separate authenticated browser form, never a model tool.
Only the upstream opaque cookie is returned for encrypted, owner/session-bound
storage. Response text and diagnostics never enter logs or tool results.
"""
from __future__ import annotations

import asyncio
import json
import ipaddress
import re
from urllib.parse import urlsplit

import httpx

LOCATIONS = {'shenzhen_union': '深圳联合VM集群', 'shenzhen_ziguang': '深圳紫光VM集群',
             'design_server': '设计服务器VM集群', 'changsha': '长沙VM集群',
             'zhuzhou': '株洲VM集群', 'yihe': '怡和VM集群'}
COOKIE = re.compile(r'^[A-Za-z0-9_-]{30,128}$')


class MonitorFailure(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class TonggongHTTP:
    def __init__(self, origin, *, transport=None):
        parsed = urlsplit(origin)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or
                parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
            raise ValueError('invalid configured monitor origin')
        # No host/IP supplied through tool/form arguments. Server operator pins
        # this one existing service origin, including its port.
        self.origin, self.transport = origin.rstrip('/'), transport

    async def _request(self, path, *, cookie=None, payload=None):
        if path not in {'run', 'refresh', 'disk'}:
            raise MonitorFailure('monitor_action_invalid')
        if cookie is not None and not COOKIE.fullmatch(cookie):
            raise MonitorFailure('monitor_login_required')
        timeout = 620 if path == 'disk' else 320
        headers = {'Accept': 'application/json'}
        if cookie:
            headers['Cookie'] = 'tonggong_session=' + cookie
        try:
            # A separate cookie jar per request prevents cross-employee state.
            async with asyncio.timeout(timeout), httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout - 5, connect=5), follow_redirects=False,
                    trust_env=False, transport=self.transport) as client:
                async with client.stream('POST', self.origin + '/tonggong/api/' + path,
                                         json=payload, headers=headers) as response:
                    if 300 <= response.status_code < 400:
                        raise MonitorFailure('monitor_upstream_redirected')
                    if response.status_code == 403:
                        raise MonitorFailure('monitor_upstream_access_denied')
                    if response.status_code in {400, 502, 503}:
                        detail_body = bytearray()
                        async for chunk in response.aiter_bytes():
                            detail_body.extend(chunk)
                            if len(detail_body) > 4096: break
                        try: detail = json.loads(detail_body).get('detail') if len(detail_body) <= 4096 else None
                        except (ValueError, AttributeError): detail = None
                        safe_codes = {'VCENTER_ADDRESS_INVALID': 'monitor_address_invalid',
                            'VCENTER_ADDRESS_FORBIDDEN': 'monitor_address_forbidden',
                            'VCENTER_ADDRESS_OUTSIDE_REGION': 'monitor_address_outside_region',
                            'VCENTER_REGION_INVALID': 'monitor_login_fields_invalid',
                            'VCENTER_POLICY_INVALID': 'monitor_address_catalog_unavailable',
                            'VCENTER_LOCAL_BOUNDARY_UNAVAILABLE': 'monitor_address_catalog_unavailable',
                            'VCENTER_UNREACHABLE': 'monitor_target_unreachable',
                            'VCENTER_PROTOCOL_UNCONFIRMED': 'monitor_target_protocol_unconfirmed'}
                        if detail in safe_codes: raise MonitorFailure(safe_codes[detail])
                        raise MonitorFailure('monitor_service_unavailable')
                    if response.status_code in {404, 405, 422}:
                        raise MonitorFailure('monitor_protocol_mismatch')
                    if response.status_code == 429:
                        raise MonitorFailure('monitor_service_busy')
                    if response.status_code not in {200, 401}:
                        raise MonitorFailure('monitor_collection_failed' if response.status_code == 500 else 'monitor_service_unavailable')
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        maximum = 4 * 1024 * 1024 if response.status_code == 200 else 64 * 1024
                        if len(body) + len(chunk) > maximum:
                            raise MonitorFailure('monitor_response_too_large')
                        body.extend(chunk)
                    if response.status_code == 401:
                        if path != 'run':
                            raise MonitorFailure('monitor_login_required')
                        try:
                            error = json.loads(body)
                        except (ValueError, TypeError, RecursionError):
                            error = None
                        # Only the original app's exact safe JSON error means
                        # vCenter rejected credentials. An IAM/reverse-proxy
                        # 401 must not be mislabeled as a wrong password.
                        if isinstance(error, dict) and error.get('detail') == '账号或密码错误':
                            raise MonitorFailure('monitor_credentials_rejected')
                        raise MonitorFailure('monitor_upstream_access_denied')
                    value = json.loads(body)
                    if not isinstance(value, dict):
                        raise ValueError()
                    session_cookie = response.cookies.get('tonggong_session')
                    if path == 'run' and not COOKIE.fullmatch(session_cookie or ''):
                        raise MonitorFailure('monitor_session_unavailable')
                    # Deliberately omit output/stderr and unknown top-level keys.
                    allowed = {'summary', 'vm_resources', 'host_resources', 'storage_resources', 'alert_resources'}
                    if path == 'disk':
                        allowed = {'summary', 'disks', 'hosts', 'multipath_issues', 'datastores', 'smart', 'smart_unmatched', 'collection_errors'}
                    return {key: value[key] for key in allowed if key in value}, session_cookie
        except MonitorFailure:
            raise
        except (TimeoutError, httpx.TimeoutException):
            raise MonitorFailure('monitor_request_timeout') from None
        except httpx.HTTPError:
            raise MonitorFailure('monitor_connection_failed') from None
        except (ValueError, TypeError, RecursionError):
            raise MonitorFailure('monitor_response_invalid') from None

    async def locations(self):
        try:
            async with asyncio.timeout(8), httpx.AsyncClient(timeout=5, follow_redirects=False, trust_env=False, transport=self.transport) as client:
                async with client.stream('GET', self.origin + '/tonggong/api/locations') as response:
                    if response.status_code != 200: raise MonitorFailure('monitor_address_catalog_unavailable')
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > 32768: raise MonitorFailure('monitor_address_catalog_unavailable')
                    values = json.loads(body).get('locations')
                    if not isinstance(values, list) or len(values) != len(LOCATIONS): raise ValueError()
                    catalog = []
                    seen = set()
                    for row in values:
                        key = row.get('value')
                        if key not in LOCATIONS or key in seen: raise ValueError()
                        seen.add(key)
                        target = str(ipaddress.IPv4Address(row.get('target')))
                        networks = row.get('allowed_networks')
                        if not isinstance(networks, list) or not 1 <= len(networks) <= 8: raise ValueError()
                        networks = [str(ipaddress.IPv4Network(value, strict=True)) for value in networks]
                        if any(ipaddress.IPv4Network(value).prefixlen < 24 for value in networks): raise ValueError()
                        catalog.append({'value': key, 'label': LOCATIONS[key], 'target': target, 'allowed_networks': networks})
                    return catalog
        except MonitorFailure: raise
        except (TimeoutError, httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise MonitorFailure('monitor_address_catalog_unavailable') from None

    async def login(self, username, password, location, target=None):
        if (not isinstance(username, str) or not 1 <= len(username.strip()) <= 128 or
                not isinstance(password, str) or not 1 <= len(password) <= 1024 or
                location not in LOCATIONS or '\x00' in username or '\x00' in password):
            raise MonitorFailure('monitor_login_fields_invalid')
        if target is not None:
            try:
                if not isinstance(target, str) or str(ipaddress.IPv4Address(target)) != target: raise ValueError()
            except ValueError: raise MonitorFailure('monitor_address_invalid') from None
        return await self._request('run', payload={'username': username.strip(), 'password': password, 'location': location, 'target': target})

    async def collect(self, cookie, kind):
        if kind not in {'performance', 'disk'}:
            raise MonitorFailure('monitor_action_invalid')
        data, _ = await self._request('refresh' if kind == 'performance' else 'disk', cookie=cookie)
        return data
