"""Transport regressions with controlled readiness, short writes and clock edges."""

import selectors
import socket
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from game import Event, Game
from protocol import FrameDecoder, encode_message
from question_bank import Question, Quiz
from server import (
    CLOSE_TIMEOUT,
    FRAME_TIMEOUT,
    LOGIN_TIMEOUT,
    MAX_OUTPUT_BYTES,
    REQUESTS_PER_TURN,
    WRITE_TIMEOUT,
    Connection,
    QuizServer,
    open_listener,
)


def request(request_id: int, action="quizzes", payload=None) -> bytes:
    return encode_message({
        "version": 1, "kind": "request", "type": action,
        "request_id": request_id, "payload": {} if payload is None else payload,
    })


def queued_messages(connection: Connection) -> list[dict]:
    decoder = FrameDecoder()
    decoder.feed(bytes(connection.output))
    messages = []
    while (message := decoder.pop()) is not None:
        messages.append(message)
    return messages


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.clock = Mock(return_value=100.0)
        clock_patch = patch("server.time.monotonic", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        questions = tuple(
            Question(f"q{i}", "Question?", {"A": "A", "B": "B", "C": "C", "D": "D"},
                     "A", 5, 10)
            for i in range(5)
        )
        self.game = Game({"quiz": Quiz("quiz", "Quiz", "", questions)})
        self.listener = Mock(spec=socket.socket)
        self.listener.getsockname.return_value = ("127.0.0.1", 5000)
        self.selector = Mock(spec=selectors.BaseSelector)
        with patch("server.selectors.DefaultSelector", return_value=self.selector):
            self.server = QuizServer(self.listener, self.game)
        self.addCleanup(self.server.close)

    def connect(self, session_id=1, authenticated=True) -> Connection:
        sock = Mock(spec=socket.socket)
        sock.recv.side_effect = BlockingIOError
        sock.send.side_effect = lambda data: len(data)
        connection = Connection(session_id, sock, self.clock())
        self.server.connections[session_id] = connection
        self.game.connect(session_id)
        if authenticated:
            self.game.handle(session_id, "login", {
                "user_id": f"user{session_id}", "name": f"Player {session_id}",
            }, self.clock())
        return connection

    def test_output_cap_defers_departure_until_current_broadcast_finishes(self):
        slow, healthy = self.connect(1), self.connect(2)
        room_id = self.game.handle(1, "create", {"quiz_id": "quiz"}, 100).payload["room_id"]
        snapshot = self.game.handle(2, "join", {"room_id": room_id}, 100).payload
        slow.output.extend(b"x" * MAX_OUTPUT_BYTES)

        self.server._emit([Event((1, 2), "room_updated", snapshot)])
        self.assertIn(1, self.server.connections)
        self.assertTrue(self.game.is_authenticated(1))
        self.assertEqual(len(slow.output), MAX_OUTPUT_BYTES)
        self.server._sweep_drops()

        self.assertNotIn(1, self.server.connections)
        self.assertIn(2, self.server.connections)
        self.assertFalse(self.game.is_authenticated(1))
        slow.sock.close.assert_called_once()
        messages = queued_messages(healthy)
        # A departure caused by backpressure must follow, not interrupt, the event.
        self.assertEqual([message["payload"]["host_id"] for message in messages],
                         ["user1", "user2"])
        self.assertEqual([message["payload"]["room_id"] for message in messages],
                         [room_id, room_id])

    def test_partial_send_preserves_the_exact_remaining_bytes(self):
        connection = self.connect()
        original = request(1) + request(2)
        connection.output.extend(original)
        connection.write_progress_at = 100.0
        connection.sock.send.side_effect = [7, len(original) - 7]
        self.clock.return_value = 101.0

        self.server._write(connection)
        self.assertEqual(bytes(connection.output), original[7:])
        self.assertEqual(connection.write_progress_at, 101.0)
        self.clock.return_value = 102.0
        self.server._write(connection)

        self.assertEqual(bytes(connection.output), b"")
        self.assertIsNone(connection.write_progress_at)
        self.assertEqual(bytes(connection.sock.send.call_args_list[0].args[0]), original)
        self.assertEqual(bytes(connection.sock.send.call_args_list[1].args[0]), original[7:])
        self.assertIn(1, self.server.connections)
        connection.sock.close.assert_not_called()

    def test_would_block_on_read_or_write_keeps_peer_and_queued_data(self):
        connection = self.connect()
        connection.output.extend(b"pending bytes")
        connection.write_progress_at = 100.0
        connection.sock.send.side_effect = BlockingIOError
        connection.sock.recv.side_effect = BlockingIOError
        self.clock.return_value = 101.0

        self.server._write(connection)
        self.server._read(connection)

        self.assertEqual(bytes(connection.output), b"pending bytes")
        self.assertEqual(connection.write_progress_at, 100.0)
        self.assertIn(1, self.server.connections)
        self.assertTrue(self.game.is_authenticated(1))
        connection.sock.close.assert_not_called()

    def test_login_at_timeout_cannot_revive_a_readable_or_buffered_peer(self):
        for session_id, delivery in ((1, "readable"), (2, "buffered")):
            with self.subTest(delivery=delivery):
                self.clock.return_value = 100.0
                connection = self.connect(session_id, authenticated=False)
                frame = request(1, "login", {"user_id": f"u{session_id}", "name": f"N{session_id}"})
                boundary = 100.0 + LOGIN_TIMEOUT
                self.assertFalse(self.server._expire_connection(connection, boundary - 0.001))
                self.clock.return_value = boundary
                if delivery == "readable":
                    connection.sock.recv.side_effect = None
                    connection.sock.recv.return_value = frame
                    self.server._read(connection)
                    connection.sock.recv.assert_not_called()
                else:
                    connection.decoder.feed(frame)
                    self.server._dispatch_buffer(connection)
                self.assertFalse(self.game.is_authenticated(session_id))
                self.assertEqual(connection.closing_at, boundary)
                self.assertEqual([message["payload"]["code"] for message in queued_messages(connection)],
                                 ["LOGIN_TIMEOUT"])

    def test_partial_frame_timer_is_not_renewed_by_trickled_bytes(self):
        connection = self.connect()
        frame = request(1)
        connection.sock.recv.side_effect = [frame[:5], frame[5:6], frame[6:]]
        self.clock.return_value = 110.0
        self.server._read(connection)
        self.assertEqual(connection.partial_since, 110.0)
        self.clock.return_value = 110.0 + FRAME_TIMEOUT - 0.001
        self.server._read(connection)
        self.assertEqual(connection.partial_since, 110.0)
        self.clock.return_value = 110.0 + FRAME_TIMEOUT

        self.server._read(connection)

        self.assertEqual(connection.sock.recv.call_count, 2)
        self.assertEqual(connection.closing_at, 110.0 + FRAME_TIMEOUT)
        self.assertEqual([message["payload"]["code"] for message in queued_messages(connection)],
                         ["FRAME_TIMEOUT"])
        self.assertEqual(connection.last_request_id, 0)

    def test_stalled_write_expires_at_boundary_without_processing_more_input(self):
        connection = self.connect()
        connection.output.extend(request(1))
        connection.write_progress_at = 100.0
        self.assertFalse(self.server._expire_connection(connection, 100.0 + WRITE_TIMEOUT - 0.001))
        self.clock.return_value = 100.0 + WRITE_TIMEOUT

        self.server._read(connection)
        self.server._sweep_drops()

        connection.sock.recv.assert_not_called()
        self.assertNotIn(1, self.server.connections)
        self.assertFalse(self.game.is_authenticated(1))
        connection.sock.close.assert_called_once()

    def test_writable_event_at_stall_deadline_cannot_renew_the_timeout(self):
        connection = self.connect()
        connection.output.extend(request(1))
        connection.write_progress_at = 100.0
        self.clock.return_value = 100.0 + WRITE_TIMEOUT

        self.server._write(connection)
        self.server._sweep_drops()

        connection.sock.send.assert_not_called()
        self.assertNotIn(1, self.server.connections)
        self.assertFalse(self.game.is_authenticated(1))
        connection.sock.close.assert_called_once()

    def test_graceful_close_flushes_only_before_its_deadline(self):
        for session_id, elapsed in ((1, CLOSE_TIMEOUT - 0.001), (2, CLOSE_TIMEOUT)):
            with self.subTest(elapsed=elapsed):
                self.clock.return_value = 100.0
                connection = self.connect(session_id)
                connection.output.extend(request(1))
                connection.write_progress_at = 100.0
                self.server._begin_close(connection)
                self.clock.return_value = 100.0 + elapsed

                self.server._write(connection)
                self.server._sweep_drops()

                if elapsed < CLOSE_TIMEOUT:
                    connection.sock.send.assert_called_once()
                    self.assertFalse(connection.output)
                else:
                    connection.sock.send.assert_not_called()
                self.assertNotIn(session_id, self.server.connections)
                connection.sock.close.assert_called_once()

    def test_failed_socket_setup_does_not_stop_accepting_other_peers(self):
        failed = Mock(spec=socket.socket)
        failed.setblocking.side_effect = OSError("Peer setup failed")
        healthy = Mock(spec=socket.socket)
        self.listener.accept.side_effect = [
            (failed, ("127.0.0.1", 10001)),
            (healthy, ("127.0.0.1", 10002)),
            BlockingIOError(),
        ]

        self.server._accept()

        failed.close.assert_called_once()
        healthy.close.assert_not_called()
        self.assertEqual(len(self.server.connections), 1)
        connection = next(iter(self.server.connections.values()))
        self.assertIs(connection.sock, healthy)
        self.assertEqual(self.game.handle(connection.session_id, "login", {
            "user_id": "healthy", "name": "Healthy",
        }, 100.0).payload["user_id"], "healthy")

    def test_busy_peer_yields_to_another_ready_peer_before_its_buffered_tail(self):
        busy, other = self.connect(1), self.connect(2)
        total = REQUESTS_PER_TURN + 8
        busy.sock.recv.side_effect = [b"".join(request(number) for number in range(1, total + 1))]
        other.sock.recv.side_effect = [request(1)]
        readiness = [
            (SimpleNamespace(data=busy), selectors.EVENT_READ),
            (SimpleNamespace(data=other), selectors.EVENT_READ),
        ]
        self.selector.select.side_effect = [readiness, KeyboardInterrupt()]

        with patch.object(self.game, "handle", wraps=self.game.handle) as handled:
            with patch("builtins.print"), self.assertRaises(KeyboardInterrupt):
                self.server.run()

        order = [call.args[0] for call in handled.call_args_list]
        self.assertEqual(order, [1] * REQUESTS_PER_TURN + [2] + [1] * 8)
        self.assertEqual([message["request_id"] for message in queued_messages(busy)],
                         list(range(1, total + 1)))
        self.assertEqual([message["request_id"] for message in queued_messages(other)], [1])
        self.assertEqual(busy.decoder.buffered_bytes, 0)
        self.assertFalse(self.server.connections)

    def test_listener_tries_next_address_when_first_socket_family_is_unavailable(self):
        candidate = Mock(spec=socket.socket)
        addresses = [
            (socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("::1", 5000, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", ("127.0.0.1", 5000)),
        ]
        with patch("server.socket.getaddrinfo", return_value=addresses):
            with patch("server.socket.socket", side_effect=[OSError("Family unavailable"), candidate]) as create:
                listener = open_listener("localhost", 5000)
        self.assertIs(listener, candidate)
        self.assertEqual(create.call_count, 2)
        candidate.bind.assert_called_once_with(("127.0.0.1", 5000))
        candidate.setblocking.assert_called_once_with(False)
        candidate.close.assert_not_called()


if __name__ == "__main__":
    unittest.main()
