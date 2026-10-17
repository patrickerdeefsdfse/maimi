import asyncio
import base64
from dataclasses import replace
import json
import struct
import time
import zlib

import httpx
import pytest

from app import personal_model_proxy as model, iam_oauth as iam
from test_iam_oauth import config
from test_personal_model_proxy import actor, unchanged


class Provider:
    def __init__(self, models=None):
        self.key = "synthetic-vision-only-key"
        self.models = models or (model.PersonalModel("selected", "Unknown model"),
                                 model.PersonalModel("other", "Other model"))

    async def resolve(self, current):
        return model.PersonalCredential(current.user_no, self.key, time.time() + 3600, self.models)


def broker(reply, provider=None, validator=unchanged, state=None, base="https://model.test/v1"):
    return model.PersonalModelBroker(provider or Provider(), base, httpx.MockTransport(reply), validator,
        vision_state=state or model._VisionState())


@pytest.fixture
def known_image(monkeypatch):
    answer, image = model._vision_challenge()
    monkeypatch.setattr(model, "_vision_challenge", lambda: (answer, image))
    return answer, image


def successful_response(answer):
    return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})


def test_challenge_contains_only_actual_pixels_no_answer_metadata():
    answer, data_url = model._vision_challenge()
    assert len(answer) == 8 and answer.isascii() and answer.isdigit()
    png = base64.b64decode(data_url.split(",", 1)[1])
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    offset, parts, compressed = 8, [], bytearray()
    while offset < len(png):
        size, = struct.unpack(">I", png[offset:offset + 4])
        kind, payload = png[offset + 4:offset + 8], png[offset + 8:offset + 8 + size]
        parts.append(kind)
        assert zlib.crc32(kind + payload) == struct.unpack(">I", png[offset + 8 + size:offset + 12 + size])[0]
        if kind == b"IHDR":
            width, height, bits, color, *_ = struct.unpack(">IIBBBBB", payload)
            assert (width, height, bits, color) == (464, 88, 8, 0)
        if kind == b"IDAT":
            compressed.extend(payload)
        offset += 12 + size
    assert parts == [b"IHDR", b"IDAT", b"IEND"]
    rows = zlib.decompress(compressed)
    assert len(rows) == height * (width + 1)
    assert {rows[y * (width + 1)] for y in range(height)} == {0}
    assert set(rows) == {0, 255}
    # Independent 5x7 raster templates ensure the image carries the answer.
    templates = [
        "01110100011001110101110011000101110", "00100011000010000100001000010001110",
        "01110100010000100010001000100011111", "11110000010000101110000010000111110",
        "00010001100101010010111110001000010", "11111100001000011110000010000111110",
        "01110100001000011110100011000101110", "11111000010001000100010000100001000",
        "01110100011000101110100011000101110", "01110100011000101111000010000101110",
    ]
    visible = ""
    for index in range(8):
        raster = "".join("1" if rows[(16 + y * 8 + 4) * (width + 1) + 1 + 16 + index * 56 + x * 8 + 4] == 0 else "0"
                         for y in range(7) for x in range(5))
        visible += str(templates.index(raster))
    assert visible == answer


@pytest.mark.parametrize("field", ["inputModalities", "input_modalities"])
def test_explicit_company_capability_is_preserved_through_encrypted_session(config, field):
    models = model._catalog([{"model_code": "known", "model_name": "Known", field: ["image", "text"]},
                             {"model_code": "unknown", "model_name": "Unknown"}])
    assert models[0].input_modalities == ("text", "image")
    assert models[1].input_modalities is None
    current = actor("A")
    expires = int(time.time()) + 600
    credential = model.PersonalCredential(current.user_no, "synthetic-private", expires, models)
    encrypted = iam._encrypt_personal_credential(current.auth_session_id, current.account_no,
                                                 current.user_no, credential, expires)
    record = {"version": iam.PERSONAL_SESSION_VERSION, "account_no": current.account_no,
        "user_no": current.user_no, "expires_at": expires, "personal_credential_encrypted": encrypted}
    restored = iam._decrypt_personal_credential(record, current.auth_session_id)
    assert restored.models == models and "synthetic-private" not in encrypted


@pytest.mark.parametrize("modalities", [None, [], ["image"], ["text", "audio"], ["text", "text"],
                                        ["text", {}], "text,image", [False]])
def test_malformed_modality_declaration_fails_closed(modalities):
    with pytest.raises(ValueError):
        model._catalog([{"model_code": "m", "model_name": "M", "inputModalities": modalities}])


def test_conflicting_company_modality_aliases_rejected():
    with pytest.raises(ValueError):
        model._catalog([{"model_code": "m", "model_name": "M", "inputModalities": ["text"],
                         "input_modalities": ["text", "image"]}])


