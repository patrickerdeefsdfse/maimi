import asyncio
import copy
import logging
import secrets
import re
import time
import os
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from test_iam_oauth import config, env_config, make_service
from app import desktop_auth as desktop, iam_oauth as iam, main


class MemoryDesktopStore:
    """Small store double; production Lua atomicity is covered by local Redis smoke."""
    def __init__(self):
        self.flows, self.oauth, self.grants, self.confirmations = {}, {}, {}, {}

    async def create(self, flow, instance, challenge, ttl=300):
        self.flows[flow] = {"instance": instance, "challenge": challenge, "status": "pending"}

    async def browser_claim(self, flow):
        if flow not in self.flows or self.flows[flow]["status"] != "pending":
            raise iam.IAMFailure(410, "desktop_flow_unavailable")
        self.flows[flow]["status"] = "authorizing"

    async def attach(self, state, flow):
        self.oauth[state] = flow

    async def pop_oauth(self, state):
        return self.oauth.pop(state, None)

    async def complete(self, flow, sid, expires_at):
        self.flows[flow].update(status="confirming", sid=sid, expires_at=expires_at)

    async def confirmation_create(self, confirmation, flow, binding):
        self.confirmations[confirmation] = (flow, binding)

    async def confirm(self, confirmation, binding):
        value = self.confirmations.get(confirmation)
        if value is None or value[1] != binding:
            raise iam.IAMFailure(403, "desktop_confirmation_denied")
        self.confirmations.pop(confirmation)
        self.flows[value[0]]["status"] = "ready"

    async def consume(self, flow, instance, challenge):
        value = self.flows.get(flow)
        if value is None:
            raise iam.IAMFailure(410, "desktop_flow_unavailable")
        if value["instance"] != instance or value["challenge"] != challenge:
            raise iam.IAMFailure(400, "desktop_invalid_proof")
        return self.flows.pop(flow) if value["status"] == "ready" else None

    async def grant(self, token, sid, instance, ttl, challenge=None):
        self.grants[token] = {"sid": sid, "instance": instance, "expires_at": time.time() + ttl,
                              "renewal_challenge": challenge}

    async def renew_grant(self, token, record, expires_at):
        current = self.grants.get(token)
        if not current or current['expires_at'] <= time.time() or current['sid'] != record['sid']:
            raise iam.IAMFailure(401, 'desktop_session_expired')
        current['expires_at'] = max(current['expires_at'], expires_at)
        return int(current['expires_at'])

    async def read_grant(self, token):
        return copy.deepcopy(self.grants.get(token))

    async def delete_grant(self, token):
        return self.grants.pop(token, None)


@pytest.fixture
def desktop_env(monkeypatch, config, env_config):
    monkeypatch.setenv("AUTH_MODE", "iam_direct")
    service, seen = make_service(config)
    service.desktop_store = MemoryDesktopStore()

    async def run(action):
        return await action(service)

    monkeypatch.setattr(iam, "_with_service", run)
    with TestClient(main.app, base_url="https://app.test") as client:
        yield client, service, seen


def begin(client):
    verifier = secrets.token_urlsafe(48)
    instance = str(uuid4())
    response = client.post(desktop.PREFIX + "/start", json={
        "instance_id": instance, "code_challenge": desktop.proof_challenge(verifier)})
    assert response.status_code == 200
    body = response.json()
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["referrer-policy"] == "no-referrer"
    return body, {"instance_id": instance, "flow_id": body["flow_id"], "code_verifier": verifier}


def confirmation_page(client, flow):
    authorize = client.get(flow["browser_url"], follow_redirects=False)
    assert authorize.status_code == 303
    state = parse_qs(urlsplit(authorize.headers["location"]).query)["state"][0]
    callback = client.get(iam.CALLBACK_PATH, params={"state": state, "code": "synthetic-code"}, follow_redirects=False)
    assert callback.status_code == 200
    assert flow["verification_code"] in callback.text
    assert "确认登录" in callback.text
    assert iam.SESSION_COOKIE not in callback.headers.get("set-cookie", "")
    assert "access_token" not in callback.text
    assert callback.headers["referrer-policy"] == "strict-origin"
    assert "no-store" in callback.headers["cache-control"]
    assert "form-action 'self'" in callback.headers["content-security-policy"]
    confirmation = re.search(r'name="confirmation" value="([A-Za-z0-9_-]+)"', callback.text).group(1)
    return state, confirmation


