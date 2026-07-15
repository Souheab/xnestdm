from __future__ import annotations

import json
from typing import Any, Mapping

PROTOCOL_VERSION = 1
MAX_MESSAGE_SIZE = 64 * 1024


class ProtocolError(ValueError):
    pass


def encode_message(message: Mapping[str, object]) -> bytes:
    payload = (
        json.dumps(
            message,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(payload) > MAX_MESSAGE_SIZE:
        raise ProtocolError("Privileged-helper message is too large")
    return payload


def decode_message(payload: bytes) -> dict[str, Any]:
    if not payload or len(payload) > MAX_MESSAGE_SIZE:
        raise ProtocolError("Invalid privileged-helper message size")
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("Invalid JSON from privileged helper") from exc
    if not isinstance(value, dict):
        raise ProtocolError("Privileged-helper message must be an object")
    if value.get("protocol") != PROTOCOL_VERSION:
        raise ProtocolError("Unsupported privileged-helper protocol")
    return value


def message(op: str, request_id: int, **values: object) -> dict[str, object]:
    return {
        "protocol": PROTOCOL_VERSION,
        "id": request_id,
        "op": op,
        **values,
    }