def test_unknown_models_do_not_gain_images_or_make_background_requests():
    api = broker(lambda request: pytest.fail("catalog must not probe every model"))
    result = asyncio.run(api.models(actor("A")))
    assert all("inputModalities" not in row for row in result["data"])


def test_authorized_selected_model_is_visually_checked_then_cached(known_image, caplog):
    answer, image = known_image
    calls = []

    def respond(request):
        calls.append(request)
        body = json.loads(request.content)
        assert body["model"] == "selected" and body["stream"] is False
        content = body["messages"][0]["content"]
        assert content[1]["image_url"]["url"] == image
        assert answer not in content[0]["text"]
        assert request.headers["authorization"] == "Bearer synthetic-vision-only-key"
        return successful_response(answer)

    api = broker(respond)
    async def run():
        first = await api.ensure_vision(actor("A"), "selected")
        second = await api.ensure_vision(actor("A"), "selected")
        assert first == second == await api.models(actor("A"))
        assert first["data"][0]["inputModalities"] == ["text", "image"]
        assert "inputModalities" not in first["data"][1]
    asyncio.run(run())
    assert len(calls) == 1
    assert "synthetic-vision-only-key" not in caplog.text


@pytest.mark.parametrize("scenario", ["unauthorized", "allowlist", "explicit_text"])
def test_unknown_or_denied_models_do_not_trigger_probe(scenario, monkeypatch):
    provider = Provider()
    selected, code = "foreign", "personal_model_not_authorized"
    if scenario == "allowlist":
        selected = "selected"
        monkeypatch.setenv("PERSONAL_MODEL_ALLOWED_MODELS", "other")
    elif scenario == "explicit_text":
        selected, code = "selected", "personal_model_image_not_supported"
        provider.models = (model.PersonalModel(selected, "Text", ("text",)),)
    api = broker(lambda request: pytest.fail("unauthorized model contacted"), provider)
    with pytest.raises(iam.IAMFailure, match=code):
        asyncio.run(api.ensure_vision(actor("A"), selected))


@pytest.mark.parametrize("change", ["employee", "session", "key", "upstream"])
def test_negotiation_cache_cannot_cross_identity_or_upstream(known_image, change):
    calls, state, provider = [], model._VisionState(), Provider()
    def respond(request):
        calls.append(request)
        return successful_response(known_image[0])
    api = broker(respond, provider, state=state)
    async def run():
        await api.ensure_vision(actor("A"), "selected")
        current, target = actor("A"), api
        if change == "employee":
            current = actor("B")
        elif change == "session":
            current = replace(current, auth_session_id="new-login")
        elif change == "key":
            provider.key = "new-synthetic-key"
        else:
            target = broker(respond, provider, state=state, base="https://different.test/v1")
        assert "inputModalities" not in (await target.models(current))["data"][0]
        await target.ensure_vision(current, "selected")
    asyncio.run(run())
    assert len(calls) == 2


def test_simultaneous_checks_share_one_bounded_request(known_image):
    calls = []
    async def respond(request):
        calls.append(request)
        await asyncio.sleep(0)
        return successful_response(known_image[0])
    api = broker(respond)
    async def run():
        results = await asyncio.gather(*(api.ensure_vision(actor("A"), "selected") for _ in range(5)))
        assert all(item == results[0] for item in results)
    asyncio.run(run())
    assert len(calls) == 1 and not api.vision_state.pending


