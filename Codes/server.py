"""TCP transport and scheduling for the command-line quiz tournament."""

from __future__ import annotations

import argparse
import selectors
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field

from game import Event, Game, GameError
from protocol import MAX_REQUEST_ID, VERSION, FrameDecoder, ProtocolError
from protocol import encode_message, validate_request
from question_bank import BankError, load_bank

MAX_CONNECTIONS = 128
MAX_OUTPUT_BYTES = 1_048_576
IO_BYTES = 65_536
REQUESTS_PER_TURN = 32
LOGIN_TIMEOUT = 30.0
FRAME_TIMEOUT = 10.0
WRITE_TIMEOUT = 15.0
CLOSE_TIMEOUT = 2.0
POLL_INTERVAL = 0.25


@dataclass
class Connection:
    session_id: int
    sock: socket.socket
    connected_at: float
    decoder: FrameDecoder = field(default_factory=FrameDecoder)
    output: bytearray = field(default_factory=bytearray)
    last_request_id: int = 0
    partial_since: float | None = None
    write_progress_at: float | None = None
    closing_at: float | None = None


def port_number(value: str) -> int:
    try:
        port = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("port must be an integer") from error
    if not 1 <= port <= 65_535:
        raise argparse.ArgumentTypeError("port must be in 1..65535")
    return port


def open_listener(host: str, port: int) -> socket.socket:
    """Try the resolved addresses without assuming IPv4 or IPv6."""
    last_error: OSError | None = None
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    for family, kind, protocol, _, address in addresses:
        listener = None
        try:
            listener = socket.socket(family, kind, protocol)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(address)
            listener.listen(MAX_CONNECTIONS)
            listener.setblocking(False)
            return listener
        except OSError as error:
            last_error = error
            if listener is not None:
                listener.close()
    raise last_error or OSError("No usable address for the requested host.")


