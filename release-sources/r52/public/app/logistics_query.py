"""Bounded, read-only MES logistics lookup. Credentials and identity are server-owned.

No real provider request is enabled merely by supplying a waybill. Kuaidi100 is
optional and requires explicit server credentials plus a carrier-code mapping.
Classification rules describe the supplied MES workbook, not global numbering.
"""
from __future__ import annotations

import asyncio
import hashlib
import html
import inspect
import json
import os
import re
import stat
import time
import unicodedata
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from .kuaidi100_mcp import (
    Kuaidi100MCPClient,
    Kuaidi100MCPConfig as MCPTransportConfig,
    Kuaidi100MCPError,
)

MES_ENDPOINT = "http://api.megmeet.com:9080/api/MesApi/ApiFromObjectValue?sqlid=472E7EFC-26F9-4B1C-B90F-85A054401FA4"
KUAIDI100_ENDPOINT = "https://poll.kuaidi100.com/poll/query.do"
KUAIDI100_CREDENTIAL_FILE = "/run/secrets/maimi-logistics-kuaidi100.json"
KUAIDI100_MCP_CREDENTIAL_FILE = "/run/secrets/maimi-logistics-kuaidi100-mcp.json"
FACTORY_PHONE_POLICY_FILE = "/run/secrets/maimi-logistics-factory-phones.json"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_RESULT_BYTES = 85_000
MAX_TRACKING_REQUESTS = 20
MAX_ETA_REQUESTS = 4
_ETA_ACTIVE_STATES = {"IN_TRANSIT", "COLLECTED", "OUT_FOR_DELIVERY"}
# Explicit support list published by Kuaidi100's in-transit MCP tool. No
# product type, destination or service capability is guessed from a waybill.
_ETA_CARRIERS = {"yuantong", "zhongtong", "shunfeng", "shunfengkuaiyun", "jd", "jtexpress",
                 "shentong", "yunda", "ems", "kuayue", "debangkuaidi", "emsguoji",
                 "youzhengguonei", "youzhengguoji", "zhaijisong", "zhimakaimen",
                 "lianbangkuaidi", "tiandihuayu", "annengwuliu", "jinguangsudikuaijian", "jiayunmeiwuliu"}
RULE_SOURCE = "MES_WORKBOOK_SNAPSHOT"
MES_PHONE_FIELD = "发货通知人手机号码"
MES_DESTINATION_FIELD = "收货地址"
MES_FACTORY_FIELD = "出货工厂"
Authorize = Callable[[], bool | None | Awaitable[bool | None]]
PhoneResolver = Callable[[str, str, str, str], str | None | Awaitable[str | None]]
_ORDER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z", re.ASCII)
# These are sales-shipment identifiers demonstrated by the MES interface and
# workbook. Carrier waybills, telephone numbers and arbitrary numeric notes
# must not turn into MES queries. Matching never repairs or guesses digits.
_SALES_IN_TEXT = re.compile(r"(?<![A-Z0-9_-])(?:SA|SD)[A-Z]{1,4}[0-9]{8,16}(?![A-Z0-9_-])", re.ASCII)
MAX_QUERY_TEXT = 16_000
MAX_ORDERS = 20
_WAYBILL = re.compile(r"[A-Za-z0-9]{6,32}\Z", re.ASCII)
_IDENTITY = re.compile(r"[A-Za-z0-9_.@-]{1,128}\Z", re.ASCII)
_RULES = sorted([
    ("9100", "思邦"), ("8100", "思邦"), ("85", "思邦"),
    ("思邦", "思邦"), ("出账", "思邦"), ("JD", "京东"),
    ("ZY", "中通"), ("中通", "中通"), ("KY", "跨越"),
    ("SF", "顺丰快递"), ("DP", "德邦"),
    ("德邦", "德邦"), ("DHL", "DHL"), ("联邦", "联邦"),
    ("fedex", "飞帆(国外)"), ("8", "优速快递"), ("壹米滴答", "优速快递"),
    ("汇森", "汇森速运"), ("z", "自提"), ("普通", "普通"),
    ("客户自提", "客户自提"), ("世邦", "世邦"), ("进代管仓", "进代管仓"),
    ("公司车", "公司车"), ("成达", "成达"),
], key=lambda pair: (-len(pair[0]), pair[0].casefold()))
_NAME_ONLY = {"普通", "客户自提", "世邦", "进代管仓", "公司车", "成达", "飞帆", "德邦专车", "自提"}
_PHONE_CARRIERS = {"shunfeng", "shunfengkuaiyun", "zhongtong"}
_CARRIER_LABELS = {
    "汇森速运": "汇森速运", "汇森": "汇森速运",
    "顺丰快递": "顺丰快递", "顺丰速运": "顺丰快递", "顺丰": "顺丰快递",
    "顺丰快运": "顺丰快运", "德邦快递": "德邦", "德邦": "德邦",
    "京东快递": "京东", "京东": "京东", "跨越速运": "跨越", "跨越": "跨越",
    "中通快递": "中通", "中通": "中通", "优速快递": "优速快递", "优速": "优速快递",
    "壹米滴答": "优速快递", "思邦": "思邦", "联邦": "联邦", "DHL": "DHL",
}
_LABELLED_REFERENCE = re.compile(
    r"(?<![A-Za-z0-9])(" + "|".join(re.escape(label) + (r"(?=[ \t:：])" if label.isascii() else "")
        for label in sorted(_CARRIER_LABELS, key=len, reverse=True))
    + r")[ \t]*(?:物流单号|运单号|单号)?[ \t]*[:：]?[ \t]*([A-Za-z0-9]{6,32})(?![A-Za-z0-9])", re.ASCII)
_STRONG_REFERENCE = re.compile(r"(?<![A-Za-z0-9])(?:DHL|DPK?|SF|KY|JD|ZY|FEDEX)[0-9]{6,29}(?![A-Za-z0-9])", re.ASCII | re.IGNORECASE)
_TRACKING_ERRORS = {"PHONE_REQUIRED", "PHONE_INVALID", "PHONE_MISMATCH", "RATE_LIMITED", "PROVIDER_UNAVAILABLE", "PROVIDER_INVALID_RESPONSE", "PROVIDER_AUTH_ERROR", "QUOTA_EXHAUSTED", "CARRIER_UNSUPPORTED", "TRACKING_TIMEOUT", "TRACKING_BUDGET_EXCEEDED"}
_MCP_ERROR_CODES = {
    "MCP_AUTH_REJECTED": "PROVIDER_AUTH_ERROR",
    "MCP_RATE_LIMITED": "RATE_LIMITED",
    "MCP_REQUEST_TIMEOUT": "TRACKING_TIMEOUT",
    "MCP_QUERY_LIMIT": "TRACKING_BUDGET_EXCEEDED",
    "MCP_RESPONSE_INVALID": "PROVIDER_INVALID_RESPONSE",
    "MCP_RESPONSE_TOO_LARGE": "PROVIDER_INVALID_RESPONSE",
    "MCP_SECRET_ECHO": "PROVIDER_INVALID_RESPONSE",
    "MCP_TOOL_RESULT_INVALID": "PROVIDER_INVALID_RESPONSE",
    "MCP_QUERY_CONTRACT_UNAVAILABLE": "PROVIDER_INVALID_RESPONSE",
}


@dataclass
class _MCPQueryContext:
    stack: AsyncExitStack
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    client: Kuaidi100MCPClient | None = None
    unavailable: str | None = None
    phone_parameter_available: bool = False
    eta_tool_available: bool = False
    eta_product_required: bool = True


def _strict_json_object(raw: bytes | str) -> dict[str, Any]:
    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("duplicate")
            value[key] = item
        return value
    result = json.loads(raw, object_pairs_hook=unique_pairs,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError("constant")))
    if not isinstance(result, dict):
        raise ValueError("object")
    return result


def _mcp_payload(result: dict[str, Any]) -> dict[str, Any]:
    """Accept structured JSON only; tool text is data, never executable advice."""
    if result.get("isError") is True:
        raise LogisticsQueryError("PROVIDER_UNAVAILABLE")
    if "structuredContent" in result:
        payload = result["structuredContent"]
    else:
        content = result.get("content")
        if (not isinstance(content, list) or len(content) != 1
                or not isinstance(content[0], dict) or content[0].get("type") != "text"
                or not isinstance(content[0].get("text"), str)):
            raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
        try:
            payload = _strict_json_object(content[0]["text"])
        except (ValueError, TypeError, RecursionError):
            raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE") from None
    if not isinstance(payload, dict):
        raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
    # The public SDK sets isError=false even for message-only business errors.
    if "message" in payload and "kuaidiNum" not in payload:
        raise LogisticsQueryError("PROVIDER_UNAVAILABLE")
    return payload


