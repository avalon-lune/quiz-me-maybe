"""Bounded, length-prefixed JSON messages shared by both TCP endpoints."""

from __future__ import annotations

import json
import math
import re
import struct
from typing import Any

VERSION = 1
MAX_FRAME_BYTES = 65_536
MAX_REQUEST_ID = 2_147_483_647
_HEADER = struct.Struct("!I")
_ACTION = re.compile(r"[a-z_]{1,32}\Z")


class ProtocolError(ValueError):
    """An invalid frame or message; callers decide whether recovery is safe."""

    def __init__(self, message: str, code: str = "INVALID_MESSAGE") -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _object(pairs: list[tuple[str, Any]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise ProtocolError("JSON object contains a duplicate key.")
        result[key] = value
    return result


def _reject_constant(value: str) -> None:
    raise ProtocolError("JSON numbers must be finite.")


def _check_value(value: Any, depth: int = 0) -> None:
    if depth > 32:
        raise ProtocolError("JSON nesting exceeds 32 levels.")
    if isinstance(value, str):
        if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
            raise ProtocolError("JSON strings must contain Unicode scalar values.")
    elif isinstance(value, float) and not math.isfinite(value):
        raise ProtocolError("JSON numbers must be finite.")
    elif isinstance(value, dict):
        for key, item in value.items():
            _check_value(key, depth + 1)
            _check_value(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_value(item, depth + 1)


def encode_message(message: dict) -> bytes:
    """Prefix the encoded byte count, which can differ from character count."""
    if not isinstance(message, dict):
        raise ProtocolError("Message must be a JSON object.")
    try:
        _check_value(message)
        payload = json.dumps(
            message, ensure_ascii=False, allow_nan=False, separators=(",", ":")
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ProtocolError("Message cannot be encoded as UTF-8 JSON.") from error
    if not 1 <= len(payload) <= MAX_FRAME_BYTES:
        raise ProtocolError("Message exceeds the frame size limit.", "INVALID_FRAME")
    return _HEADER.pack(len(payload)) + payload


class FrameDecoder:
    """Keep incomplete bytes; pop exactly one complete message at a time."""

    def __init__(self) -> None:
        self._buffer = bytearray()

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    def feed(self, data: bytes) -> None:
        self._buffer.extend(data)

    def pop(self) -> dict | None:
        if len(self._buffer) < _HEADER.size:
            return None
        size = _HEADER.unpack_from(self._buffer)[0]
        if not 1 <= size <= MAX_FRAME_BYTES:
            raise ProtocolError("Frame length must be 1..65536 bytes.", "INVALID_FRAME")
        end = _HEADER.size + size
        if len(self._buffer) < end:
            return None
        payload = bytes(self._buffer[_HEADER.size:end])
        del self._buffer[:end]
        try:
            message = json.loads(
                payload.decode("utf-8"),
                object_pairs_hook=_object,
                parse_constant=_reject_constant,
            )
            _check_value(message)
        except (ValueError, UnicodeError, RecursionError) as error:
            raise ProtocolError("Frame must contain one valid UTF-8 JSON object.") from error
        if not isinstance(message, dict):
            raise ProtocolError("Frame JSON must be an object.")
        return message


def validate_request(message: dict) -> tuple[int, str, dict]:
    """Validate the common envelope; game rules validate action payloads."""
    if type(message.get("version")) is not int or message["version"] != VERSION:
        raise ProtocolError("Unsupported protocol version; expected integer 1.")
    if message.get("kind") != "request":
        raise ProtocolError("Client messages must have kind=request.")
    request_id = message.get("request_id")
    if type(request_id) is not int or not 1 <= request_id <= MAX_REQUEST_ID:
        raise ProtocolError("request_id must be an integer in 1..2147483647.")
    action = message.get("type")
    if not isinstance(action, str) or not _ACTION.fullmatch(action):
        raise ProtocolError("type must contain 1..32 lowercase letters or underscores.")
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise ProtocolError("payload must be an object.")
    return request_id, action, payload