class QuizServer:
    """One loop owns connections, room transitions, deadlines and scores."""

    def __init__(self, listener: socket.socket, game: Game) -> None:
        self.listener = listener
        self.game = game
        self.selector = selectors.DefaultSelector()
        self.connections: dict[int, Connection] = {}
        self._next_session = 1
        self._next_event = 1
        self._pending: deque[int] = deque()
        self._pending_set: set[int] = set()
        self._drop_pending: set[int] = set()
        self.selector.register(listener, selectors.EVENT_READ, None)

    def run(self) -> None:
        try:
            address = self.listener.getsockname()
            print(f"SERVER_READY host={address[0]} port={address[1]}", flush=True)
            while True:
                self._emit(self.game.tick(time.monotonic()))
                self._expire_connections()
                self._sweep_drops()

                # Rotate already-buffered work before waiting for more bytes.
                for _ in range(len(self._pending)):
                    session_id = self._pending.popleft()
                    self._pending_set.discard(session_id)
                    connection = self.connections.get(session_id)
                    if connection is not None and connection.closing_at is None:
                        self._dispatch_buffer(connection)
                self._sweep_drops()

                timeout = self._wait_time()
                for key, events in self.selector.select(timeout):
                    if key.data is None:
                        self._accept()
                        continue
                    connection = key.data
                    if connection.session_id not in self.connections:
                        continue
                    if events & selectors.EVENT_READ:
                        self._read(connection)
                    if (events & selectors.EVENT_WRITE
                            and connection.session_id in self.connections):
                        self._write(connection)
                    self._sweep_drops()
        finally:
            self.close()

    def close(self) -> None:
        # Shutdown needs no departure broadcasts: every connection is closing.
        for connection in list(self.connections.values()):
            connection.sock.close()
        self.connections.clear()
        self.listener.close()
        self.selector.close()

    def _wait_time(self) -> float:
        if self._pending:
            return 0.0
        deadline = self.game.next_deadline()
        if deadline is None:
            return POLL_INTERVAL
        return max(0.0, min(POLL_INTERVAL, deadline - time.monotonic()))

    def _accept(self) -> None:
        for _ in range(16):
            try:
                sock, _ = self.listener.accept()
            except BlockingIOError:
                return
            except OSError as error:
                print(f"Accept failed: {error}", file=sys.stderr, flush=True)
                return
            if len(self.connections) >= MAX_CONNECTIONS:
                sock.close()
                continue
            session_id = self._next_session
            self._next_session += 1
            connection = Connection(session_id, sock, time.monotonic())
            try:
                sock.setblocking(False)
                self.selector.register(sock, selectors.EVENT_READ, connection)
            except (OSError, ValueError):
                sock.close()
                continue
            self.connections[session_id] = connection
            self.game.connect(session_id)

    def _read(self, connection: Connection) -> None:
        # Do not accumulate more bytes while complete frames await their turn.
        if (connection.closing_at is not None
                or connection.session_id in self._pending_set):
            return
        if self._expire_connection(connection, time.monotonic()):
            return
        try:
            data = connection.sock.recv(IO_BYTES)
        except BlockingIOError:
            return
        except OSError:
            self._drop(connection)
            return
        if not data:
            self._drop(connection)
            return
        connection.decoder.feed(data)
        self._dispatch_buffer(connection)

    def _dispatch_buffer(self, connection: Connection) -> None:
        count = 0
        try:
            while count < REQUESTS_PER_TURN:
                if self._expire_connection(connection, time.monotonic()):
                    return
                message = connection.decoder.pop()
                if message is None:
                    break
                count += 1
                connection.partial_since = None
                now = time.monotonic()
                self._emit(self.game.tick(now))
                self._request(connection, message, now)
                if (connection.closing_at is not None
                        or connection.session_id in self._drop_pending):
                    return
        except ProtocolError as error:
            self._error(connection, None, "protocol", error.code, error.message)
            self._begin_close(connection)
            return
        if count == REQUESTS_PER_TURN and connection.decoder.buffered_bytes:
            if connection.session_id not in self._pending_set:
                self._pending.append(connection.session_id)
                self._pending_set.add(connection.session_id)
        elif connection.decoder.buffered_bytes:
            if connection.partial_since is None:
                connection.partial_since = time.monotonic()

    def _request(self, connection: Connection, message: dict, now: float) -> None:
        candidate_id = message.get("request_id")
        request_id = candidate_id if (
            type(candidate_id) is int and 1 <= candidate_id <= MAX_REQUEST_ID
        ) else None
        action = message.get("type")
        if not isinstance(action, str) or not action.isascii() or len(action) > 32:
            action = "protocol"
        try:
            if request_id is not None:
                if request_id <= connection.last_request_id:
                    raise ProtocolError(
                        "request_id must increase on this connection.", "REPEATED_REQUEST"
                    )
                connection.last_request_id = request_id
            request_id, action, payload = validate_request(message)
            outcome = self.game.handle(connection.session_id, action, payload, now)
        except (ProtocolError, GameError) as error:
            self._error(connection, request_id, action, error.code, error.message)
            return
        self._queue(connection, {
            "version": VERSION, "kind": "response", "type": action,
            "request_id": request_id, "payload": outcome.payload,
        })
        self._emit(outcome.events)
        if outcome.close:
            self._begin_close(connection)

    def _error(self, connection: Connection, request_id: int | None,
               action: str, code: str, message: str) -> None:
        self._queue(connection, {
            "version": VERSION, "kind": "error", "type": action,
            "request_id": request_id, "payload": {"code": code, "message": message},
        })

    def _emit(self, events: list[Event]) -> None:
        for event in events:
            message = {
                "version": VERSION, "kind": "event", "type": event.type,
                "event_id": self._next_event, "payload": event.payload,
            }
            self._next_event += 1
            for session_id in event.recipients:
                connection = self.connections.get(session_id)
                if connection is not None:
                    self._queue(connection, message)

    def _queue(self, connection: Connection, message: dict) -> None:
        if connection.closing_at is not None:
            return
        data = encode_message(message)
        if len(connection.output) + len(data) > MAX_OUTPUT_BYTES:
            self._drop_pending.add(connection.session_id)
            return
        if not connection.output:
            connection.write_progress_at = time.monotonic()
        connection.output.extend(data)
        self._interest(connection)

    def _write(self, connection: Connection) -> None:
        # Readiness may arrive after a deadline elapsed inside select().
        # A closing peer may still flush its error until the close deadline.
        self._expire_connection(connection, time.monotonic())
        if connection.session_id in self._drop_pending:
            return
        if not connection.output:
            self._interest(connection)
            return
        try:
            sent = connection.sock.send(connection.output[:IO_BYTES])
        except BlockingIOError:
            return
        except OSError:
            self._drop(connection)
            return
        if sent == 0:
            self._drop(connection)
            return
        del connection.output[:sent]
        connection.write_progress_at = time.monotonic() if connection.output else None
        if connection.closing_at is not None and not connection.output:
            self._drop(connection)
        else:
            self._interest(connection)

    def _interest(self, connection: Connection) -> None:
        if connection.session_id not in self.connections:
            return
        events = selectors.EVENT_READ if connection.closing_at is None else 0
        if connection.output:
            events |= selectors.EVENT_WRITE
        if not events:
            self._drop_pending.add(connection.session_id)
            return
        try:
            self.selector.modify(connection.sock, events, connection)
        except (OSError, ValueError, KeyError):
            self._drop_pending.add(connection.session_id)

    def _begin_close(self, connection: Connection) -> None:
        if connection.closing_at is not None:
            return
        now = time.monotonic()
        connection.closing_at = now
        self._emit(self.game.disconnect(connection.session_id, now))
        self._interest(connection)

    def _drop(self, connection: Connection) -> None:
        if self.connections.pop(connection.session_id, None) is None:
            return
        try:
            self.selector.unregister(connection.sock)
        except (KeyError, ValueError, OSError):
            pass
        connection.sock.close()
        self._pending_set.discard(connection.session_id)
        self._emit(self.game.disconnect(connection.session_id, time.monotonic()))

    def _sweep_drops(self) -> None:
        while self._drop_pending:
            session_id = self._drop_pending.pop()
            connection = self.connections.get(session_id)
            if connection is not None:
                self._drop(connection)

    def _expire_connections(self) -> None:
        now = time.monotonic()
        for connection in list(self.connections.values()):
            self._expire_connection(connection, now)

    def _expire_connection(self, connection: Connection, now: float) -> bool:
        if connection.closing_at is not None:
            if now - connection.closing_at >= CLOSE_TIMEOUT:
                self._drop_pending.add(connection.session_id)
            return True
        if not self.game.is_authenticated(connection.session_id) and (
            now - connection.connected_at >= LOGIN_TIMEOUT
        ):
            self._error(connection, None, "protocol", "LOGIN_TIMEOUT", "Login timed out.")
            self._begin_close(connection)
            return True
        if connection.partial_since is not None and (
            now - connection.partial_since >= FRAME_TIMEOUT
        ):
            self._error(connection, None, "protocol", "FRAME_TIMEOUT", "Incomplete frame timed out.")
            self._begin_close(connection)
            return True
        if connection.write_progress_at is not None and (
            now - connection.write_progress_at >= WRITE_TIMEOUT
        ):
            self._drop_pending.add(connection.session_id)
            return True
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the networked quiz tournament server.")
    parser.add_argument("--host", required=True, help="local IPv4/IPv6 address or hostname")
    parser.add_argument("--port", required=True, type=port_number)
    parser.add_argument("--questions", required=True, help="UTF-8 JSON question-bank path")
    args = parser.parse_args(argv)
    listener: socket.socket | None = None
    try:
        bank = load_bank(args.questions)
        listener = open_listener(args.host, args.port)
        server = QuizServer(listener, Game(bank))
        server.run()
    except KeyboardInterrupt:
        print("Server stopped.", file=sys.stderr, flush=True)
        return 0
    except (BankError, OSError, ValueError) as error:
        print(f"Server error: {error}", file=sys.stderr, flush=True)
        return 1
    finally:
        if listener is not None:
            listener.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