def _event_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})?", value, re.ASCII):
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        # Kuaidi100's local timestamps are Chinese local time, not the gateway
        # host's possibly different timezone. Explicit offsets take priority.
        return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone(timedelta(hours=8)))
    except ValueError:
        return None


# Kuaidi100's public waybill state table, including resultv2=4 sub-states.
# This is an exact vocabulary, not a substring classifier. In particular 501
# means placed in a locker/station, and 201 means NOT signed after a delay.
_STATE_CODES = {
    "0": "IN_TRANSIT", "1": "COLLECTED", "2": "EXCEPTION", "3": "DELIVERED",
    "4": "RETURN_SIGNED", "5": "OUT_FOR_DELIVERY", "6": "RETURNING",
    "7": "TRANSFERRED", "8": "CUSTOMS", "10": "AWAITING_CUSTOMS",
    "11": "CUSTOMS", "12": "CUSTOMS_CLEARED", "13": "CUSTOMS_EXCEPTION",
    "14": "REFUSED", "101": "ORDERED", "102": "AWAITING_COLLECTION",
    "103": "COLLECTED", "1001": "IN_TRANSIT", "1002": "IN_TRANSIT",
    "1003": "IN_TRANSIT", "501": "AT_PICKUP_POINT",
    "301": "DELIVERED", "302": "DELIVERED", "303": "DELIVERED", "304": "DELIVERED",
    "401": "CANCELLED", "201": "EXCEPTION", "202": "EXCEPTION",
    "203": "REJECTION_REQUESTED", "204": "EXCEPTION", "205": "EXCEPTION",
    "206": "EXCEPTION", "207": "EXCEPTION", "208": "EXCEPTION",
    "209": "EXCEPTION", "210": "EXCEPTION",
}
_STATE_LABELS = {
    "在途": "IN_TRANSIT", "到达派件城市": "IN_TRANSIT", "干线": "IN_TRANSIT",
    "转递": "IN_TRANSIT", "揽收": "COLLECTED", "已揽收": "COLLECTED",
    "已下单": "ORDERED", "待揽收": "AWAITING_COLLECTION",
    "派件": "OUT_FOR_DELIVERY", "派送中": "OUT_FOR_DELIVERY",
    "投柜或驿站": "AT_PICKUP_POINT", "签收": "DELIVERED", "已签收": "DELIVERED",
    "本人签收": "DELIVERED", "派件异常后签收": "DELIVERED", "代签": "DELIVERED",
    "投柜或站签收": "DELIVERED", "投柜或驿站签收": "DELIVERED",
    "退回": "RETURNING", "退签": "RETURN_SIGNED", "拒签": "REFUSED",
    "拒收": "REJECTION_REQUESTED", "转投": "TRANSFERRED", "已销单": "CANCELLED",
    "疑难": "EXCEPTION", "异常": "EXCEPTION", "超时未签收": "EXCEPTION",
    "超时未更新": "EXCEPTION", "派件异常": "EXCEPTION", "柜或驿站超时未取": "EXCEPTION",
    "无法联系": "EXCEPTION", "超区": "EXCEPTION", "滞留": "EXCEPTION",
    "破损": "EXCEPTION", "销单": "EXCEPTION", "清关": "CUSTOMS",
    "待清关": "AWAITING_CUSTOMS", "清关中": "CUSTOMS", "已清关": "CUSTOMS_CLEARED",
    "清关异常": "CUSTOMS_EXCEPTION",
}


def _state_token(value: Any) -> str | None:
    # Boolean/float/coerced objects are not a documented state code.
    if type(value) is int:
        return str(value) if 0 <= value <= 9999 else None
    return value.strip() if isinstance(value, str) and len(value) <= 80 else None


def _tracking_state(value: Any, *, code: Any = None) -> str:
    token, code_token = _state_token(value), _state_token(code)
    by_value = _STATE_LABELS.get(token) or _STATE_CODES.get(token)
    by_code = _STATE_CODES.get(code_token)
    # A known status label contradicting a known structured code is unresolved,
    # not a license to select whichever one happens to say delivered.
    if by_value and by_code and by_value != by_code:
        return "TRACKING_AVAILABLE"
    return by_value or by_code or "TRACKING_AVAILABLE"


def _signed_event(event: Mapping[str, Any]) -> bool:
    return _tracking_state(event.get("status"), code=event.get("statusCode")) == "DELIVERED"


def _private_phone_text(value: str, phone: str) -> str:
    # Only a complete authorized phone sent on this request is eligible for
    # masking. Never strip arbitrary four digits or unrelated courier phones.
    if not re.fullmatch(r"[0-9]{11}", phone, re.ASCII):
        return value
    pattern = r"(?<![0-9])" + r"[ \t-]*".join(phone) + r"(?![0-9])"
    return re.sub(pattern, "[校验电话已隐藏]", value)


def _apply_mcp_trace(row: dict[str, Any], result: dict[str, Any], *, private_phone: str = "") -> dict[str, Any] | None:
    payload = _mcp_payload(result)
    if (payload.get("kuaidiNum") != row["waybill"]
            or any(not isinstance(payload.get(key), str) for key in ("kuaidiCom", "kuaidiName", "state", "fromTo"))
            or not isinstance(payload.get("data"), list)
            or len(payload["kuaidiCom"]) > 80 or len(payload["kuaidiName"]) > 80
            or len(payload["state"]) > 80):
        raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
    events = payload["data"]
    if len(events) > 2048:
        raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
    parsed: list[tuple[datetime | None, dict[str, Any]]] = []
    for event in events:
        if (not isinstance(event, dict)
                or any(not isinstance(event.get(key), str) for key in ("time", "status", "context"))):
            raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
        parsed.append((_event_datetime(event["time"]), {
            **event, "context": _private_phone_text(event["context"], private_phone)}))
    payload = {**payload, "data": [event for _, event in parsed],
               "fromTo": _private_phone_text(payload["fromTo"], private_phone)}
    # Keep workbook-derived carrier visible; provider identification is a
    # separate field, especially when the enterprise prefix rule differs.
    row["tracking_carrier_name"] = _private_phone_text(payload["kuaidiName"], private_phone)
    row["tracking_carrier_code"] = _private_phone_text(payload["kuaidiCom"], private_phone)
    row["tracking_state_text"] = _private_phone_text(payload["state"], private_phone)
    if not events:
        row["tracking_status"] = "NO_TRACKING"
        return
    row["tracking_status"] = _tracking_state(payload["state"])
    if any(stamp is None for stamp, _ in parsed):
        # An unparseable event might be the latest; do not pick another one and
        # call it latest. Preserve provider state with an explicit limitation.
        row["tracking_time_unverified"] = True
        return
    _, latest = max(parsed, key=lambda pair: pair[0])
    row["latest_event"] = latest["context"][:1024]
    row["event_time"] = latest["time"]
    row["event_truncated"] = len(latest["context"]) > 1024
    if row["tracking_status"] == "DELIVERED":
        signed = [(stamp, event) for stamp, event in parsed if _signed_event(event)]
        if signed:
            row["signed_event_time"] = max(signed, key=lambda pair: pair[0])[1]["time"]
    if row["tracking_status"] in _ETA_ACTIVE_STATES and _eta_time(payload.get("arrivalTime")) != "—":
        row["estimated_arrival_time"] = payload["arrivalTime"]
        row["eta_source"] = "KUAIDI100_QUERY_TRACE"
        row["eta_status"] = "AVAILABLE"
    return payload


def _eta_time(value: Any) -> str:
    """Respect the provider's hour/minute precision; never manufacture minutes."""
    if not isinstance(value, str):
        return "—"
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}", value, re.ASCII):
        try:
            return datetime.strptime(value, "%Y-%m-%d %H").strftime("%Y-%m-%d %H时")
        except ValueError:
            return "—"
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}", value, re.ASCII):
        value += ":00"
    stamp = _event_datetime(value)
    return stamp.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if stamp else "—"