def login_browser(client, flow):
    state, confirmation = confirmation_page(client, flow)
    confirmed = client.post(desktop.PREFIX + "/confirm", data={"confirmation": confirmation},
                            headers={"Origin": "https://app.test"})
    assert confirmed.status_code == 200 and "登录完成" in confirmed.text
    assert confirmed.headers["referrer-policy"] == "no-referrer"
    return state


def login_desktop(client):
    flow, proof = begin(client)
    login_browser(client, flow)
    response = client.post(desktop.PREFIX + "/exchange", json=proof)
    assert response.status_code == 200
    return response.json(), proof


def test_renewal_requires_original_proof_and_never_extends_iam_lease(desktop_env):
    client, service, _ = desktop_env
    body, proof = login_desktop(client)
    assert body['renewal_supported'] is True
    token = body['access_token']
    record = service.desktop_store.grants[token]
    sid = record['sid']
    original_expiry = service.store.sessions[sid]['expires_at']
    record['expires_at'] = time.time() + 80
    headers = {'Authorization': 'Bearer ' + token}
    renewal = {k: proof[k] for k in ('instance_id', 'code_verifier')}
    for override in ({'instance_id': str(uuid4())}, {'code_verifier': 'q'*43}):
        response = client.post(desktop.PREFIX+'/renew', headers=headers, json={**renewal, **override})
        assert response.status_code == 403
    assert client.post(desktop.PREFIX+'/renew', headers={**headers, 'Origin':'https://evil.test'}, json=renewal).status_code == 403
    response = client.post(desktop.PREFIX+'/renew', headers=headers, json=renewal)
    assert response.status_code == 200
    assert 500 <= response.json()['expires_in'] <= 600
    assert service.desktop_store.grants[token]['expires_at'] <= original_expiry
    assert service.store.sessions[sid]['expires_at'] == original_expiry
    assert response.json()['user']['user_no'] == 'EMPLOYEE-A'
    assert 'access_token' not in response.text and 'synthetic-oauth-access' not in response.text
    assert 'no-store' in response.headers['cache-control']
    client.post(desktop.PREFIX+'/logout', headers=headers)
    assert client.post(desktop.PREFIX+'/renew', headers=headers, json=renewal).status_code == 401


@pytest.mark.parametrize('case',['grant-expired','iam-expired','reassigned','no-proof'])
def test_renewal_cannot_resurrect_or_reassign_a_login(desktop_env, case):
    client, service, _ = desktop_env
    body, proof = login_desktop(client)
    token=body['access_token']; record=service.desktop_store.grants[token]
    if case=='grant-expired': record['expires_at']=time.time()-1
    elif case=='iam-expired': service.store.sessions.pop(record['sid'])
    elif case=='no-proof': record.pop('renewal_challenge')
    else:
        async def reassigned(account):
            return {'account_no':account,'user_no':'EMPLOYEE-B','display_name':'B','organization_codes':[]}
        service.mapper=reassigned
    result=client.post(desktop.PREFIX+'/renew', headers={'Authorization':'Bearer '+token},
        json={k:proof[k] for k in ('instance_id','code_verifier')})
    assert result.status_code in (401,403)


@pytest.mark.skipif(not os.getenv('REDIS_TEST_URL'), reason='owned Redis integration fixture only')
def test_real_redis_renewal_atomicity_revocation_and_iam_deadline():
    from redis.asyncio import Redis
    import json
    async def run():
        redis=Redis.from_url(os.environ['REDIS_TEST_URL'],decode_responses=True)
        store=desktop.RedisDesktopStore(redis)
        sid=secrets.token_urlsafe(32); token='mdt_'+secrets.token_urlsafe(32)
        session_key='megmeet:iam:session:'+iam._digest(sid)
        grant_key=store.key('grant',token)
        try:
            await redis.set(session_key,'synthetic-live-session',ex=120)
            await store.grant(token,sid,str(uuid4()),60,desktop.proof_challenge('q'*43))
            record=await store.read_grant(token)
            deadline=await store.renew_grant(token,record,int(time.time())+900)
            assert int(time.time())+100<=deadline<=int(time.time())+120
            assert 100<=await redis.ttl(grant_key)<=120
            wrong={**record,'instance':str(uuid4())}
            with pytest.raises(iam.IAMFailure):await store.renew_grant(token,wrong,int(time.time())+900)
            await redis.delete(grant_key)
            with pytest.raises(iam.IAMFailure):await store.renew_grant(token,record,int(time.time())+900)
            assert not await redis.exists(grant_key)
            await store.grant(token,sid,record['instance'],60,record['renewal_challenge'])
            await redis.delete(session_key)
            with pytest.raises(iam.IAMFailure):await store.renew_grant(token,record,int(time.time())+900)
        finally:
            await redis.delete(session_key,grant_key); await redis.aclose()
    asyncio.run(run())


