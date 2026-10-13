"""Stream framing and envelope cases independent of the game rules."""

import json
import struct
import unittest

from protocol import MAX_FRAME_BYTES, FrameDecoder, ProtocolError
from protocol import encode_message, validate_request


def request(**changes):
    message = {
        "version": 1, "kind": "request", "request_id": 1,
        "type": "login", "payload": {"user_id": "player1", "name": "玩家"},
    }
    message.update(changes)
    return message


def raw_frame(payload):
    return struct.pack("!I", len(payload)) + payload


class FramingTests(unittest.TestCase):
    def test_every_two_piece_split_including_header_and_multibyte_text(self):
        message = request()
        frame = encode_message(message)
        for split in range(1, len(frame)):
            with self.subTest(split=split):
                decoder = FrameDecoder()
                decoder.feed(frame[:split])
                self.assertIsNone(decoder.pop())
                decoder.feed(frame[split:])
                self.assertEqual(decoder.pop(), message)
                self.assertIsNone(decoder.pop())
                self.assertEqual(decoder.buffered_bytes, 0)

    def test_bytewise_delivery_and_many_coalesced_frames(self):
        messages = [request(request_id=value) for value in range(1, 41)]
        decoder = FrameDecoder()
        first = encode_message(messages[0])
        for index, value in enumerate(first):
            decoder.feed(bytes([value]))
            decoded = decoder.pop()
            if index < len(first) - 1:
                self.assertIsNone(decoded)
            else:
                self.assertEqual(decoded, messages[0])
        decoder.feed(b"".join(encode_message(item) for item in messages[1:]))
        self.assertEqual([decoder.pop() for _ in messages[1:]], messages[1:])
        self.assertIsNone(decoder.pop())

    def test_complete_frames_preserve_incomplete_tail(self):
        decoder = FrameDecoder()
        first, second = request(), request(request_id=2)
        tail = encode_message(second)
        decoder.feed(encode_message(first) + tail[:7])
        self.assertEqual(decoder.pop(), first)
        self.assertIsNone(decoder.pop())
        self.assertEqual(decoder.buffered_bytes, 7)
        decoder.feed(tail[7:])
        self.assertEqual(decoder.pop(), second)

    def test_length_counts_utf8_bytes(self):
        frame = encode_message(request())
        self.assertEqual(struct.unpack("!I", frame[:4])[0], len(frame[4:]))
        self.assertGreater(len(frame[4:]), len(frame[4:].decode("utf-8")))

    def test_invalid_lengths_fail_before_payload_arrives(self):
        for size in (0, MAX_FRAME_BYTES + 1, 0xFFFFFFFF):
            with self.subTest(size=size):
                decoder = FrameDecoder()
                decoder.feed(struct.pack("!I", size))
                with self.assertRaises(ProtocolError):
                    decoder.pop()

    def test_invalid_json_and_encoding(self):
        cases = [
            b"\xff", b"{", b"[]", b"null", b'{}{}',
            b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}',
            b'{"x":1e999}', b'{"x":"\\ud800"}',
            b'{"x":' + b'[' * 40 + b'0' + b']' * 40 + b'}',
        ]
        for payload in cases:
            with self.subTest(payload=payload[:40]):
                decoder = FrameDecoder()
                decoder.feed(raw_frame(payload))
                with self.assertRaises(ProtocolError):
                    decoder.pop()

    def test_encoder_rejects_oversized_or_non_json_messages(self):
        cases = [[], {"x": "x" * MAX_FRAME_BYTES}, {"x": float("inf")}, {"x": object()}]
        for message in cases:
            with self.subTest(kind=type(message).__name__):
                with self.assertRaises(ProtocolError):
                    encode_message(message)


class EnvelopeTests(unittest.TestCase):
    def test_request_and_extension_fields(self):
        message = request(extension={"reserved": True})
        self.assertEqual(validate_request(message), (1, "login", message["payload"]))

    def test_missing_and_wrong_type_fields(self):
        for name in ("version", "kind", "request_id", "type", "payload"):
            with self.subTest(missing=name):
                message = request()
                del message[name]
                with self.assertRaises(ProtocolError):
                    validate_request(message)
        cases = [
            {"version": True}, {"version": 2}, {"kind": "event"},
            {"request_id": True}, {"request_id": 0}, {"request_id": 2**31},
            {"request_id": "1"}, {"type": "LOGIN"}, {"type": "bad\n"},
            {"payload": []},
        ]
        for changes in cases:
            with self.subTest(changes=changes):
                with self.assertRaises(ProtocolError):
                    validate_request(request(**changes))


if __name__ == "__main__":
    unittest.main()