def _eta_arguments(payload: dict[str, Any], *, require_product: bool = True,
                   record_destination: str | None = None) -> dict[str, str] | None:
    """Use matched trace history, plus a private same-record MES region if needed."""
    if payload.get("kuaidiCom") not in _ETA_CARRIERS:
        return None
    exp_type, from_to, events = payload.get("expType"), payload.get("fromTo"), payload.get("data")
    # The hosted contract has no product input. Do not bind its response to an
    # unused trace field, or manufacture a product for the public snake contract.
    product = exp_type.strip() if require_product and isinstance(exp_type, str) and len(exp_type) <= 80 else ""
    if ((require_product and not product)
            or not isinstance(from_to, str) or from_to.count("->") != 1
            or not isinstance(events, list) or not 1 <= len(events) <= 100):
        return None
    origin, destination = (part.strip() for part in from_to.split("->"))
    if record_destination:
        if destination and _route_conflicts(destination, record_destination):
            return None
        if not destination:
            destination = record_destination
    if not origin or not destination or max(len(origin), len(destination)) > 200:
        return None
    if any(not isinstance(e, dict) or _event_datetime(e.get("time", "")) is None
           or any(not isinstance(e.get(k), str) for k in ("status", "context")) for e in events):
        return None
    normalized_events = [{"time": _event_datetime(e["time"]).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                          "status": e["status"], "context": e["context"]} for e in events]
    logistic = json.dumps(normalized_events, ensure_ascii=False, separators=(",", ":"))
    if len(logistic.encode("utf-8")) > 64_000:
        return None
    return {"kuaidi_com": payload["kuaidiCom"], "from_loc": origin, "to_loc": destination,
            "order_time": min(normalized_events, key=lambda e: e["time"])["time"],
            "exp_type": product, "logistic": logistic}


def _eta_route_key(value: Any) -> str | None:
    # Only typography differs here: retain every place name and administrative
    # suffix, and every comma. No geocoding, abbreviations or missing endpoints.
    if not isinstance(value, str) or len(value) > 200:
        return None
    return re.sub(r"\s+", "", value.replace("，", ",")) or None


_PROVINCE_NAMES = {
    "内蒙古自治区": "内蒙古", "广西壮族自治区": "广西", "西藏自治区": "西藏",
    "宁夏回族自治区": "宁夏", "新疆维吾尔自治区": "新疆",
    **{name + "省": name for name in ("河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽",
        "福建", "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州", "云南", "陕西",
        "甘肃", "青海", "台湾")},
    **{name + "市": name for name in ("北京", "天津", "上海", "重庆")},
    "香港特别行政区": "香港", "澳门特别行政区": "澳门",
}


def _explicit_admin_parts(value: str) -> dict[str, str]:
    """Compare only administrative levels explicitly present in both texts.

    This is a conflict check, not a geocoder. No absent city is inferred from
    a county, a town, a transit station or a postcode.
    """
    layers = [re.sub(r"\s+", "", item) for item in re.split(r"[,，]", value)]
    text = "".join(layers)
    parts: dict[str, str] = {}
    # Supplier examples use a province abbreviation as its own comma-delimited
    # level. Consume that level before joining; otherwise 广东,深圳市 becomes
    # the false city name 广东深圳. An unseparated abbreviation is not inferred.
    if len(layers) > 1 and layers[0] in set(_PROVINCE_NAMES.values()):
        parts["province"] = layers[0]
        text = "".join(layers[1:])
        if layers[0] in {"北京", "天津", "上海", "重庆"}:
            parts["city"] = layers[0]
    for full in sorted(_PROVINCE_NAMES, key=len, reverse=True):
        if text.startswith(full):
            parts["province"] = _PROVINCE_NAMES[full]
            text = text[len(full):]
            if full in {"北京市", "天津市", "上海市", "重庆市"}:
                parts["city"] = parts["province"]
            break
    if not parts:
        province = re.match(r"^([\u4e00-\u9fff]{2,12})省", text)
        if province:
            parts["province"] = province[1]
            text = text[province.end():]
    city = re.match(r"^((?:(?![区县旗])[\u4e00-\u9fff]){2,16}?)(?:市|地区|自治州|盟)", text)
    if city:
        parts["city"] = city[1]
        text = text[city.end():]
    # Without an explicit city boundary, e.g. 宣城绩溪县, tokenizing a county
    # would incorrectly claim we know where the unlabelled city name ends.
    if "city" in parts:
        district = re.match(r"^([\u4e00-\u9fff]{1,20}?)(?:自治县|区|县|旗|市)", text)
        if district:
            parts["district"] = district[1]
    return parts


def _route_conflicts(left: str, right: str) -> bool:
    a, b = _explicit_admin_parts(left), _explicit_admin_parts(right)
    return any(a[key] != b[key] for key in a.keys() & b.keys())


def _eta_echo_address(value: Any) -> bool:
    return (isinstance(value, str) and 1 <= len(value.strip()) <= 200
            and not any(ord(c) < 32 for c in value)
            and not any(token in value for token in ("->", "→", "<", ">", ";", "；", "|")))


def _eta_route_relation(requested: Any, echoed: Any) -> str:
    """Metadata classification only; compatibility is not geocoding proof."""
    if not _eta_echo_address(requested) or not _eta_echo_address(echoed):
        return "MISSING_OR_INVALID"
    if _route_conflicts(requested, echoed):
        return "EXPLICIT_CONFLICT"
    return "EXACT" if requested == echoed else "NORMALIZED_COMPATIBLE"


def _apply_mcp_eta(row: dict[str, Any], response: dict[str, Any], args: dict[str, str]) -> None:
    payload = _mcp_payload(response)
    # MCP already correlates the strict JSON-RPC response id to this serialized
    # call. Published fromName/toName are supplier-parsed addresses, not exact
    # echoes of user input. Reject explicit conflicts without claiming local
    # geographic verification; keep the real event time and product binding.
    if (_eta_route_relation(args["from_loc"], payload.get("fromName")) not in {"EXACT", "NORMALIZED_COMPATIBLE"}
            or _eta_route_relation(args["to_loc"], payload.get("toName")) not in {"EXACT", "NORMALIZED_COMPATIBLE"}
            or ("kuaidiNum" in payload and payload["kuaidiNum"] != row.get("waybill"))
            or ("kuaidicom" in payload and payload["kuaidicom"] != args["kuaidi_com"])
            or (args.get("exp_type") and payload.get("expType") is not None
                and payload["expType"] != args["exp_type"])
            or _event_datetime(payload.get("orderTime", "")) != _event_datetime(args["order_time"])
            or _eta_time(payload.get("arrivalTime")) == "—"):
        row["eta_status"] = "UNVERIFIED"
        return
    row["estimated_arrival_time"] = payload["arrivalTime"]
    row["eta_source"] = "KUAIDI100_IN_TRANSIT_FORECAST"
    row["eta_status"] = "AVAILABLE"


class LogisticsQueryError(Exception):
    """Stable error code only; never includes provider bodies or credentials."""
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def mes_phone_suffix(value: Any) -> str | None:
    """Reduce the authorized MES mobile field without retaining its full value.

    A blank value means MES has no maintained number. The provider also accepts
    four-digit suffixes; virtual numbers use their explicit four-digit extension.
    Do not salvage digits from masked, multiple, or arbitrary textual values.
    """
    if value is None or isinstance(value, str) and not value.strip():
        return None
    if not isinstance(value, str) or len(value) > 64:
        raise LogisticsQueryError("PHONE_INVALID")
    phone = value.strip()
    if re.fullmatch(r"[0-9]{4}", phone, re.ASCII):
        return phone
    if not re.fullmatch(r"[0-9+ \t-]+", phone, re.ASCII):
        raise LogisticsQueryError("PHONE_INVALID")
    compact = re.sub(r"[ \t]", "", phone)

    def mainland_mobile(candidate: str) -> str | None:
        candidate = candidate.replace("-", "")
        for prefix in ("+86", "0086", "86"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix):]
                break
        return candidate if re.fullmatch(r"1[3-9][0-9]{9}", candidate, re.ASCII) else None

    base, separator, extension = compact.rpartition("-")
    if separator and re.fullmatch(r"[0-9]{4}", extension, re.ASCII) and mainland_mobile(base):
        return extension
    mobile = mainland_mobile(compact)
    if mobile:
        return mobile[-4:]
    raise LogisticsQueryError("PHONE_INVALID")


@dataclass(frozen=True, repr=False)
class _RecordPhone:
    # This object is only held beside its MES record's rows. It is never put in
    # progress snapshots, returned rows, caches, or serialized diagnostic data.
    suffix: str | None = field(default=None, repr=False)
    status: str | None = None


def _record_phone(record: dict[str, Any]) -> _RecordPhone | None:
    if MES_PHONE_FIELD not in record:
        return None
    try:
        suffix = mes_phone_suffix(record[MES_PHONE_FIELD])
        return _RecordPhone(suffix, None if suffix else "PHONE_REQUIRED")
    except LogisticsQueryError:
        return _RecordPhone(status="PHONE_INVALID")


@dataclass(frozen=True, repr=False)
class _RecordContext:
    # Retained only next to this source record's pending rows. Never serialize
    # these fields into results, progress, model prompts or the response cache.
    phone: _RecordPhone | None = field(default=None, repr=False)
    factory: str | None = field(default=None, repr=False)
    destination: str | None = field(default=None, repr=False)


