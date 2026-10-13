"""Client command, presentation, and protocol boundary checks without networking."""

import argparse
import errno
import io
import queue
import socket
import threading
import unittest
from unittest.mock import Mock, call, patch

from client import (
    ClientError,
    InputLine,
    MAX_INPUT_LENGTH,
    PendingRequest,
    QuestionContext,
    QuizClient,
    RawLineReader,
    connect_socket,
    parse_command,
    port_number,
    read_input,
    terminal_text,
    validate_server_message,
)
from protocol import FrameDecoder, encode_message


def message(kind, action, payload, identifier=1):
    key = "event_id" if kind == "event" else "request_id"
    return {
        "version": 1, "kind": kind, "type": action,
        key: identifier, "payload": payload,
    }


def question_payload():
    return {
        "room_id": "r1", "question_id": "q1", "number": 1,
        "total_questions": 5, "text": "Which option?",
        "options": {"A": "First", "B": "Second", "C": "Third", "D": "Fourth"},
        "time_limit": 5, "points": 100,
    }


class ConnectionTests(unittest.TestCase):
    def test_unsupported_address_family_falls_back_to_next_address(self):
        addresses = [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 5000, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 5000)),
        ]
        connection = Mock()
        connection.connect_ex.return_value = errno.EINPROGRESS
        connection.getsockopt.return_value = 0

        def immediate_thread(*, target, **kwargs):
            thread = Mock()
            thread.start.side_effect = target
            return thread

        with (
            patch("client.socket.getaddrinfo", return_value=addresses) as resolve,
            patch("client.threading.Thread", side_effect=immediate_thread),
            patch("client.time.monotonic", return_value=100.0),
            patch("client.socket.socket", side_effect=[
                OSError(errno.EAFNOSUPPORT, "Address family unavailable"), connection,
            ]) as create_socket,
            patch("client.select.select", return_value=([], [connection], [])) as select_ready,
        ):
            self.assertIs(connect_socket("localhost", 5000), connection)

        resolve.assert_called_once_with("localhost", 5000, type=socket.SOCK_STREAM)
        self.assertEqual(create_socket.call_args_list, [
            call(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP),
            call(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP),
        ])
        connection.setblocking.assert_called_once_with(False)
        connection.connect_ex.assert_called_once_with(("127.0.0.1", 5000))
        select_ready.assert_called_once_with([], [connection], [connection], 5.0)
        connection.getsockopt.assert_called_once_with(socket.SOL_SOCKET, socket.SO_ERROR)
        connection.close.assert_not_called()


