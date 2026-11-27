"""Private-chat routing for the proposed employee relay.

Called only by an authenticated channel receiver, after platform decoding.
The receiver supplies the *local* tenant and bot identity of its connection.
No HTTP route, account login, reply or employee credential is created here.
"""
from dataclasses import dataclass
from hashlib import sha256
import hmac
import json
import re
from typing import Callable, Iterable

OWNER = re.compile(r"M[0-9]{8}")


class Refused(ValueError):
    """A fixed reason suitable for numeric counters, without peer identifiers."""


@dataclass(frozen=True, repr=False)
class Channel:
    provider: str
    bot: str
    tenant: str


@dataclass(frozen=True, repr=False)
class Binding:
    channel: Channel
    subject: str
    employee: str
    generation: str
    valid_since: int
    valid_until: int
    enabled: bool


@dataclass(frozen=True, repr=False)
class ReceivedText:
    channel: Channel
    sender: str
    conversation: str
    message_id: str
    text: str
    created_at: int
    direct: bool


@dataclass(frozen=True, repr=False)
class Route:
    employee: str
    conversation_key: str
    receipt_key: str
    binding_generation: str


def _id(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise Refused("INVALID_IDENTIFIER")
    if not value.isprintable() or value.strip() != value:
        raise Refused("INVALID_IDENTIFIER")
    return value


def dingtalk_text(connection: Channel, payload: dict) -> ReceivedText:
    if connection.provider != "dingtalk" or not connection.tenant or not connection.bot:
        raise Refused("CONNECTION_MISMATCH")
    if (payload.get("robotCode") != connection.bot or
        payload.get("senderCorpId") != connection.tenant or
        payload.get("chatbotCorpId") != connection.tenant):
        raise Refused("ORGANIZATION_OR_BOT_MISMATCH")
    if str(payload.get("conversationType")) != "1" or payload.get("msgtype") != "text":
        raise Refused("PRIVATE_TEXT_REQUIRED")
    timestamp = payload.get("createAt")
    if type(timestamp) != int or timestamp < 0:
        raise Refused("TIMESTAMP_REQUIRED")
    body = payload.get("text")
    return ReceivedText(connection, _id(payload.get("senderStaffId")),
                        _id(payload.get("conversationId")), _id(payload.get("msgId")),
                        body.get("content") if isinstance(body, dict) else None, timestamp // 1000, True)


def resolve(event: ReceivedText, bindings: Iterable[Binding], active_employee: Callable[[str], bool],
            *, now: int, routing_key: bytes) -> Route:
    """Resolve a verified direct, fresh text event against exactly one unexpired binding.

    The caller must atomically claim receipt_key before dispatch and use an employee-scoped
    gateway grant. This function alone neither claims the receipt nor authorizes a gateway call.
    """
    if len(routing_key) < 32:
        raise Refused("ROUTING_KEY_INVALID")
    channel = event.channel
    if channel.provider not in ("dingtalk","weixin"):
        raise Refused("CHANNEL_UNKNOWN")
    for value in (channel.bot, channel.tenant, event.conversation, event.sender, event.message_id):
        _id(value)
    if event.direct is not True or not isinstance(event.text, str) or not event.text.strip():
        raise Refused("PRIVATE_TEXT_REQUIRED")
    if len(event.text) > 8000 or event.text.lstrip().startswith("/"):
        raise Refused("UNSUPPORTED_CONTENT")
    if type(event.created_at) != int or not now - 300 <= event.created_at <= now + 30:
        raise Refused("EVENT_EXPIRED")
    matches = [b for b in bindings if b.channel == channel and b.subject == event.sender and b.enabled is True]
    if len(matches) != 1:
        raise Refused("EMPLOYEE_BINDING_REQUIRED")
    binding = matches[0]
    if not OWNER.fullmatch(binding.employee) or not binding.valid_since <= min(now,event.created_at) or now >= binding.valid_until:
        raise Refused("EMPLOYEE_BINDING_INACTIVE")
    if active_employee(binding.employee) is not True:
        raise Refused("EMPLOYEE_BINDING_INACTIVE")
    _id(binding.generation)
    def fingerprint(*components):
        data = json.dumps(components, ensure_ascii=False, separators=(",", ":")).encode("utf8")
        return hmac.new(routing_key, data, sha256).hexdigest()
    account = (channel.provider, channel.tenant, channel.bot)
    conversation = fingerprint("conversation", *account, event.sender, event.conversation, binding.employee, binding.generation)
    # Redelivery uses the same receipt even after a binding changes.
    receipt = fingerprint("receipt", *account, event.sender, event.message_id)
    return Route(binding.employee, conversation, receipt, binding.generation)

