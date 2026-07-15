from __future__ import annotations

import pytest

from xnestdm.helper_protocol import (
    MAX_MESSAGE_SIZE,
    PROTOCOL_VERSION,
    ProtocolError,
    decode_message,
    encode_message,
)


def test_protocol_round_trip() -> None:
    value = {"protocol": PROTOCOL_VERSION, "id": 1, "op": "shutdown"}
    assert decode_message(encode_message(value).rstrip(b"\n")) == value


def test_protocol_rejects_oversized_messages() -> None:
    with pytest.raises(ProtocolError):
        encode_message({"protocol": PROTOCOL_VERSION, "value": "x" * MAX_MESSAGE_SIZE})