def test_cancelled_waiter_does_not_cancel_other_waiters(known_image):
    calls = []
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        async def respond(request):
            calls.append(request)
            started.set()
            await release.wait()
            return successful_response(known_image[0])
        api = broker(respond)
        first = asyncio.create_task(api.ensure_vision(actor("A"), "selected"))
        await started.wait()
        second = asyncio.create_task(api.ensure_vision(actor("A"), "selected"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        result = await second
        assert result["data"][0]["inputModalities"] == ["text", "image"]
        assert not api.vision_state.pending
    asyncio.run(run())
    assert len(calls) == 1


def test_expired_positive_cache_requires_fresh_visual_check(known_image):
    calls = []
    def respond(request):
        calls.append(request)
        return successful_response(known_image[0])
    api = broker(respond)
    async def run():
        await api.ensure_vision(actor("A"), "selected")
        key = next(iter(api.vision_state.results))
        api.vision_state.results[key] = True, time.monotonic() - 1
        assert "inputModalities" not in (await api.models(actor("A")))["data"][0]
        await api.ensure_vision(actor("A"), "selected")
    asyncio.run(run())
    assert len(calls) == 2


def test_positive_http_response_without_correct_pixels_is_not_capability(known_image):
    calls = []
    def respond(request):
        calls.append(request)
        return successful_response("Yes, I can see images.")
    api = broker(respond)
    async def run():
        for _ in range(2):
            with pytest.raises(iam.IAMFailure, match="personal_model_vision_unverified"):
                await api.ensure_vision(actor("A"), "selected")
        assert "inputModalities" not in (await api.models(actor("A")))["data"][0]
    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize("status, expected", [(400, "personal_model_vision_unverified"),
    (404, "personal_model_vision_unverified"), (401, "personal_key_reauthorization_required"),
    (403, "personal_key_reauthorization_required"), (429, "personal_model_vision_busy"),
    (500, "personal_model_upstream_unavailable"), (302, "personal_model_upstream_unavailable")])
def test_probe_errors_are_fixed_and_do_not_follow_redirects_or_echo_body(status, expected):
    calls = []
    def respond(request):
        calls.append(request)
        return httpx.Response(status, headers={"Location": "https://foreign.test"},
                              text="synthetic-secret private-upstream-diagnostic")
    api = broker(respond)
    with pytest.raises(iam.IAMFailure, match="^" + expected + "$"):
        asyncio.run(api.ensure_vision(actor("A"), "selected"))
    assert len(calls) == 1


def test_revocation_during_probe_does_not_advertise_images(known_image):
    revoked = False
    async def validate(current):
        if revoked:
            raise iam.IAMFailure(401, "desktop_session_expired")
        return current
    def respond(request):
        nonlocal revoked
        revoked = True
        return successful_response(known_image[0])
    api = broker(respond, validator=validate)
    with pytest.raises(iam.IAMFailure, match="desktop_session_expired"):
        asyncio.run(api.ensure_vision(actor("A"), "selected"))
    assert not api.vision_state.results


def test_key_rotation_during_probe_cannot_publish_old_result(known_image):
    provider = Provider()
    def respond(request):
        provider.key = "rotated-synthetic-key"
        return successful_response(known_image[0])
    api = broker(respond, provider)
    async def run():
        with pytest.raises(iam.IAMFailure, match="personal_key_reauthorization_required"):
            await api.ensure_vision(actor("A"), "selected")
        assert "inputModalities" not in (await api.models(actor("A")))["data"][0]
    asyncio.run(run())


def test_catalog_deny_during_probe_cannot_publish_old_result(known_image):
    provider = Provider()
    def respond(request):
        provider.models = (model.PersonalModel("selected", "Text", ("text",)),)
        return successful_response(known_image[0])
    api = broker(respond, provider)
    async def run():
        with pytest.raises(iam.IAMFailure, match="personal_model_image_not_supported"):
            await api.ensure_vision(actor("A"), "selected")
        assert (await api.models(actor("A")))["data"][0]["inputModalities"] == ["text"]
    asyncio.run(run())


@pytest.mark.parametrize("raw, code", [(b"{" + b"x" * 65536, "personal_model_vision_unverified"),
    (b"invalid JSON", "personal_model_vision_check_unavailable"),
    (b'{"choices":[]}', "personal_model_vision_check_unavailable")])
def test_probe_response_is_bounded_and_invalid_response_never_enables_images(raw, code):
    api = broker(lambda request: httpx.Response(200, content=raw))
    with pytest.raises(iam.IAMFailure, match="^" + code + "$"):
        asyncio.run(api.ensure_vision(actor("A"), "selected"))
    assert all(not result[0] for result in api.vision_state.results.values())


def test_negotiation_is_rate_bounded_per_login(known_image):
    provider = Provider(tuple(model.PersonalModel("m" + str(i), "Model") for i in range(5)))
    calls = []
    def respond(request):
        calls.append(request)
        return successful_response(known_image[0])
    api = broker(respond, provider)
    async def run():
        for index in range(4):
            await api.ensure_vision(actor("A"), "m" + str(index))
        with pytest.raises(iam.IAMFailure, match="personal_model_vision_busy"):
            await api.ensure_vision(actor("A"), "m4")
    asyncio.run(run())
    assert len(calls) == 4


def test_inline_image_payload_above_old_two_mib_limit_has_bounded_budget():
    content = "data:image/png;base64," + "A" * (3 * 1024 * 1024)
    body = {"model": "selected", "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": content}}]}]}
    assert model.validate_body(body) == body
    body["messages"][0]["content"][0]["image_url"]["url"] = "A" * model.MAX_BODY
    with pytest.raises(iam.IAMFailure, match="personal_model_request_too_large"):
        model.validate_body(body)