def _mes_destination_region(value: Any) -> str | None:
    # Read only the explicit region prefix of the authorized MES address.
    # Never transmit the trailing street number, consignee or contact number.
    if not isinstance(value, str) or not 1 <= len(value) <= 2000:
        return None
    text = re.sub(r"^(?:收货地址|地址)\s*[:：]\s*", "", value.strip(), count=1)
    if not text:
        return None
    # Consume full autonomous-region names first; their final 区 is a province
    # marker, not the district boundary at which to cut the delivery address.
    offset = 0
    for full in sorted(_PROVINCE_NAMES, key=len, reverse=True):
        if text.startswith(full):
            offset = len(full)
            break
    # Restrict the region scan to the leading geographic text. Trailing street
    # numbers and contact notes are irrelevant, even if they contain / or a
    # newline. Never scan through a phone/contact label to find a later region.
    lead = re.split(r"[0-9@+<>:=：=/;；|\r\n]|联系人|收件人|收货人|电话|手机", text, maxsplit=1)[0]
    tail = lead[offset:]
    # A 镇 inside 镇江市 or 景德镇市 is part of an explicitly named city.
    city = re.match(r"^(?:(?![区县旗])[\u4e00-\u9fff]){2,16}?(?:市|地区|自治州|盟)", tail)
    city_length = city.end() if city else 0
    boundary = re.search(r"区|县|旗|镇(?![\u4e00-\u9fff]{0,2}[区县旗市])|街道", tail[city_length:])
    if boundary is None:
        return None
    end = offset + city_length + boundary.end()
    region = text[:end].strip()
    if (not 3 <= len(region) <= 100
            or re.search(r"[0-9@+<>:=：=]", region)
            or re.search(r"联系人|收件人|收货人|电话|手机|先生|女士", region)
            or not re.fullmatch(r"[\u4e00-\u9fff,，· \u3000]+", region)):
        return None
    # A second explicitly named province/municipality denotes an ambiguous
    # multi-address field, even when no semicolon was supplied.
    remainder = text[offset:] if offset else text
    rest = text[end:]
    if (any(name in remainder for name in _PROVINCE_NAMES)
            or re.search(r"另(?:一个|一)?(?:收货)?地址|(?:收货地址|地址)\s*[:：]", rest)
            or re.match(r"\s*(?:/|;|；|\||或|\r?\n)\s*[\u4e00-\u9fff]{1,16}(?:区|县|旗|镇|街道)", rest)):
        return None
    return region


def _record_context(record: dict[str, Any], *, allow_destination: bool = True) -> _RecordContext:
    factory, destination = record.get(MES_FACTORY_FIELD), record.get(MES_DESTINATION_FIELD)
    factory = factory.strip() if isinstance(factory, str) and len(factory) <= 80 else None
    destination = _mes_destination_region(destination) if allow_destination else None
    return _RecordContext(_record_phone(record), factory or None, destination or None)


@dataclass(frozen=True, repr=False)
class FactoryPhonePolicy:
    # Values are loaded from a separate server-only file. No real phone number
    # or factory alias is built into a distributed skill or client package.
    aliases: Mapping[str, str] = field(repr=False)
    ruixinte_sender_suffix: str = field(repr=False)
    shuote_kuayue_phone: str = field(repr=False)

    @classmethod
    def from_private_data(cls, data: Any) -> "FactoryPhonePolicy":
        if (not isinstance(data, dict) or set(data) != {
                "schema", "factory_aliases", "ruixinte_sender_phone", "shuote_kuayue_phone"}
                or type(data["schema"]) is not int or data["schema"] != 1):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        aliases = data["factory_aliases"]
        if (not isinstance(aliases, dict) or not 1 <= len(aliases) <= 16
                or any(not isinstance(name, str) or not 1 <= len(name) <= 80
                       or name != name.strip() or any(ord(c) < 32 for c in name)
                       or not isinstance(kind, str) or kind not in {"ruixinte", "aisimengte", "shuote"}
                       for name, kind in aliases.items())):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        if aliases != {"瑞欣特": "ruixinte", "埃斯蒙特": "aisimengte", "硕特科技": "shuote"}:
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        try:
            suffix = mes_phone_suffix(data["ruixinte_sender_phone"])
            full = data["shuote_kuayue_phone"]
            if not suffix or not isinstance(full, str) or len(full) > 64:
                raise ValueError("shape")
            full = re.sub(r"[ \t-]", "", full.strip())
            for prefix in ("+86", "0086", "86"):
                if full.startswith(prefix):
                    full = full[len(prefix):]
                    break
            if not re.fullmatch(r"1[3-9][0-9]{9}", full, re.ASCII):
                raise ValueError("shape")
        except (LogisticsQueryError, ValueError, TypeError):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION") from None
        return cls(dict(aliases), suffix, full)


def _load_factory_phone_policy() -> FactoryPhonePolicy:
    try:
        fd = os.open(FACTORY_PHONE_POLICY_FILE, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_uid != os.geteuid() or info.st_nlink != 1 or info.st_size > 8192):
                raise ValueError("permissions")
            return FactoryPhonePolicy.from_private_data(_strict_json_object(stream.read(8193)))
    except Exception:
        raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION") from None


@dataclass(frozen=True)
class Kuaidi100Config:
    customer: str = field(repr=False)
    key: str = field(repr=False)
    carrier_codes: Mapping[str, str] = field(default_factory=dict)
    phone_resolver: PhoneResolver | None = field(default=None, repr=False)
    single_process_rate_guard: bool = False