def test_handoff_pending_wrong_verifier_instance_replay_and_server_secrets(desktop_env):
    client, service, seen = desktop_env
    flow, proof = begin(client)
    assert client.post(desktop.PREFIX + "/exchange", json=proof).status_code == 202
    state = login_browser(client, flow)
    for overrides in ({"code_verifier": "x" * 43}, {"instance_id": str(uuid4())}):
        wrong = client.post(desktop.PREFIX + "/exchange", json={**proof, **overrides})
        assert wrong.status_code == 400
    success = client.post(desktop.PREFIX + "/exchange", json=proof)
    assert success.status_code == 200
    body = success.json()
    assert desktop.TOKEN.fullmatch(body["access_token"])
    assert body["user"]["user_no"] == "EMPLOYEE-A"
    assert 1 <= body["expires_in"] <= 600
    for value in ("synthetic-client-secret", "synthetic-oauth-access", "unused-synthetic-refresh", "auth_session_id"):
        assert value not in success.text
    assert client.post(desktop.PREFIX + "/exchange", json=proof).status_code == 410
    assert client.get(iam.CALLBACK_PATH, params={"state": state, "code": "synthetic-code"}).status_code == 400
    assert len(seen) == 2


def test_expiry_logout_and_employee_reassignment_revoke(desktop_env, monkeypatch):
    from app import personal_model_proxy as model
    class SyntheticProvider:
        async def resolve(self, actor):
            return model.PersonalCredential(actor.user_no, "synthetic-key", time.time() + 60)
    monkeypatch.setattr(model, "get_broker", lambda: model.PersonalModelBroker(
        SyntheticProvider(), "https://model.test/v1"))
    client, service, _ = desktop_env
    body, _ = login_desktop(client)
    token = body["access_token"]
    headers = {"Authorization": "Bearer " + token}
    assert client.get(desktop.PREFIX + "/session", headers=headers).status_code == 200
    assert client.get("/v1/personal-model/models", headers=headers).status_code == 200
    # A desktop token does not become a credential for all legacy platform APIs.
    assert client.get("/v1/chat/history", headers=headers).status_code == 401
    assert client.post(desktop.PREFIX + "/logout", headers=headers).json() == {"revoked": True}
    assert client.get(desktop.PREFIX + "/session", headers=headers).status_code == 401
    assert client.post(desktop.PREFIX + "/logout", headers=headers).status_code == 200
    body, _ = login_desktop(client)
    token = body["access_token"]
    headers = {"Authorization": "Bearer " + token}
    service.desktop_store.grants[token]["expires_at"] = time.time() - 1
    assert client.get(desktop.PREFIX + "/session", headers=headers).status_code == 401
    service.desktop_store.grants[token]["expires_at"] = time.time() + 30

    async def reassigned(account):
        return {"account_no": account, "user_no": "EMPLOYEE-B", "display_name": "B", "organization_codes": ["B"]}
    service.mapper = reassigned
    assert client.get(desktop.PREFIX + "/session", headers=headers).status_code == 403
    assert not service.store.sessions


def test_browser_identity_is_authoritative_and_header_cannot_change_employee(desktop_env):
    client, _, _ = desktop_env
    body, _ = login_desktop(client)
    response = client.get(desktop.PREFIX + "/session", headers={
        "Authorization": "Bearer " + body["access_token"], "X-Employee-No": "EMPLOYEE-B"})
    assert response.json()["user"]["user_no"] == "EMPLOYEE-A"


