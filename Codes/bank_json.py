"""Iterative JSON decoding for a size-bounded, server-side question bank."""

from dataclasses import dataclass
import json
import re
from typing import Callable


_WHITESPACE = re.compile(r"[ \t\r\n]*")
_NUMBER = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?")


@dataclass(slots=True)
class _Frame:
    container: dict | list
    state: str
    key: str | None = None


def loads(document: str, *, parse_int: Callable[[str], object]) -> object:
    """Decode all JSON syntax without using the Python call stack for containers.

    The caller bounds file size and supplies integer conversion. Strings use the
    standard decoder; it never receives an object or array at its starting index.
    Duplicate object keys are rejected, including equivalent escaped spellings.
    """
    decoder = json.JSONDecoder()
    stack: list[_Frame] = []
    position = 0
    root: object = None
    have_root = False

    def fail(message: str) -> None:
        raise json.JSONDecodeError(message, document, position)

    def read_value() -> tuple[object, _Frame | None]:
        nonlocal position
        token = document[position:position + 1]
        if token == "{":
            position += 1
            value = {}
            return value, _Frame(value, "first_key")
        if token == "[":
            position += 1
            value = []
            return value, _Frame(value, "first_value")
        if token == '"':
            value, position = decoder.raw_decode(document, position)
            return value, None
        for literal, value in (("true", True), ("false", False), ("null", None)):
            if document.startswith(literal, position):
                position += len(literal)
                return value, None
        number = _NUMBER.match(document, position)
        if number is not None:
            token = number.group()
            position = number.end()
            value = float(token) if any(char in token for char in ".eE") else parse_int(token)
            return value, None
        fail("Expected a JSON value")

    while True:
        position = _WHITESPACE.match(document, position).end()
        token = document[position:position + 1]
        if stack:
            frame = stack[-1]
            if frame.state in ("first_key", "key"):
                if token == "}" and frame.state == "first_key":
                    position += 1
                    stack.pop()
                    continue
                if token != '"':
                    fail("Expected an object member name")
                key, position = decoder.raw_decode(document, position)
                if key in frame.container:
                    fail(f"Duplicate JSON member: {key!r}")
                frame.key = key
                frame.state = "colon"
                continue
            if frame.state == "colon":
                if token != ":":
                    fail("Expected ':' after an object member name")
                position += 1
                frame.state = "value"
                continue
            if frame.state == "after_value":
                closing = "}" if isinstance(frame.container, dict) else "]"
                if token == closing:
                    position += 1
                    stack.pop()
                elif token == ",":
                    position += 1
                    frame.state = "key" if isinstance(frame.container, dict) else "value"
                else:
                    fail(f"Expected ',' or '{closing}'")
                continue
            if frame.state == "first_value" and token == "]":
                position += 1
                stack.pop()
                continue
        elif have_root:
            if position != len(document):
                fail("Extra data after the JSON value")
            return root

        value, child = read_value()
        if stack:
            parent = stack[-1]
            if isinstance(parent.container, dict):
                parent.container[parent.key] = value
            else:
                parent.container.append(value)
            parent.state = "after_value"
        else:
            root = value
            have_root = True
        if child is not None:
            # Attach first; closing a child only pops a frame, never recurses.
            stack.append(child)