@dataclass(frozen=True)
class Kuaidi100MCPConfig:
    key: str = field(repr=False)
    single_process_rate_guard: bool = False
    phone_resolver: PhoneResolver | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LogisticsConfig:
    enabled: bool = False
    mes_headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    kuaidi100: Kuaidi100Config | None = field(default=None, repr=False)
    kuaidi100_mcp: Kuaidi100MCPConfig | None = field(default=None, repr=False)
    mes_user_code: str | None = field(default=None, repr=False)
    factory_phone_policy: FactoryPhonePolicy | None = field(default=None, repr=False)
    request_timeout_seconds: float = 8.0
    total_timeout_seconds: float = 45.0
    concurrency: int = 4
    max_source_records_per_order: int = 32
    max_rows: int = 200
    max_text_characters: int = 2048

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> "LogisticsConfig":
        env = os.environ if environ is None else environ
        def flag(name: str) -> bool:
            value = env.get(name, "false").lower()
            if value not in {"true", "false"}:
                raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
            return value == "true"
        enabled = flag("MAIMI_LOGISTICS_ENABLED")
        provider_enabled = flag("MAIMI_LOGISTICS_KUAIDI100_ENABLED")
        mcp_enabled = flag("MAIMI_LOGISTICS_KUAIDI100_MCP_ENABLED")
        factory_policy_enabled = flag("MAIMI_LOGISTICS_FACTORY_PHONE_POLICY_ENABLED")
        mes_user_code = env.get("MAIMI_LOGISTICS_MES_USER_CODE") or None
        if mes_user_code is not None and mes_user_code != "M00001402":
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        if provider_enabled and mcp_enabled:
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        if not provider_enabled and not mcp_enabled:
            if factory_policy_enabled:
                raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
            return cls(enabled=enabled, mes_user_code=mes_user_code)
        if not enabled:
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        if (not flag("MAIMI_LOGISTICS_KUAIDI100_SINGLE_PROCESS")
                or env.get("WEB_CONCURRENCY", "1") != "1"
                or env.get("UVICORN_WORKERS", "1") != "1"):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        try:
            if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "geteuid"):
                raise ValueError("platform")
            factory_policy = _load_factory_phone_policy() if factory_policy_enabled else None
            fd = os.open(KUAIDI100_MCP_CREDENTIAL_FILE if mcp_enabled else KUAIDI100_CREDENTIAL_FILE, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600
                        or info.st_uid != os.geteuid() or info.st_size > 16_384):
                    raise ValueError("permissions")
                data = _strict_json_object(stream.read(16_385))
            if mcp_enabled:
                if (info.st_size > 8192 or not isinstance(data, dict) or set(data) != {"key"}
                        or not isinstance(data["key"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", data["key"], re.ASCII)):
                    raise ValueError("shape")
                return cls(enabled=True, mes_user_code=mes_user_code,
                           factory_phone_policy=factory_policy,
                           kuaidi100_mcp=Kuaidi100MCPConfig(key=data["key"], single_process_rate_guard=True))
            if (not isinstance(data, dict) or set(data) != {"customer", "key", "carrier_codes"}
                    or not isinstance(data["customer"], str) or not isinstance(data["key"], str)
                    or not isinstance(data["carrier_codes"], dict)
                    or not 1 <= len(data["customer"]) <= 256 or not 1 <= len(data["key"]) <= 512):
                raise ValueError("shape")
            return cls(enabled=True, mes_user_code=mes_user_code, factory_phone_policy=factory_policy,
                       kuaidi100=Kuaidi100Config(**data, single_process_rate_guard=True))
        except Exception:
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION") from None

    def require_ready(self) -> None:
        if not self.enabled:
            raise LogisticsQueryError("NOT_CONFIGURED")

    def availability(self) -> dict[str, Any]:
        return {"enabled": self.enabled, "mes_ready": self.enabled,
                "tracking_ready": bool(self.enabled and (self.kuaidi100_mcp or self.kuaidi100 and self.kuaidi100.carrier_codes)),
                "provider": ("kuaidi100-mcp" if self.kuaidi100_mcp else "kuaidi100") if self.enabled and (self.kuaidi100 or self.kuaidi100_mcp) else None}


def extract_order_codes(text: str) -> list[str]:
    """Extract exact sales IDs from a bounded user message in first-seen order."""
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_QUERY_TEXT or "\x00" in text:
        raise LogisticsQueryError("INVALID_ARGUMENTS")
    normalized = unicodedata.normalize("NFKC", text).upper()
    codes = list(dict.fromkeys(match.group(0) for match in _SALES_IN_TEXT.finditer(normalized)))
    if not codes:
        raise LogisticsQueryError("NO_ORDER_CODES")
    if len(codes) > MAX_ORDERS:
        # No partial request occurs; the caller can split the complete input.
        raise LogisticsQueryError("ORDER_LIMIT_EXCEEDED")
    return codes


def validate_arguments(args: Mapping[str, Any]) -> tuple[list[str], bool]:
    if not isinstance(args, Mapping) or set(args) - {"order_codes", "query_text", "include_tracking"}:
        raise LogisticsQueryError("INVALID_ARGUMENTS")
    codes = args.get("order_codes")
    query_text = args.get("query_text")
    tracking = args.get("include_tracking", True)
    if type(tracking) is not bool or (codes is None) == (query_text is None):
        raise LogisticsQueryError("INVALID_ARGUMENTS")
    if query_text is not None:
        return extract_order_codes(query_text), tracking
    if not isinstance(codes, list) or not 1 <= len(codes) <= MAX_ORDERS:
        raise LogisticsQueryError("INVALID_ARGUMENTS")
    if any(not isinstance(code, str) or not _ORDER.fullmatch(code) for code in codes):
        raise LogisticsQueryError("INVALID_ARGUMENTS")
    if any(code.isdecimal() or _STRONG_REFERENCE.fullmatch(code) for code in codes):
        raise LogisticsQueryError("NO_ORDER_CODES")
    return list(dict.fromkeys(code.upper() for code in codes)), tracking


_DISPLAY_TRACKING = {
    "IN_TRANSIT": "运输中", "COLLECTED": "已揽收", "OUT_FOR_DELIVERY": "派送中",
    "DELIVERED": "已签收", "RETURNED": "已退回", "REFUSED": "拒签",
    "RETURNING": "退回中", "RETURN_SIGNED": "退签", "REJECTION_REQUESTED": "拒收待确认",
    "TRANSFERRED": "转投", "AT_PICKUP_POINT": "已投柜或驿站，待取件",
    "ORDERED": "已下单", "AWAITING_COLLECTION": "待揽收", "CANCELLED": "已销单",
    "AWAITING_CUSTOMS": "待清关", "CUSTOMS_CLEARED": "已清关", "CUSTOMS_EXCEPTION": "清关异常",
    "EXCEPTION": "运输异常", "CUSTOMS": "清关中", "TRACKING_AVAILABLE": "已有物流动态",
    "NO_TRACKING": "暂无物流动态", "NOT_CONFIGURED": "轨迹查询暂不可用",
    "NOT_REQUESTED": "未查询轨迹", "NOT_APPLICABLE": "暂无可查询运单",
    "TRACKING_PENDING": "物流状态待确认", "PHONE_REQUIRED": "需补充寄件电话校验",
    "PHONE_INVALID": "寄件电话校验未通过", "PHONE_MISMATCH": "寄件电话校验未通过",
    "RATE_LIMITED": "查询频繁，请稍后重试", "QUOTA_EXHAUSTED": "轨迹查询暂不可用",
    "PROVIDER_AUTH_ERROR": "轨迹查询暂不可用", "PROVIDER_UNAVAILABLE": "轨迹查询暂不可用",
    "PROVIDER_INVALID_RESPONSE": "物流状态暂未确认", "TRACKING_TIMEOUT": "轨迹查询超时",
    "CARRIER_UNSUPPORTED": "暂不支持该物流公司", "TRACKING_BUDGET_EXCEEDED": "本批查询已达上限，请分批查询",
}
_DISPLAY_QUERY = {
    "MES_EMPTY": "暂无物流单号", "MES_NO_RECORD": "未查到该发货单",
    "MES_UNAVAILABLE": "查询暂不可用", "MES_BUSINESS_REJECTED": "本次查询未成功",
    "MES_INVALID_RESPONSE": "查询结果暂未确认", "MES_TIMEOUT": "查询超时",
}


def _display_text(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        return "—"
    plain = re.sub(r"<br\s*/?>", " ", html.unescape(value), flags=re.IGNORECASE)
    plain = re.sub(r"[\x00-\x1f\x7f]+", " ", plain)
    return re.sub(r"\s+", " ", plain).strip() or "—"


def _markdown_cell(value: Any) -> str:
    text = _display_text(value).replace("\\", "\\\\")
    # Keep upstream text inert even if it contains a link, table or HTML.
    for char in "|`*_[]":
        text = text.replace(char, "\\" + char)
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _display_time(value: Any) -> str:
    stamp = _event_datetime(value) if isinstance(value, str) else None
    return stamp.astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M") if stamp else "—"


def render_logistics_table(result: Mapping[str, Any], orders: list[str]) -> str:
    """One deterministic display row per requested sales shipment, not per parcel."""
    grouped = {order: [] for order in orders}
    for row in result["rows"]:
        if row.get("order_code") in grouped:
            grouped[row["order_code"]].append(row)
    lines = ["| 销售发货单号 | 物流单号/原记录 | 物流公司 | 状态 / 时间 / 物流信息 |",
             "| --- | --- | --- | --- |"]
    truncated = result["truncation"]
    for order, rows in grouped.items():
        waybills, carriers, states = [], [], []
        if not rows:
            waybills, carriers, states = ["—"], ["—"], ["结果未完整返回&#10;&#10;请分批查询"]
        for index, row in enumerate(rows, 1):
            prefix = f"{index}. " if len(rows) > 1 else ""
            waybill = row.get("waybill") or row.get("mes_logistics_text")
            carrier = row.get("tracking_carrier_name") or row.get("carrier")
            status = _DISPLAY_QUERY.get(row.get("query_status"))
            if status is None:
                status = _DISPLAY_TRACKING.get(row.get("tracking_status"), "物流状态待确认")
            state = row.get("tracking_status")
            event, time_line = "暂无物流动态", ""
            if row.get("tracking_time_unverified"):
                status = "物流时间待核对"
            elif row.get("query_status") == "MES_FOUND" and state in (
                    set(_STATE_CODES.values()) | {"RETURNED", "TRACKING_AVAILABLE"}):
                if _display_time(row.get("event_time")) != "—":
                    event = _display_text(row.get("latest_event"))
                if row.get("event_truncated"):
                    event += "（动态内容已截断）"
                if state == "DELIVERED":
                    signed_time = _display_time(row.get("signed_event_time"))
                    time_line = "签收时间：" + (signed_time if signed_time != "—" else "暂未提供")
                elif state in _ETA_ACTIVE_STATES:
                    eta = _eta_time(row.get("estimated_arrival_time"))
                    if eta != "—":
                        time_line = "预计送达：" + eta
                else:
                    latest_time = _display_time(row.get("event_time"))
                    time_line = "动态时间：" + (latest_time if latest_time != "—" else "暂未提供")
            if row.get("mes_text_truncated"):
                waybill = _display_text(waybill) + "（原记录已截断）"
            waybills.append(prefix + _display_text(waybill))
            carriers.append(prefix + _display_text(carrier))
            # Encoded LF stays within one Markdown table row; the native renderer
            # decodes it as text. The exact logistics table gets pre-line CSS.
            # Escape each untrusted block first; only our formatter inserts
            # structural separators, never raw HTML or source-provided markup.
            blocks = [prefix + status]
            if time_line:
                blocks.append(time_line)
            if event != "暂无物流动态" or time_line:
                blocks.append(event)
            states.append("&#10;&#10;".join(_markdown_cell(block) for block in blocks))
        if truncated.get("omitted_rows") or truncated.get("omitted_source_records"):
            states.append("部分记录未返回，请分批查询")
        cells = [_markdown_cell(order), _markdown_cell("；".join(waybills)), _markdown_cell("；".join(carriers)), "&#10;&#10;&#10;".join(states)]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _carrier(text: str) -> str | None:
    folded = text.casefold()
    for prefix, carrier in _RULES:
        if folded.startswith(prefix.casefold()):
            return carrier
    return None


def classify_references(text: str) -> list[tuple[str | None, str | None]]:
    """Extract bounded complete references using explicit carrier/prefix anchors.

    Chinese remarks may surround a reference. Unlabelled numeric substrings and
    separate numeric groups in prose are never assembled into a new waybill.
    """
    stripped = text.strip()
    if not stripped or stripped in _NAME_ONLY:
        return [(None, _carrier(stripped))]
    parts = re.split(r"[,，;；|\r\n]+", stripped)
    parsed: list[tuple[str | None, str | None]] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        carrier = _carrier(part)
        candidate = part
        grouped = re.fullmatch(r"(?:[A-Za-z]{1,5}[0-9]{0,8}|[0-9]{3,4})[ \t]+[0-9]{3,4}(?:[ \t]+[0-9]{3,4})+", part)
        if grouped and carrier and carrier != "自提":
            candidate = re.sub(r"[ \t]", "", part)
        if (carrier and carrier != "自提" and _WAYBILL.fullmatch(candidate)
                and any(c.isdigit() for c in candidate)
                and not candidate.upper().startswith(("SAC", "SDXM"))):
            parsed.append((candidate, carrier))
            continue
        anchored: list[tuple[int, tuple[str | None, str | None]]] = []
        labelled_spans: list[tuple[int, int]] = []
        for match in _LABELLED_REFERENCE.finditer(part):
            labelled_spans.append(match.span())
            label_carrier = _CARRIER_LABELS[match[1]]
            token = match[2]
            token_carrier = _carrier(token) if re.match(r"[A-Za-z]", token, re.ASCII) else None
            # Explicit names outrank workbook-only numeric-prefix heuristics.
            # A contrary strong prefix is a conflict, not permission to guess.
            if (not any(c.isdigit() for c in token)
                    or token.upper().startswith(("SAC", "SDXM"))
                    or token_carrier and token_carrier != label_carrier):
                anchored.append((match.start(), (None, None)))
            else:
                anchored.append((match.start(), (token, label_carrier)))
        for match in _STRONG_REFERENCE.finditer(part):
            if not _WAYBILL.fullmatch(match[0]):
                continue
            if any(start <= match.start() < end for start, end in labelled_spans):
                continue
            # A second separate bare number makes a space-only expression
            # ambiguous. Keep the old refusal rather than drop that number.
            if re.fullmatch(r"[A-Za-z0-9 \t]+", part) and part.strip() != match[0]:
                continue
            anchored.append((match.start(), (match[0], _carrier(match[0]))))
        parsed.extend(value for _, value in sorted(anchored, key=lambda item: item[0]))
        if not anchored:
            parsed.append((None, carrier))
    # More than eight separately written references require clarification; do
    # not fan out hundreds of external queries from a single free-text field.
    return (list(dict.fromkeys(parsed)) if len(parsed) <= 8 else [(None, None)]) or [(None, None)]


def _row(order: str, text: str | None, query_status: str,
         waybill: str | None = None, carrier: str | None = None,
         tracking_status: str = "NOT_APPLICABLE") -> dict[str, Any]:
    source = RULE_SOURCE if carrier else None
    if text and waybill and carrier:
        if any(match[2] == waybill and _CARRIER_LABELS[match[1]] == carrier
               for match in _LABELLED_REFERENCE.finditer(text)):
            source = "MES_EXPLICIT_CARRIER_LABEL"
        elif text.strip() != waybill and _STRONG_REFERENCE.search(text):
            source = "MES_WAYBILL_PREFIX"
    return {"order_code": order, "mes_logistics_text": text, "waybill": waybill,
            "carrier": carrier, "rule_source": source,
            "tracking_status": tracking_status, "latest_event": None,
            "event_time": None, "query_status": query_status}


class LogisticsQueryService:
    """Reuse one instance per process to preserve its Kuaidi100 rate guard.

    The optional adapter's half-hour guard is in-process. A multi-worker deployment
    must provide a shared guard before enabling it. This candidate is disabled by
    default and performs no account registration, subscription or retries.
    """
    def __init__(self, config: LogisticsConfig | None = None, *,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.config = config or LogisticsConfig()
        c = self.config
        if not (type(c.enabled) is bool and 1 <= c.concurrency <= 4 and 0 < c.request_timeout_seconds <= 30
                and 0 < c.total_timeout_seconds <= 45 and 1 <= c.max_rows <= 200
                and 1 <= c.max_source_records_per_order <= 32
                and 1 <= c.max_text_characters <= 2048):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        provider = c.kuaidi100
        mcp = c.kuaidi100_mcp
        if (c.mes_user_code not in {None, "M00001402"} or (provider and mcp)
                or mcp and (not isinstance(mcp.key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,512}", mcp.key, re.ASCII)
                            or mcp.single_process_rate_guard is not True)):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        if provider and (not provider.customer or not provider.key or provider.single_process_rate_guard is not True
                         or any(not isinstance(k, str) or not isinstance(v, str)
                                or not re.fullmatch(r"[a-z0-9_]{1,40}", v)
                                for k, v in provider.carrier_codes.items())):
            raise LogisticsQueryError("INVALID_SERVER_CONFIGURATION")
        self._transport = transport
        self._semaphore = asyncio.Semaphore(c.concurrency)
        self._provider_last_attempt: dict[str, float] = {}

    async def _authorized(self, authorize: Authorize) -> None:
        try:
            decision = authorize()
            if inspect.isawaitable(decision):
                decision = await asyncio.wait_for(decision, timeout=3.0)
            if decision is not None and decision is not True:
                raise LogisticsQueryError("AUTHORIZATION_LOST")
        except asyncio.CancelledError:
            raise
        except Exception:
            raise LogisticsQueryError("AUTHORIZATION_LOST") from None

    async def _read_json(self, client: httpx.AsyncClient, url: str, **kwargs: Any) -> dict[str, Any]:
        try:
            async with client.stream("POST", url, **kwargs) as response:
                if response.status_code != 200:
                    raise LogisticsQueryError("PROVIDER_UNAVAILABLE")
                raw_length = response.headers.get("content-length", "")
                if raw_length.isdigit() and int(raw_length) > MAX_RESPONSE_BYTES:
                    raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE")
                    data.extend(chunk)
                result = json.loads(data)
                if not isinstance(result, dict):
                    raise ValueError("shape")
                return result
        except asyncio.CancelledError:
            raise
        except LogisticsQueryError:
            raise
        except httpx.TimeoutException:
            raise LogisticsQueryError("PROVIDER_TIMEOUT") from None
        except (httpx.HTTPError, OSError):
            raise LogisticsQueryError("PROVIDER_UNAVAILABLE") from None
        except (ValueError, TypeError, RecursionError):
            raise LogisticsQueryError("PROVIDER_INVALID_RESPONSE") from None

    async def _tracking_phone(self, row: dict[str, Any], user_no: str,
                              resolver: PhoneResolver | None,
                              record_phone: _RecordPhone | None,
                              record_context: _RecordContext | None = None) -> tuple[str, str | None]:
        override = self._factory_phone_override(row, record_context)
        if override is not None:
            return override, None
        if record_phone is not None:
            return record_phone.suffix or "", record_phone.status
        # Preserve integrations with a trusted scoped resolver for older MES
        # contracts. An explicitly blank/invalid MES field never falls back.
        resolved = None
        if resolver:
            try:
                resolved = resolver(user_no, row["order_code"], row["carrier"], row["waybill"])
                if inspect.isawaitable(resolved):
                    resolved = await asyncio.wait_for(resolved, self.config.request_timeout_seconds)
            except asyncio.CancelledError:
                raise
            except Exception:
                resolved = None
        if not resolved:
            return "", "PHONE_REQUIRED"
        if not isinstance(resolved, str) or not re.fullmatch(r"[0-9]{4,20}", resolved, re.ASCII):
            return "", "PHONE_INVALID"
        return resolved, None

    def _factory_phone_override(self, row: dict[str, Any], context: _RecordContext | None) -> str | None:
        policy = self.config.factory_phone_policy
        if policy is None or context is None:
            return None
        factory = policy.aliases.get(context.factory or "")
        if factory == "ruixinte" and row["carrier"] in {"顺丰快递", "顺丰快运", "中通"}:
            return policy.ruixinte_sender_suffix
        if factory == "shuote" and row["carrier"] == "跨越":
            return policy.shuote_kuayue_phone
        return None

    async def _tracking_mcp(self, context: _MCPQueryContext, row: dict[str, Any],
                            user_no: str, authorize: Authorize, budget: dict[str, int],
                            record_phone: _RecordPhone | None = None,
                            record_context: _RecordContext | None = None) -> None:
        config = self.config.kuaidi100_mcp
        if config is None:
            row["tracking_status"] = "NOT_CONFIGURED"
            return
        phone = ""
        if (row["carrier"] in {"顺丰快递", "顺丰快运", "中通"}
                or self._factory_phone_override(row, record_context) is not None):
            phone, phone_status = await self._tracking_phone(row, user_no, config.phone_resolver, record_phone, record_context)
            if phone_status:
                row["tracking_status"] = phone_status
                return
        # Serialize this query's client so authority is rechecked after waiting
        # for the connection, rather than before another row's slow request.
        async with context.lock:
            await self._authorized(authorize)
            rate_key = hashlib.sha256(("mcp:" + row["waybill"].upper()).encode()).hexdigest()
            now = time.monotonic()
            self._provider_last_attempt = {k: v for k, v in self._provider_last_attempt.items() if now - v < 1800}
            if rate_key in self._provider_last_attempt or len(self._provider_last_attempt) >= 2048:
                row["tracking_status"] = "RATE_LIMITED"
                return
            if budget["requests"] >= MAX_TRACKING_REQUESTS:
                row["tracking_status"] = "TRACKING_BUDGET_EXCEEDED"
                return
            if context.unavailable:
                row["tracking_status"] = context.unavailable
                return
            if context.client is None:
                try:
                    context.client = await context.stack.enter_async_context(Kuaidi100MCPClient(
                        MCPTransportConfig(key=config.key,
                            request_timeout_seconds=min(15.0, self.config.request_timeout_seconds),
                            operation_timeout_seconds=min(30.0, self.config.total_timeout_seconds)),
                        transport=self._transport))
                    found = await context.client.discover()
                    if not found.query_tool_available:
                        raise Kuaidi100MCPError("MCP_QUERY_CONTRACT_UNAVAILABLE")
                    context.phone_parameter_available = found.phone_parameter_available
                    context.eta_tool_available = found.eta_tool_available
                    context.eta_product_required = found.eta_product_required
                except asyncio.CancelledError:
                    raise
                except Kuaidi100MCPError as exc:
                    context.unavailable = _MCP_ERROR_CODES.get(exc.code, "PROVIDER_UNAVAILABLE")
                except Exception:
                    context.unavailable = "PROVIDER_UNAVAILABLE"
                if context.unavailable:
                    row["tracking_status"] = context.unavailable
                    return
            if phone and not context.phone_parameter_available:
                row["tracking_status"] = "PROVIDER_INVALID_RESPONSE"
                return
            await self._authorized(authorize)
            # Other concurrently running employee queries share this guard.
            # Discovery awaited network I/O, so repeat the claim check here.
            now = time.monotonic()
            if rate_key in self._provider_last_attempt or len(self._provider_last_attempt) >= 2048:
                row["tracking_status"] = "RATE_LIMITED"
                return
            budget["requests"] += 1
            self._provider_last_attempt[rate_key] = now
            payload = None
            try:
                result = await context.client.query_trace(row["waybill"], phone=phone)
                await self._authorized(authorize)
                payload = _apply_mcp_trace(row, result, private_phone=phone)
            except asyncio.CancelledError:
                raise
            except LogisticsQueryError as exc:
                if exc.code == "AUTHORIZATION_LOST":
                    raise
                row["tracking_status"] = exc.code if exc.code in _TRACKING_ERRORS else "PROVIDER_UNAVAILABLE"
            except Kuaidi100MCPError as exc:
                row["tracking_status"] = _MCP_ERROR_CODES.get(exc.code, "PROVIDER_UNAVAILABLE")
            except Exception:
                row["tracking_status"] = "PROVIDER_UNAVAILABLE"
                return
            # Optional forecasts cannot convert successful tracking into a
            # failed query. All inputs are from the already matched response.
            if (row.get("tracking_status") not in _ETA_ACTIVE_STATES or row.get("estimated_arrival_time")
                    or not context.eta_tool_available or not payload):
                return
            args = _eta_arguments(payload, require_product=context.eta_product_required,
                                  record_destination=record_context.destination if record_context else None)
            if not args:
                row["eta_status"] = "SOURCE_INCOMPLETE_OR_UNSUPPORTED"
                return
            if budget.get("eta_requests", 0) >= MAX_ETA_REQUESTS:
                row["eta_status"] = "BUDGET_EXCEEDED"
                return
            await self._authorized(authorize)
            budget["eta_requests"] = budget.get("eta_requests", 0) + 1
            try:
                response = await asyncio.wait_for(context.client.estimate_time_with_logistic(**args),
                                                  timeout=min(8.0, self.config.request_timeout_seconds))
                await self._authorized(authorize)
                _apply_mcp_eta(row, response, args)
            except asyncio.CancelledError:
                raise
            except LogisticsQueryError as exc:
                if exc.code == "AUTHORIZATION_LOST":
                    raise
                row["eta_status"] = "UNAVAILABLE"
            except (Kuaidi100MCPError, asyncio.TimeoutError):
                row["eta_status"] = "UNAVAILABLE"
            except Exception:
                row["eta_status"] = "UNAVAILABLE"

    async def _tracking(self, client: httpx.AsyncClient, row: dict[str, Any],
                        user_no: str, authorize: Authorize, budget: dict[str, int],
                        mcp_context: _MCPQueryContext, record_phone: _RecordPhone | None = None,
                        record_context: _RecordContext | None = None) -> None:
        if self.config.kuaidi100_mcp:
            await self._tracking_mcp(mcp_context, row, user_no, authorize, budget, record_phone, record_context)
            return
        config = self.config.kuaidi100
        if not config:
            row["tracking_status"] = "NOT_CONFIGURED"
            return
        company = config.carrier_codes.get(row["carrier"])
        if not company:
            row["tracking_status"] = "CARRIER_UNSUPPORTED"
            return
        params = {"com": company, "num": row["waybill"], "show": "0", "order": "desc", "lang": "zh"}
        if company in _PHONE_CARRIERS or (company == "kuayue" and self._factory_phone_override(row, record_context) is not None):
            phone, phone_status = await self._tracking_phone(row, user_no, config.phone_resolver, record_phone, record_context)
            if phone_status:
                row["tracking_status"] = phone_status
                return
            params["phone"] = phone
        await self._authorized(authorize)
        rate_key = hashlib.sha256((company + ":" + row["waybill"].upper()).encode()).hexdigest()
        now = time.monotonic()
        self._provider_last_attempt = {k: v for k, v in self._provider_last_attempt.items() if now - v < 1800}
        if rate_key in self._provider_last_attempt or len(self._provider_last_attempt) >= 2048:
            row["tracking_status"] = "RATE_LIMITED"
            return
        param = json.dumps(params, ensure_ascii=False, separators=(",", ":"))
        sign = hashlib.md5((param + config.key + config.customer).encode("utf-8")).hexdigest().upper()
        # One shared per-query counter bounds optional paid/provider work even
        # when MES returns many records and several references in each record.
        # No await separates the check, claim and rate-key claim. A budget-
        # rejected reference never consumes its 30-minute per-waybill guard.
        if budget["requests"] >= MAX_TRACKING_REQUESTS:
            row["tracking_status"] = "TRACKING_BUDGET_EXCEEDED"
            return
        budget["requests"] += 1
        self._provider_last_attempt[rate_key] = now
        try:
            result = await self._read_json(client, KUAIDI100_ENDPOINT,
                                          data={"customer": config.customer, "sign": sign, "param": param})
        except LogisticsQueryError as exc:
            row["tracking_status"] = "TRACKING_TIMEOUT" if exc.code == "PROVIDER_TIMEOUT" else exc.code
            return
        code = str(result.get("returnCode", result.get("status", "")))
        if code != "200" or result.get("result") is False:
            row["tracking_status"] = {"408": "PHONE_MISMATCH", "500": "NO_TRACKING",
                "503": "PROVIDER_AUTH_ERROR", "504": "RATE_LIMITED", "601": "QUOTA_EXHAUSTED",
                "401": "CARRIER_UNSUPPORTED"}.get(code, "PROVIDER_UNAVAILABLE")
            return
        if (str(result.get("nu", "")).upper() != row["waybill"].upper()
                or result.get("com") != company or not isinstance(result.get("data"), list)):
            row["tracking_status"] = "PROVIDER_INVALID_RESPONSE"
            return
        events = result["data"]
        if not events:
            row["tracking_status"] = "NO_TRACKING"
            return
        event = events[0]
        if (not isinstance(event, dict) or not isinstance(event.get("context"), str)
                or not isinstance(event.get("ftime", event.get("time")), str)):
            row["tracking_status"] = "PROVIDER_INVALID_RESPONSE"
            return
        row["latest_event"] = _private_phone_text(event["context"], params.get("phone", ""))[:1024]
        row["event_time"] = event.get("ftime", event.get("time"))[:64]
        row["event_truncated"] = len(event["context"]) > 1024
        row["tracking_status"] = _tracking_state(result.get("state"))
        if row["tracking_status"] in _ETA_ACTIVE_STATES and _eta_time(result.get("arrivalTime")) != "—":
            row["estimated_arrival_time"] = result["arrivalTime"]
            row["eta_source"] = "KUAIDI100_REALTIME_QUERY"
            row["eta_status"] = "AVAILABLE"
        if row["tracking_status"] == "DELIVERED":
            signed = [e for e in events if isinstance(e, dict) and _signed_event(e)
                      and _event_datetime(e.get("ftime", e.get("time"))) is not None]
            if signed:
                signed_event = max(signed, key=lambda e: _event_datetime(e.get("ftime", e.get("time"))))
                row["signed_event_time"] = signed_event.get("ftime", signed_event.get("time"))

    async def _order(self, client: httpx.AsyncClient, order: str, user_no: str,
                     include_tracking: bool, authorize: Authorize,
                     progress: dict[str, tuple[list[dict[str, Any]], int, int]],
                     budget: dict[str, int], mcp_context: _MCPQueryContext) -> tuple[list[dict[str, Any]], int, int]:
        async with self._semaphore:
            await self._authorized(authorize)
            try:
                result = await self._read_json(client, MES_ENDPOINT, headers=dict(self.config.mes_headers), json={
                    "module": "query_sa_record", "user_code": self.config.mes_user_code or user_no,
                    "filters": [{"field": "sa_code", "operator": "eq", "value": order}]})
            except LogisticsQueryError as exc:
                state = {"PROVIDER_TIMEOUT": "MES_TIMEOUT", "PROVIDER_INVALID_RESPONSE": "MES_INVALID_RESPONSE"}.get(exc.code, "MES_UNAVAILABLE")
                return [_row(order, None, state)], 0, 0
            if type(result.get("code")) is not int:
                return [_row(order, None, "MES_INVALID_RESPONSE")], 0, 0
            if result["code"] != 0:
                return [_row(order, None, "MES_BUSINESS_REJECTED")], 0, 0
            source = result.get("ObjectData")
            if not isinstance(source, list):
                return [_row(order, None, "MES_INVALID_RESPONSE")], 0, 0
            if not source:
                return [_row(order, None, "MES_NO_RECORD")], 0, 0
            rows: list[dict[str, Any]] = []
            pending: list[tuple[dict[str, Any], _RecordContext]] = []
            for record in source[:self.config.max_source_records_per_order]:
                if not isinstance(record, dict) or "物流单号" not in record or (record["物流单号"] is not None and not isinstance(record["物流单号"], str)):
                    rows.append(_row(order, None, "MES_INVALID_RESPONSE"))
                    continue
                text = record["物流单号"]
                if text is None or not text.strip():
                    rows.append(_row(order, text, "MES_EMPTY"))
                    continue
                truncated = len(text) > self.config.max_text_characters
                bounded_text = text[:self.config.max_text_characters]
                # Never query a reference obtained from cutting off its suffix.
                refs = [(None, None)] if truncated else classify_references(text)
                # A shared address attached to a free-text list of waybills has
                # no proven per-waybill association. Keep tracing those rows,
                # but do not borrow that address to forecast each parcel.
                record_context = _record_context(record, allow_destination=len(refs) == 1)
                for waybill, carrier in refs:
                    row = _row(order, bounded_text, "MES_FOUND", waybill, carrier)
                    if truncated:
                        row["mes_text_truncated"] = True
                        row["original_text_characters"] = len(text)
                    if not include_tracking:
                        row["tracking_status"] = "NOT_REQUESTED"
                    elif waybill:
                        row["tracking_status"] = "TRACKING_PENDING"
                        pending.append((row, record_context))
                    rows.append(row)
            snapshot = rows, len(source), max(0, len(source) - self.config.max_source_records_per_order)
            # Publish the complete MES-only snapshot before the first carrier
            # await, so a timeout preserves every already-received MES value.
            progress[order] = snapshot
            for row, record_context in pending:
                await self._tracking(client, row, user_no, authorize, budget, mcp_context, record_context.phone, record_context)
            return snapshot

    async def query(self, args: Mapping[str, Any], *, trusted_user_no: str,
                    authorize: Authorize) -> dict[str, Any]:
        orders, tracking = validate_arguments(args)
        self.config.require_ready()
        if not isinstance(trusted_user_no, str) or not _IDENTITY.fullmatch(trusted_user_no):
            raise LogisticsQueryError("AUTHORIZATION_LOST")
        await self._authorized(authorize)
        timeout = httpx.Timeout(self.config.request_timeout_seconds)
        async with AsyncExitStack() as stack, httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False,
                                                               transport=self._transport) as client:
            progress: dict[str, tuple[list[dict[str, Any]], int, int]] = {}
            budget = {"requests": 0}
            mcp_context = _MCPQueryContext(stack)
            tasks = [asyncio.create_task(self._order(client, code, trusted_user_no, tracking, authorize, progress, budget, mcp_context)) for code in orders]
            try:
                done, pending = await asyncio.wait(tasks, timeout=self.config.total_timeout_seconds)
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                batches = []
                for code, task in zip(orders, tasks):
                    if task in done:
                        batches.append(task.result())
                    elif code in progress:
                        snapshot = progress[code]
                        for row in snapshot[0]:
                            if row["tracking_status"] == "TRACKING_PENDING":
                                row["tracking_status"] = "TRACKING_TIMEOUT"
                        batches.append(snapshot)
                    else:
                        batches.append(([_row(code, None, "MES_TIMEOUT")], 0, 0))
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        await self._authorized(authorize)
        rows = [row for batch, _, _ in batches for row in batch]
        budget_exceeded_rows = sum(row["tracking_status"] == "TRACKING_BUDGET_EXCEEDED" for row in rows)
        source_records = sum(count for _, count, _ in batches)
        skipped_records = sum(count for _, _, count in batches)
        omitted_rows = max(0, len(rows) - self.config.max_rows)
        rows = rows[:self.config.max_rows]
        result = {"schema": "logistics-query-result-v1", "status": "completed",
                  "queried_at": datetime.now(timezone.utc).isoformat(), "rows": rows,
                  "order_codes": orders, "display_schema": "logistics-table-v1", "display_layout": "logistics-multiline-v2",
                  "counts": {"requested_orders": len(args["order_codes"]) if args.get("order_codes") is not None else len(orders), "queried_orders": len(orders),
                             "source_records": source_records, "returned_rows": len(rows),
                             "tracking_requests": budget["requests"], "tracking_budget_exceeded_rows": budget_exceeded_rows,
                             "eta_requests": budget.get("eta_requests", 0),
                             "eta_budget_exceeded_rows": sum(row.get("eta_status") == "BUDGET_EXCEEDED" for row in rows)},
                  "truncation": {"truncated": False, "omitted_source_records": skipped_records,
                                 "omitted_rows": omitted_rows, "text_truncated_rows": 0}}
        def refresh() -> None:
            result["counts"]["returned_rows"] = len(rows)
            trunc = result["truncation"]
            trunc["text_truncated_rows"] = sum(bool(r.get("mes_text_truncated") or r.get("event_truncated")) for r in rows)
            trunc["truncated"] = bool(trunc["omitted_source_records"] or trunc["omitted_rows"] or trunc["text_truncated_rows"])
            result["status"] = "partial" if (trunc["truncated"] or any(r.get("tracking_time_unverified") is True for r in rows) or any(
                r["query_status"] in {"MES_UNAVAILABLE", "MES_INVALID_RESPONSE", "MES_BUSINESS_REJECTED", "MES_TIMEOUT"}
                or r["tracking_status"] in _TRACKING_ERRORS for r in rows)) else "completed"
            result["display_markdown"] = render_logistics_table(result, orders)
        refresh()
        # ASCII escaping plus default JSON whitespace is the larger encoding;
        # the 85 KB ceiling also leaves space for the native 90 KB wrapper.
        while len(json.dumps(result, ensure_ascii=True).encode("utf-8")) > MAX_RESULT_BYTES and rows:
            rows.pop()
            result["truncation"]["omitted_rows"] += 1
            refresh()
        # A final check covers authorization revoked while constructing a result.
        await self._authorized(authorize)
        return result