def test_handoff_rejects_cross_origin_and_extra_identity_fields(desktop_env):
    client, _, _ = desktop_env
    request = {"instance_id": str(uuid4()), "code_challenge": desktop.proof_challenge("a" * 43)}
    assert client.post(desktop.PREFIX + "/start", json=request, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post(desktop.PREFIX + "/start", json={**request, "user_no": "EMPLOYEE-B"}).status_code == 422
    assert client.post(desktop.PREFIX + "/start", json={**request, "instance_id": "not-uuid"}).status_code == 400


@pytest.mark.parametrize("origin", [None, "null", "https://evil.test"])
def test_confirmation_rejects_bad_origin_without_consuming_flow(desktop_env, origin):
    client, _, _ = desktop_env
    flow, proof = begin(client)
    _, confirmation = confirmation_page(client, flow)
    headers = {} if origin is None else {"Origin": origin}
    rejected = client.post(desktop.PREFIX + "/confirm", data={"confirmation": confirmation}, headers=headers)
    assert rejected.status_code == 403
    assert client.post(desktop.PREFIX + "/exchange", json=proof).status_code == 202
    accepted = client.post(desktop.PREFIX + "/confirm", data={"confirmation": confirmation},
                           headers={"Origin": "https://app.test"})
    assert accepted.status_code == 200
    assert client.post(desktop.PREFIX + "/exchange", json=proof).status_code == 200


def test_confirmation_requires_bound_cookie_and_consumes_nonce_once(desktop_env):
    client, _, _ = desktop_env
    flow, proof = begin(client)
    _, confirmation = confirmation_page(client, flow)
    binding = client.cookies.get(desktop.CONFIRM_COOKIE)
    client.cookies.clear()
    headers = {"Origin": "https://app.test"}
    for cookie in (None, secrets.token_urlsafe(32)):
        if cookie is not None:
            client.cookies.set(desktop.CONFIRM_COOKIE, cookie, domain="app.test", path=desktop.PREFIX)
        rejected = client.post(desktop.PREFIX + "/confirm", data={"confirmation": confirmation}, headers=headers)
        assert rejected.status_code == (400 if cookie is None else 403)
    assert client.post(desktop.PREFIX + "/exchange", json=proof).status_code == 202
    client.cookies.set(desktop.CONFIRM_COOKIE, binding, domain="app.test", path=desktop.PREFIX)
    accepted = client.post(desktop.PREFIX + "/confirm", data={"confirmation": confirmation}, headers=headers)
    assert accepted.status_code == 200
    assert client.cookies.get(desktop.CONFIRM_COOKIE) is None
    client.cookies.set(desktop.CONFIRM_COOKIE, binding, domain="app.test", path=desktop.PREFIX)
    assert client.post(desktop.PREFIX + "/confirm", data={"confirmation": confirmation}, headers=headers).status_code == 403
    assert client.post(desktop.PREFIX + "/exchange", json=proof).status_code == 200


def test_cloud_actor_revalidation_rejects_revocation(desktop_env):
    client, service, _ = desktop_env
    body, _ = login_desktop(client)
    token = body["access_token"]
    sid = service.desktop_store.grants[token]["sid"]
    actor = desktop.TrustedActor("EMPLOYEE-A", "app-account-a", "A", ("DEPT-A",), sid)
    assert asyncio.run(desktop.validate_trusted_actor(actor)).user_no == "EMPLOYEE-A"
    client.post(desktop.PREFIX + "/logout", headers={"Authorization": "Bearer " + token})
    with pytest.raises(iam.IAMFailure, match="desktop_session_expired"):
        asyncio.run(desktop.validate_trusted_actor(actor))


def test_handoff_and_oauth_query_credentials_are_not_access_logged():
    filter = iam._IAMLogFilter()
    for path in ("/v1/iam/callback?code=synthetic-secret", "/v1/desktop/auth/authorize?flow=synthetic-secret",
                 "/esc-sso/oauth2.0/profile?access_token=synthetic-secret"):
        record = logging.LogRecord("uvicorn.access", logging.INFO, "", 1, path, (), None)
        assert not filter.filter(record)
    assert filter.filter(logging.LogRecord("uvicorn.access", logging.INFO, "", 1, "/health", (), None))