class CommandTests(unittest.TestCase):
    def test_commands_map_to_protocol_actions(self):
        cases = {
            "/quizzes": ("quizzes", {}),
            "/rooms": ("rooms", {}),
            "/create basics": ("create", {"quiz_id": "basics"}),
            "/join r1": ("join", {"room_id": "r1"}),
            "/players": ("players", {}),
            "/ready": ("ready", {"ready": True}),
            "/unready": ("ready", {"ready": False}),
            "/start": ("start", {}),
            "/score": ("score", {}),
            "/leave": ("leave", {}),
            "/quit": ("quit", {}),
            "/help": ("help", {}),
            "   ": ("", {}),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(parse_command(text, None), expected)

    def test_implicit_answer_uses_captured_question(self):
        context = QuestionContext()
        context.set("old_question")
        line = InputLine("/answer b", context.capture())
        context.set("new_question")
        self.assertEqual(
            parse_command(line.text, line.question_id),
            ("answer", {"question_id": "old_question", "choice": "B"}),
        )

    def test_explicit_answer_can_target_a_closed_question(self):
        self.assertEqual(
            parse_command("/answer A previous", None),
            ("answer", {"question_id": "previous", "choice": "A"}),
        )

    def test_answer_without_a_captured_question_is_rejected(self):
        with self.assertRaises(ClientError) as caught:
            parse_command("/answer A", None)
        self.assertEqual(caught.exception.code, "NO_QUESTION")

    def test_invalid_commands_cannot_add_identity_fields(self):
        for text in (
            "/login other Other", "/join", "/create a b", "/quizzes extra",
            "/answer AB", "/answer Z", "/answer A q1 user", "/join r!",
            "/answer A " + "q" * 33,
        ):
            with self.subTest(text=text), self.assertRaises(ClientError):
                parse_command(text, "q1")

    def test_port_is_ascii_integer_in_range(self):
        self.assertEqual(port_number("65535"), 65535)
        for value in ("0", "65536", "-1", "+1", "1.0", "１２３", "9" * 5000):
            with self.subTest(value=value[:20]), self.assertRaises(argparse.ArgumentTypeError):
                port_number(value)


class InputTests(unittest.TestCase):
    def read_lines(self, content, context=None):
        destination = queue.Queue(maxsize=16)
        read_input(io.StringIO(content), destination, context or QuestionContext(), threading.Event())
        items = []
        while not destination.empty():
            items.append(destination.get_nowait())
        return items

    def test_eof_preserves_last_line_without_newline(self):
        items = self.read_lines("/rooms")
        self.assertEqual([item.text for item in items], ["/rooms", None])

    def test_overlong_line_is_drained_without_losing_next_command(self):
        items = self.read_lines("x" * (MAX_INPUT_LENGTH + 200) + "\n/rooms\n")
        self.assertIsNotNone(items[0].error)
        self.assertEqual(items[1].text, "/rooms")
        self.assertIsNone(items[2].text)

    def test_line_at_size_limit_is_accepted(self):
        items = self.read_lines("x" * MAX_INPUT_LENGTH + "\n")
        self.assertIsNone(items[0].error)
        self.assertEqual(len(items[0].text), MAX_INPUT_LENGTH)

    def test_reader_stores_question_with_input(self):
        context = QuestionContext()
        context.set("q1")
        items = self.read_lines("/answer A\n", context)
        context.set("q2")
        self.assertEqual(items[0].question_id, "q1")

    def test_control_and_direction_characters_are_visible_escapes(self):
        displayed = terminal_text("Quiz\x1b[31m\n\r\t\u202e题目")
        self.assertEqual(displayed, "Quiz\\u001b[31m\\u000a\\u000d\\u0009\\u202e题目")
        self.assertTrue(displayed.isprintable())

    def test_raw_reader_handles_split_unicode_and_bounds_long_lines(self):
        raw = Mock()
        raw.read.side_effect = [b"\xe9", b"\xa2\x98\n/rooms\n", b""]
        reader = RawLineReader(raw, "utf-8")
        self.assertEqual(reader.readline(100), "题\n")
        self.assertEqual(reader.readline(100), "/rooms\n")
        self.assertEqual(reader.readline(100), "")
        reader = RawLineReader(io.BytesIO(b"abcdef\n"), "utf-8")
        self.assertEqual(reader.readline(3), "abc")
        self.assertEqual(reader.readline(3), "def")
        self.assertEqual(reader.readline(3), "\n")


class ServerMessageTests(unittest.TestCase):
    def test_protocol_error_can_have_no_request_id(self):
        incoming = message("error", "protocol", {"code": "BAD_FRAME", "message": "Invalid frame"}, None)
        self.assertEqual(validate_server_message(incoming)[:3], ("error", None, "protocol"))

    def test_invalid_envelope_types_raise_controlled_errors(self):
        original = message("response", "login", {"user_id": "alice", "name": "Alice"})
        for key, value in (
            ("version", True), ("kind", []), ("kind", "request"),
            ("request_id", True), ("request_id", 0), ("payload", []), ("type", None),
        ):
            incoming = dict(original, **{key: value})
            with self.subTest(key=key, value=value), self.assertRaises(ClientError):
                validate_server_message(incoming)

    def test_unhashable_room_state_is_a_protocol_error(self):
        room = {
            "room_id": "r1", "quiz_id": "quiz", "title": "Quiz", "host_id": "alice",
            "state": [], "players": [],
        }
        with self.assertRaises(ClientError):
            validate_server_message(message("event", "room_updated", room))

    def test_question_options_must_have_exactly_four_labels(self):
        payload = question_payload()
        payload["options"]["E"] = "Fifth"
        with self.assertRaises(ClientError):
            validate_server_message(message("event", "question_opened", payload))

    def test_boolean_is_not_a_question_number(self):
        payload = question_payload()
        payload["number"] = True
        with self.assertRaises(ClientError):
            validate_server_message(message("event", "question_opened", payload))


class ClientStateTests(unittest.TestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.connection = Mock()
        self.client = QuizClient(
            self.connection, "alice", "Alice", input_stream=io.StringIO(), output_stream=self.output,
        )

    def test_login_must_match_outstanding_request_and_identity(self):
        self.client.pending[1] = PendingRequest("login", 10.0)
        self.client.handle_message(message("response", "login", {"user_id": "alice", "name": "Alice"}))
        self.assertTrue(self.client.authenticated)
        self.assertIn("LOGIN_OK user_id=alice name=Alice", self.output.getvalue())
        self.assertFalse(self.client.pending)
        with self.assertRaises(ClientError):
            self.client.handle_message(message("response", "login", {"user_id": "alice", "name": "Alice"}))

    def test_changed_identity_is_rejected(self):
        self.client.pending[1] = PendingRequest("login", 10.0)
        with self.assertRaises(ClientError):
            self.client.handle_message(message("response", "login", {"user_id": "other", "name": "Other"}))
        self.assertFalse(self.client.authenticated)

    def test_replayed_events_are_rejected(self):
        incoming = message("event", "question_opened", question_payload(), 3)
        self.client.handle_message(incoming)
        with self.assertRaises(ClientError):
            self.client.handle_message(incoming)

    def test_bad_event_does_not_mutate_state_or_print_partial_question(self):
        payload = question_payload()
        payload["options"]["D"] = None
        with self.assertRaises(ClientError):
            self.client.handle_message(message("event", "question_opened", payload))
        self.assertIsNone(self.client.context.capture())
        self.assertEqual(self.client.last_event_id, 0)
        self.assertEqual(self.output.getvalue(), "")

    def test_question_control_sequences_are_escaped_and_close_clears_context(self):
        payload = question_payload()
        payload["options"]["A"] = "\x1b[2J"
        self.client.handle_message(message("event", "question_opened", payload))
        self.assertEqual(self.client.context.capture(), "q1")
        self.assertNotIn("\x1b", self.output.getvalue())
        self.assertIn("\\u001b[2J", self.output.getvalue())
        self.client.handle_message(message("event", "question_closed", {
            "room_id": "r1", "question_id": "q1", "correct_answer": "A",
            "your_answer": None, "points_awarded": 0, "leaderboard": [],
        }, 2))
        self.assertIsNone(self.client.context.capture())
        self.assertIn("RESULT question_id=q1 correct=A your_answer=- points_awarded=0", self.output.getvalue())

    def test_question_context_is_set_before_question_marker_is_printed(self):
        observed = []
        original_emit = self.client.emit

        def capture(text):
            if text.startswith("QUESTION "):
                observed.append(self.client.context.capture())
            original_emit(text)

        self.client.emit = capture
        self.client.handle_message(message("event", "question_opened", question_payload()))
        self.assertEqual(observed, ["q1"])

    def test_complete_frame_backlog_is_drained_before_more_reads(self):
        frames = b"".join(
            encode_message(message("event", "question_opened", question_payload(), identifier))
            for identifier in range(1, 70)
        )
        self.client.decoder.feed(frames)
        self.client.drain_messages(1.0)
        self.assertTrue(self.client.decode_pending)
        self.assertIsNone(self.client.partial_since)
        self.assertEqual(self.client.last_event_id, 32)
        self.client.drain_messages(2.0)
        self.client.drain_messages(3.0)
        self.assertFalse(self.client.decode_pending)
        self.assertEqual(self.client.last_event_id, 69)
        self.assertEqual(self.client.decoder.buffered_bytes, 0)

    def test_partial_frame_timer_does_not_reset_on_each_poll(self):
        self.client.decoder.feed(b"\x00\x00")
        self.client.drain_messages(1.0)
        self.client.drain_messages(2.0)
        self.assertEqual(self.client.partial_since, 1.0)
        self.assertFalse(self.client.decode_pending)

    def test_buffered_reply_cannot_clear_an_expired_request(self):
        self.client.pending[1] = PendingRequest("login", 110.0)
        frame = encode_message(message("response", "login", {
            "user_id": "alice", "name": "Alice",
        }))
        self.client.decoder.feed(frame)
        with patch("client.time.monotonic", return_value=110.0):
            with self.assertRaises(ClientError) as caught:
                self.client.step()
        self.assertEqual(caught.exception.code, "REQUEST_TIMEOUT")
        self.assertFalse(self.client.authenticated)
        self.assertIn(1, self.client.pending)
        self.assertEqual(self.client.decoder.buffered_bytes, len(frame))
        self.assertEqual(self.output.getvalue(), "")

    def test_partial_frame_expiring_during_select_is_not_completed(self):
        self.client.decoder.feed(b"\x00\x00")
        self.client.partial_since = 100.0
        with (
            patch("client.time.monotonic", side_effect=[109.99, 110.0]),
            patch("client.select.select", return_value=([self.connection], [], [])),
        ):
            with self.assertRaises(ClientError) as caught:
                self.client.step()
        self.assertEqual(caught.exception.code, "PROTOCOL_ERROR")
        self.connection.recv.assert_not_called()
        self.assertEqual(self.client.decoder.buffered_bytes, 2)

    def test_stalled_write_expiring_during_select_is_not_resumed(self):
        self.client.outgoing.extend(b"unfinished frame")
        self.client.last_write = 100.0
        with (
            patch("client.time.monotonic", side_effect=[109.99, 110.0]),
            patch("client.select.select", return_value=([], [self.connection], [])),
        ):
            with self.assertRaises(ClientError) as caught:
                self.client.step()
        self.assertEqual(caught.exception.code, "WRITE_TIMEOUT")
        self.connection.send.assert_not_called()

    def test_duplicate_room_event_is_quiet_but_players_command_reprints(self):
        room = {
            "room_id": "r1", "quiz_id": "quiz", "title": "Quiz", "host_id": "alice",
            "state": "waiting", "players": [{"user_id": "alice", "name": "Alice", "ready": False}],
        }
        self.client.pending[1] = PendingRequest("create", 10.0)
        self.client.handle_message(message("response", "create", room))
        before_event = self.output.getvalue()
        self.client.handle_message(message("event", "room_updated", room))
        self.assertEqual(self.output.getvalue(), before_event)
        self.client.pending[2] = PendingRequest("players", 10.0)
        self.client.handle_message(message("response", "players", room, 2))
        self.assertEqual(self.output.getvalue().count("ROOM room_id=r1"), 2)

    def test_queued_answer_does_not_rebind_to_next_question(self):
        self.client.authenticated = True
        self.client.context.set("new")
        self.client.inputs.put_nowait(InputLine("/answer B", "old"))
        self.client.process_inputs(1.0)
        decoder = FrameDecoder()
        decoder.feed(bytes(self.client.outgoing))
        self.assertEqual(decoder.pop()["payload"], {"question_id": "old", "choice": "B"})
        self.connection.assert_not_called()

    def test_request_ids_increase_and_eof_queues_graceful_quit(self):
        self.client.authenticated = True
        self.client.inputs.put_nowait(InputLine("/rooms", None))
        self.client.inputs.put_nowait(InputLine(None, None))
        self.client.process_inputs(1.0)
        decoder = FrameDecoder()
        decoder.feed(bytes(self.client.outgoing))
        first, second = decoder.pop(), decoder.pop()
        self.assertEqual((first["request_id"], first["type"]), (1, "rooms"))
        self.assertEqual((second["request_id"], second["type"]), (2, "quit"))
        self.assertTrue(self.client.closing)
        self.assertTrue(self.client.stop_input.is_set())


if __name__ == "__main__":
    unittest.main()
