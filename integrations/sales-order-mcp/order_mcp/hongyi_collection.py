"""EDRMS V6 JSON collection of a sales order whose original is in ECM."""
import os
import re
import uuid
from urllib.parse import urlsplit

import httpx

FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,127}\Z")
STATUSES = ("accepted", "duplicate", "archived")


def valid_map(value):
    return isinstance(value, dict) and all(isinstance(k, str) and FIELD.fullmatch(k) and
        isinstance(v, str) and FIELD.fullmatch(v) for k, v in value.items())


def predicate(value):
    return isinstance(value, dict) and isinstance(value.get("field"), str) and all(
        FIELD.fullmatch(x) for x in value["field"].split(".")) and type(value.get("equals")) in (str, int, bool) and value["equals"] != ""


def missing_settings(cfg):
    missing = []
    try:
        url = urlsplit(cfg.get("submit_url") or "")
        valid = (url.scheme == "https" or (url.scheme == "http" and cfg.get("allow_internal_http") is True)) and (
            url.hostname and not url.username and not url.password and not url.query and not url.fragment and
            re.fullmatch(r"/(?:[a-zA-Z0-9_-]+/)*collectArchives/submitMetadata/[a-zA-Z0-9_-]+", url.path))
    except (TypeError, ValueError):
        valid = False
    if not valid:
        missing.append("submit_url")
    rule = cfg.get("rule_name")
    if (not isinstance(rule, str) or not rule.strip() or len(rule)>256 or rule != rule.strip()
            or any(s in rule for s in ("客户基础资料","销售物流资料"))):
        missing.append("rule_name")
    env = cfg.get("token_env")
    if not isinstance(env, str) or not FIELD.fullmatch(env) or not os.getenv(env):
        missing.append("token_env")
    fields = cfg.get("content_fields")
    if not fields or not valid_map(fields) or "uniqueId" in fields:
        missing.append("content_fields")
    defaults = cfg.get("content_defaults", {})
    if not isinstance(defaults, dict) or any(not isinstance(k, str) or not FIELD.fullmatch(k) or k == "uniqueId" or
            type(v) not in (str, int, bool) or (isinstance(fields, dict) and k in fields) for k,v in defaults.items()):
        missing.append("content_defaults")
    for key in ("activity_fields", "relation_fields"):
        if not valid_map(cfg.get(key, {})):
            missing.append(key)
    for status in STATUSES:
        key = status+"_response"
        if (status == "accepted" or key in cfg) and not predicate(cfg.get(key)):
            missing.append(key)
    receipt_field = cfg.get("receipt_field")
    if receipt_field is not None and (not isinstance(receipt_field,str) or not all(FIELD.fullmatch(k) for k in receipt_field.split("."))):
        missing.append("receipt_field")
    return missing


def status(cfg):
    missing = missing_settings(cfg)
    return {"mode": "edrms_v6_ecm", "ready": not missing, "missing": missing}


def file_type(cfg,source_name):
    extension = source_name.rsplit(".",1)[-1].lower() if "." in source_name else ""
    types = cfg.get("file_types", {})
    filetype = types.get(extension,extension) if isinstance(types,dict) else None
    if not isinstance(filetype,str) or not filetype.strip():
        raise ValueError("鸿翼销售订单规则缺少原件的 fileType。")
    return filetype


def mapped(fields, values):
    result = {}
    for destination, local in fields.items():
        value = values.get(local)
        if type(value) not in (str,int,float,bool) or (isinstance(value,str) and not value.strip()):
            raise ValueError(f"鸿翼销售订单规则缺少映射值 {local}（{destination}）。")
        result[destination] = str(value)
    return result


def validate(cfg, values=None):
    missing = missing_settings(cfg)
    if missing:
        raise ValueError("请补齐鸿翼销售订单专用收集配置：" + "、".join(missing))
    if values is not None:
        for key in ("content_fields", "activity_fields", "relation_fields"):
            mapped(cfg.get(key, {}), values)


def lookup(response, field):
    for part in field.split("."):
        if not isinstance(response, dict) or part not in response:
            return None
        response = response[part]
    return response


async def collect(client, cfg, values, key, sources):
    validate(cfg,values)
    content = {**cfg.get("content_defaults", {}), **mapped(cfg["content_fields"], values), "uniqueId": key}
    documents = []
    for source_name, file_id, original_key in sources:
        documents.append({"sourceFileName":source_name, "downloadType":"ecm","downloadContent":str(file_id),
                          "uniqueId":str(uuid.uuid5(uuid.NAMESPACE_URL,"maimi-sales-order:original:"+original_key)),
                          "fileType":file_type(cfg,source_name)})
    if not documents or len({item["downloadContent"] for item in documents}) != len(documents):
        raise ValueError("原件回执不完整或 fileId 重复，请核查企业库。")
    block = {"contentDescnption": content, "documents":documents,
             "businessEntity": [], "fileEntityRelationShip": [], "extend": ""}
    for field, setting in (("businessEntity","activity_fields"),("fileEntityRelationShip","relation_fields")):
        if cfg.get(setting):
            block[field].append(mapped(cfg[setting],values))
    body = {"ruleName": cfg["rule_name"], "token": os.environ[cfg["token_env"]], "fileEntityBlock": [block]}
    try:
        reply = await client.post(cfg["submit_url"], json=body)
        if not 200 <= reply.status_code < 300:
            raise ValueError("鸿翼收集请求未获确认，请核查收集记录。")
        response = reply.json()
    except (httpx.HTTPError, ValueError):
        raise ValueError("鸿翼收集回执未确认，请核查收集记录。") from None
    matched = []
    for name in STATUSES:
        match = cfg.get(name+"_response")
        if match:
            actual = lookup(response,match["field"])
            if type(actual) is type(match["equals"]) and actual == match["equals"]:
                matched.append(name)
    if len(matched) != 1:
        raise ValueError("鸿翼收集回执状态无法唯一确认，请核查收集记录。")
    receipt = {"business_unique_id": key, "original_unique_id": documents[0]["uniqueId"]}
    if len(documents)>1:
        receipt["original_unique_ids"] = [doc["uniqueId"] for doc in documents]
    if cfg.get("receipt_field"):
        identifier = lookup(response,cfg["receipt_field"])
        if identifier is None and matched[0] == "duplicate":
            return {"collection_status":"duplicate","collection_receipt":receipt}
        if type(identifier) not in (str,int) or not re.fullmatch(r"[\w.-]{1,180}",str(identifier)):
            raise ValueError("鸿翼收集回执缺少配置的回执编号，请核查收集记录。")
        receipt["receipt_id"] = str(identifier)
    return {"collection_status": matched[0], "collection_receipt":receipt}
