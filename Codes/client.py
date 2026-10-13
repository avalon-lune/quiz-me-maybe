"""Interactive TCP quiz client; the question bank stays on the server."""

from __future__ import annotations

import argparse
import codecs
from dataclasses import dataclass
import errno
import queue
import re
import select
import socket
import sys
import threading
import time
from typing import TextIO

from protocol import MAX_FRAME_BYTES, FrameDecoder, ProtocolError, encode_message


CONNECT_TIMEOUT = 5.0
REQUEST_TIMEOUT = 10.0
QUIT_TIMEOUT = 3.0
WRITE_TIMEOUT = 10.0
FRAME_TIMEOUT = 10.0
MAX_INPUT_LENGTH = 4096
MAX_PENDING_REQUESTS = 64
MAX_OUTPUT_BYTES = 1_048_576
RECEIVE_CHUNK_BYTES = 16384
MAX_RECEIVE_BYTES = MAX_FRAME_BYTES + 4 + RECEIVE_CHUNK_BYTES
MAX_REQUEST_ID = 2_147_483_647

HELP_LINES = (
    "/quizzes                  List available quizzes",
    "/rooms                    List rooms accepting players",
    "/create QUIZ_ID           Create and join a room",
    "/join ROOM_ID             Join a waiting room",
    "/players                  Show your room and its players",
    "/ready | /unready         Change your readiness",
    "/start                    Start your room (host only)",
    "/answer A [QUESTION_ID]   Answer A, B, C or D",
    "/score                    Show the current leaderboard",
    "/leave                    Leave your room",
    "/quit                     Disconnect and exit",
    "/help                     Show these commands",
)


class ClientError(ValueError):
    """An actionable local input, transport, or server-protocol failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def terminal_text(value: str) -> str:
    """Display received text without executing control or terminal sequences."""
    return "".join(
        character if character.isprintable() else f"\\u{ord(character):04x}"
        for character in value
    )


def parse_command(text: str, question_id: str | None) -> tuple[str, dict]:
    """Bind an implicit answer to the question captured with this input line."""
    parts = text.strip().split()
    if not parts:
        return "", {}
    command, *arguments = parts
    no_arguments = {
        "/help", "/quizzes", "/rooms", "/players", "/start",
        "/score", "/leave", "/quit", "/ready", "/unready",
    }
    if command in no_arguments:
        if arguments:
            raise ClientError("INVALID_COMMAND", f"Usage: {command}")
        if command in {"/ready", "/unready"}:
            return "ready", {"ready": command == "/ready"}
        return command[1:], {}
    if command in {"/create", "/join"}:
        key = "quiz_id" if command == "/create" else "room_id"
        if len(arguments) != 1 or not valid_token(arguments[0]):
            raise ClientError("INVALID_COMMAND", f"Usage: {command} {key.upper()}")
        return command[1:], {key: arguments[0]}
    if command == "/answer":
        if len(arguments) not in {1, 2} or arguments[0].upper() not in "ABCD":
            raise ClientError("INVALID_COMMAND", "Usage: /answer A [QUESTION_ID]")
        if len(arguments[0]) != 1:
            raise ClientError("INVALID_COMMAND", "Choose one of A, B, C or D.")
        target = arguments[1] if len(arguments) == 2 else question_id
        if target is None:
            raise ClientError("NO_QUESTION", "No question is open; wait for QUESTION.")
        if not valid_token(target):
            raise ClientError("INVALID_COMMAND", "Invalid question ID.")
        return "answer", {"question_id": target, "choice": arguments[0].upper()}
    raise ClientError("INVALID_COMMAND", "Unknown command. Type /help for commands.")


def valid_token(value: str) -> bool:
    return 1 <= len(value) <= 32 and all(
        character.isalnum() or character in "_-" for character in value
    )


class QuestionContext:
    """Share only the displayed question identifier with the input reader."""

    def __init__(self) -> None:
        self._question_id: str | None = None
        self._lock = threading.Lock()

    def capture(self) -> str | None:
        with self._lock:
            return self._question_id

    def set(self, question_id: str | None) -> None:
        with self._lock:
            self._question_id = question_id


@dataclass(frozen=True)
class InputLine:
    text: str | None
    question_id: str | None
    error: str | None = None


class RawLineReader:
    """Read console or pipe bytes without holding stdin's buffered-reader lock."""

    def __init__(self, raw, encoding: str) -> None:
        self.raw = raw
        self.decoder = codecs.getincrementaldecoder(encoding)(errors="replace")
        self.pending = ""
        self.eof = False

    def readline(self, limit: int) -> str:
        while True:
            newline = self.pending.find("\n", 0, limit)
            if newline >= 0:
                end = newline + 1
                break
            if len(self.pending) >= limit or self.eof:
                end = min(limit, len(self.pending))
                break
            data = self.raw.read(4096)
            self.eof = not data
            self.pending += self.decoder.decode(data or b"", final=self.eof)
        line, self.pending = self.pending[:end], self.pending[end:]
        return line


def read_input(
    stream: TextIO | RawLineReader,
    destination: queue.Queue[InputLine],
    context: QuestionContext,
    stop: threading.Event,
) -> None:
    """A daemon may wait on stdin; the network loop never waits on this thread."""
    while not stop.is_set():
        try:
            line = stream.readline(MAX_INPUT_LENGTH + 2)
            captured = context.capture()
            text = line.rstrip("\r\n")
            if len(text) > MAX_INPUT_LENGTH:
                while line and not line.endswith("\n") and not stop.is_set():
                    line = stream.readline(MAX_INPUT_LENGTH + 2)
                item = InputLine("", captured, "Input exceeds 4096 characters.")
            else:
                item = InputLine(text if line else None, captured)
        except (OSError, UnicodeError, ValueError) as error:
            item = InputLine(None, None, f"Cannot read input: {error}")
        while not stop.is_set():
            try:
                destination.put(item, timeout=0.1)
                break
            except queue.Full:
                continue
        if item.text is None:
            return


def _text(payload: dict, key: str, maximum: int = 500, *, empty: bool = False) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not (0 if empty else 1) <= len(value) <= maximum:
        raise ClientError("PROTOCOL_ERROR", f"Invalid server field: {key}.")
    return value


def _integer(payload: dict, key: str, minimum: int = 0, maximum: int = 2**63 - 1) -> int:
    value = payload.get(key)
    if type(value) is not int or not minimum <= value <= maximum:
        raise ClientError("PROTOCOL_ERROR", f"Invalid server field: {key}.")
    return value


def _boolean(payload: dict, key: str) -> bool:
    value = payload.get(key)
    if type(value) is not bool:
        raise ClientError("PROTOCOL_ERROR", f"Invalid server field: {key}.")
    return value


def _items(payload: dict, key: str, maximum: int) -> list[dict]:
    value = payload.get(key)
    if not isinstance(value, list) or len(value) > maximum or any(
        not isinstance(item, dict) for item in value
    ):
        raise ClientError("PROTOCOL_ERROR", f"Invalid server field: {key}.")
    return value


def _room(payload: dict, *, listing: bool = False) -> None:
    for key in ("room_id", "quiz_id", "host_id"):
        _text(payload, key, 32)
    _text(payload, "title", 100)
    if payload.get("state") not in ("waiting", "running", "finished"):
        raise ClientError("PROTOCOL_ERROR", "Invalid room state.")
    if listing:
        _integer(payload, "player_count", 0, 32)
    else:
        for player in _items(payload, "players", 32):
            _text(player, "user_id", 32)
            _text(player, "name", 64)
            _boolean(player, "ready")


def _leaderboard(payload: dict, key: str = "leaderboard") -> None:
    for row in _items(payload, key, 32):
        _integer(row, "rank", 1, 32)
        _text(row, "user_id", 32)
        _text(row, "name", 64)
        _integer(row, "score")
        _boolean(row, "active")


def validate_server_message(message: dict) -> tuple[str, int | None, str, dict]:
    """Validate envelopes before matching responses or changing client state."""
    if type(message.get("version")) is not int or message["version"] != 1:
        raise ClientError("PROTOCOL_ERROR", "Unsupported server protocol version.")
    kind = message.get("kind")
    if kind not in ("response", "error", "event"):
        raise ClientError("PROTOCOL_ERROR", "Invalid server message kind.")
    action = _text(message, "type", 64)
    payload = message.get("payload")
    if not isinstance(payload, dict):
        raise ClientError("PROTOCOL_ERROR", "Server payload must be an object.")
    key = "event_id" if kind == "event" else "request_id"
    identifier = message.get(key)
    if kind == "error" and action == "protocol" and identifier is None:
        _text(payload, "code", 64)
        _text(payload, "message", 1000)
        return kind, None, action, payload
    _integer(message, key, 1, 2**63 - 1 if kind == "event" else MAX_REQUEST_ID)
    validate_payload(kind, action, payload)
    return kind, identifier, action, payload


def validate_payload(kind: str, action: str, payload: dict) -> None:
    if kind == "error":
        _text(payload, "code", 64)
        _text(payload, "message", 1000)
        return
    if kind == "response":
        if action == "login":
            _text(payload, "user_id", 32)
            _text(payload, "name", 64)
        elif action == "quizzes":
            for quiz in _items(payload, "quizzes", 20):
                _text(quiz, "quiz_id", 32)
                _text(quiz, "title", 100)
                _text(quiz, "description", 300, empty=True)
                _integer(quiz, "question_count", 5, 50)
        elif action == "rooms":
            for room in _items(payload, "rooms", 64):
                _room(room, listing=True)
        elif action in {"create", "join", "players", "ready", "start"}:
            _room(payload)
        elif action == "answer":
            if _boolean(payload, "accepted") is not True:
                raise ClientError("PROTOCOL_ERROR", "Unaccepted answer needs an error reply.")
            _text(payload, "question_id", 32)
        elif action == "score":
            _text(payload, "room_id", 32)
            _leaderboard(payload)
        elif action == "leave":
            _text(payload, "room_id", 32)
        elif action != "quit":
            raise ClientError("PROTOCOL_ERROR", "Unknown server response type.")
        return
    if action == "room_updated":
        _room(payload)
        return
    _text(payload, "room_id", 32)
    if action == "quiz_started":
        _text(payload, "quiz_id", 32)
        _text(payload, "title", 100)
        _integer(payload, "total_questions", 5, 50)
    elif action == "question_opened":
        _text(payload, "question_id", 32)
        _integer(payload, "total_questions", 5, 50)
        _integer(payload, "number", 1, payload["total_questions"])
        _text(payload, "text", 500)
        options = payload.get("options")
        if not isinstance(options, dict) or set(options) != set("ABCD"):
            raise ClientError("PROTOCOL_ERROR", "Invalid question options.")
        for label in "ABCD":
            _text(options, label, 200)
        _integer(payload, "time_limit", 5, 60)
        _integer(payload, "points", 1, 1000)
    elif action == "question_closed":
        _text(payload, "question_id", 32)
        if payload.get("correct_answer") not in ("A", "B", "C", "D"):
            raise ClientError("PROTOCOL_ERROR", "Invalid correct answer.")
        if "your_answer" not in payload or payload["your_answer"] not in (None, "A", "B", "C", "D"):
            raise ClientError("PROTOCOL_ERROR", "Invalid submitted answer.")
        _integer(payload, "points_awarded", 0, 1000)
        _leaderboard(payload)
    elif action == "quiz_finished":
        _text(payload, "quiz_id", 32)
        _leaderboard(payload, "final_leaderboard")
        winners = payload.get("winners")
        if not isinstance(winners, list) or len(winners) > 32 or any(
            not isinstance(winner, str) or not valid_token(winner) for winner in winners
        ):
            raise ClientError("PROTOCOL_ERROR", "Invalid winners list.")
    else:
        raise ClientError("PROTOCOL_ERROR", "Unknown server event type.")


def connect_socket(host: str, port: int) -> socket.socket:
    """Bound name resolution and connection attempts with one total deadline."""
    deadline = time.monotonic() + CONNECT_TIMEOUT
    resolved: queue.Queue = queue.Queue(maxsize=1)

    def resolve() -> None:
        try:
            result = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError, ValueError) as error:
            result = error
        resolved.put_nowait(result)

    threading.Thread(target=resolve, daemon=True, name="dns-reader").start()
    try:
        addresses = resolved.get(timeout=CONNECT_TIMEOUT)
    except queue.Empty as error:
        raise ClientError("CONNECTION_FAILED", "Server name resolution timed out.") from error
    if isinstance(addresses, Exception):
        raise ClientError("CONNECTION_FAILED", str(addresses)) from addresses
    in_progress = {
        errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EALREADY,
        10035, 10036, 10037,  # Windows nonblocking connect results.
    }
    last_error = "No server address is available."
    for family, socktype, protocol, _, address in addresses:
        if time.monotonic() >= deadline:
            break
        connection = None
        try:
            connection = socket.socket(family, socktype, protocol)
            connection.setblocking(False)
            result = connection.connect_ex(address)
            if result and result not in in_progress:
                raise OSError(result, "Connection failed")
            if result:
                _, writable, exceptional = select.select(
                    [], [connection], [connection], max(0.0, deadline - time.monotonic())
                )
                if not writable and not exceptional:
                    raise TimeoutError("Connection timed out")
                result = connection.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR)
                if result:
                    raise OSError(result, "Connection failed")
            return connection
        except (OSError, ValueError) as error:
            last_error = str(error)
            if connection is not None:
                connection.close()
    raise ClientError("CONNECTION_FAILED", last_error)


@dataclass(frozen=True)
class PendingRequest:
    action: str
    deadline: float


class QuizClient:
    def __init__(
        self, connection: socket.socket, user_id: str, name: str,
        *, input_stream: TextIO = sys.stdin, output_stream: TextIO = sys.stdout,
    ) -> None:
        self.connection = connection
        self.user_id = user_id
        self.name = name
        self.input_stream = input_stream
        self.output_stream = output_stream
        self.decoder = FrameDecoder()
        self.outgoing = bytearray()
        self.pending: dict[int, PendingRequest] = {}
        self.next_request_id = 1
        self.last_event_id = 0
        self.context = QuestionContext()
        self.inputs: queue.Queue[InputLine] = queue.Queue(maxsize=64)
        self.stop_input = threading.Event()
        self.authenticated = False
        self.last_room_snapshot: dict | None = None
        self.closing = False
        self.done = False
        self.exit_code = 0
        self.last_write = time.monotonic()
        self.partial_since: float | None = None
        self.decode_pending = False

    def emit(self, text: str) -> None:
        print(terminal_text(text), file=self.output_stream, flush=True)

    def send_request(self, action: str, payload: dict, now: float) -> None:
        if len(self.pending) >= MAX_PENDING_REQUESTS and action != "quit":
            raise ClientError("CLIENT_BUSY", "Too many requests are awaiting replies.")
        if self.next_request_id > MAX_REQUEST_ID:
            raise ClientError("REQUEST_LIMIT", "Request ID limit reached; reconnect.")
        request_id = self.next_request_id
        frame = encode_message({
            "version": 1, "kind": "request", "type": action,
            "request_id": request_id, "payload": payload,
        })
        if len(self.outgoing) + len(frame) > MAX_OUTPUT_BYTES:
            raise ClientError("CLIENT_BUSY", "Server is not accepting outgoing data.")
        if not self.outgoing:
            self.last_write = now
        self.outgoing.extend(frame)
        self.next_request_id += 1
        timeout = QUIT_TIMEOUT if action == "quit" else REQUEST_TIMEOUT
        self.pending[request_id] = PendingRequest(action, now + timeout)

    def request_quit(self, now: float) -> None:
        if self.closing:
            self.done = True
            return
        self.closing = True
        self.stop_input.set()
        self.context.set(None)
        self.send_request("quit", {}, now)

    def show_room(self, room: dict, *, listing: bool = False) -> None:
        self.emit(
            f"ROOM room_id={room['room_id']} quiz_id={room['quiz_id']} "
            f"state={room['state']} host_id={room['host_id']}"
        )
        self.emit(f"TITLE {room['title']}")
        if listing:
            self.emit(f"PLAYERS count={room['player_count']}")
        else:
            for player in room["players"]:
                self.emit(
                    f"PLAYER user_id={player['user_id']} ready={str(player['ready']).lower()} "
                    f"name={player['name']}"
                )

    def show_leaderboard(self, rows: list[dict]) -> None:
        self.emit("LEADERBOARD")
        for row in rows:
            self.emit(
                f"RANK rank={row['rank']} user_id={row['user_id']} score={row['score']} "
                f"active={str(row['active']).lower()} name={row['name']}"
            )

    def handle_message(self, message: dict) -> None:
        kind, identifier, action, payload = validate_server_message(message)
        if kind == "event":
            if identifier <= self.last_event_id:
                raise ClientError("PROTOCOL_ERROR", "Server event IDs are not increasing.")
            self.last_event_id = identifier
            self.handle_event(action, payload)
            return
        if action == "protocol":
            raise ClientError(payload["code"], payload["message"])
        pending = self.pending.get(identifier)
        if pending is None or pending.action != action:
            raise ClientError("PROTOCOL_ERROR", "Response does not match an outstanding request.")
        del self.pending[identifier]
        if kind == "error":
            self.emit(f"ERROR code={payload['code']} message={payload['message']}")
            if action in {"login", "quit"}:
                self.exit_code = 1
                self.done = True
            return
        if action == "login":
            if payload["user_id"] != self.user_id or payload["name"] != self.name:
                raise ClientError("PROTOCOL_ERROR", "Login reply changed the requested identity.")
            self.authenticated = True
            self.emit(f"LOGIN_OK user_id={self.user_id} name={self.name}")
            self.emit("Type /help for commands. Enter /answer A while a question is open.")
        elif action == "quizzes":
            self.emit(f"QUIZZES count={len(payload['quizzes'])}")
            for quiz in payload["quizzes"]:
                self.emit(
                    f"QUIZ quiz_id={quiz['quiz_id']} questions={quiz['question_count']} "
                    f"title={quiz['title']}"
                )
                self.emit(f"DESCRIPTION {quiz['description']}")
        elif action == "rooms":
            self.emit(f"ROOMS count={len(payload['rooms'])}")
            for room in payload["rooms"]:
                self.show_room(room, listing=True)
        elif action in {"create", "join", "players", "ready", "start"}:
            self.show_room(payload)
            self.last_room_snapshot = payload
        elif action == "answer":
            self.emit(f"ANSWER_ACCEPTED question_id={payload['question_id']}")
        elif action == "score":
            self.show_leaderboard(payload["leaderboard"])
        elif action == "leave":
            self.context.set(None)
            self.last_room_snapshot = None
            self.emit(f"LEFT room_id={payload['room_id']}")
        elif action == "quit":
            self.emit("GOODBYE")
            self.done = True

    def handle_event(self, action: str, payload: dict) -> None:
        if action == "room_updated":
            if payload != self.last_room_snapshot:
                self.show_room(payload)
                self.last_room_snapshot = payload
        elif action == "quiz_started":
            self.context.set(None)
            self.emit(
                f"STARTED room_id={payload['room_id']} quiz_id={payload['quiz_id']} "
                f"questions={payload['total_questions']} title={payload['title']}"
            )
        elif action == "question_opened":
            self.context.set(payload["question_id"])
            self.emit(
                f"QUESTION id={payload['question_id']} number={payload['number']}/"
                f"{payload['total_questions']} time_limit={payload['time_limit']} "
                f"points={payload['points']}"
            )
            self.emit(payload["text"])
            for label in "ABCD":
                self.emit(f"  {label}. {payload['options'][label]}")
        elif action == "question_closed":
            if self.context.capture() == payload["question_id"]:
                self.context.set(None)
            self.emit(
                f"RESULT question_id={payload['question_id']} correct={payload['correct_answer']} "
                f"your_answer={payload['your_answer'] or '-'} points_awarded={payload['points_awarded']}"
            )
            self.show_leaderboard(payload["leaderboard"])
        elif action == "quiz_finished":
            self.context.set(None)
            self.emit(f"FINISHED room_id={payload['room_id']} quiz_id={payload['quiz_id']}")
            self.show_leaderboard(payload["final_leaderboard"])
            self.emit(f"WINNERS {', '.join(payload['winners'])}")

    def process_inputs(self, now: float) -> None:
        if not self.authenticated or self.closing:
            return
        for _ in range(16):
            try:
                item = self.inputs.get_nowait()
            except queue.Empty:
                return
            if item.error:
                self.emit(f"ERROR code=INPUT_ERROR message={item.error}")
            if item.text is None:
                if item.error:
                    self.exit_code = 1
                self.request_quit(now)
                return
            if item.error:
                continue
            try:
                action, payload = parse_command(item.text, item.question_id)
                if action == "help":
                    for line in HELP_LINES:
                        self.emit(line)
                elif action == "quit":
                    self.request_quit(now)
                    return
                elif action:
                    self.send_request(action, payload, now)
            except ClientError as error:
                self.emit(f"ERROR code={error.code} message={error.message}")

    def drain_messages(self, now: float) -> None:
        self.check_timeouts(now)
        for _ in range(32):
            message = self.decoder.pop()
            if message is None:
                self.decode_pending = False
                if not self.decoder.buffered_bytes:
                    self.partial_since = None
                elif self.partial_since is None:
                    self.partial_since = now
                return
            self.handle_message(message)
            self.partial_since = None
            if self.done:
                return
        # Drain buffered frames before receiving more; one busy peer stays bounded.
        self.decode_pending = bool(self.decoder.buffered_bytes)

    def check_timeouts(self, now: float) -> None:
        """Apply deadlines before incoming data can clear the tracked state."""
        for pending in self.pending.values():
            if now >= pending.deadline:
                raise ClientError("REQUEST_TIMEOUT", f"No reply to {pending.action} within the time limit.")
        if self.outgoing and now - self.last_write >= WRITE_TIMEOUT:
            raise ClientError("WRITE_TIMEOUT", "Server stopped accepting data.")
        if self.partial_since is not None and now - self.partial_since >= FRAME_TIMEOUT:
            raise ClientError("PROTOCOL_ERROR", "Server left a message incomplete.")

    def step(self) -> None:
        now = time.monotonic()
        self.drain_messages(now)
        if self.done:
            return
        self.process_inputs(now)
        readable, writable, exceptional = select.select(
            [] if self.decode_pending else [self.connection],
            [self.connection] if self.outgoing else [],
            [self.connection], 0.0 if self.decode_pending else 0.05,
        )
        self.check_timeouts(time.monotonic())
        if exceptional:
            raise ClientError("CONNECTION_LOST", "Server connection failed.")
        if writable:
            try:
                sent = self.connection.send(self.outgoing[:65536])
            except BlockingIOError:
                sent = None
            if sent == 0:
                raise ClientError("CONNECTION_LOST", "Server connection closed during a write.")
            if sent:
                del self.outgoing[:sent]
                self.last_write = time.monotonic()
        if readable:
            try:
                data = self.connection.recv(RECEIVE_CHUNK_BYTES)
            except BlockingIOError:
                return
            if not data:
                raise ClientError("CONNECTION_LOST", "Server disconnected.")
            if self.decoder.buffered_bytes + len(data) > MAX_RECEIVE_BYTES:
                raise ClientError("PROTOCOL_ERROR", "Server exceeded the receive buffer limit.")
            self.decoder.feed(data)
            self.drain_messages(time.monotonic())

    def run(self) -> int:
        try:
            self.send_request("login", {"user_id": self.user_id, "name": self.name}, time.monotonic())
            source = self.input_stream
            if source is sys.stdin and hasattr(source, "buffer") and hasattr(source.buffer, "raw"):
                source = RawLineReader(source.buffer.raw, source.encoding or "utf-8")
            threading.Thread(
                target=read_input,
                args=(source, self.inputs, self.context, self.stop_input),
                daemon=True, name="stdin-reader",
            ).start()
            while not self.done:
                try:
                    self.step()
                except KeyboardInterrupt:
                    self.emit("Disconnecting...")
                    self.request_quit(time.monotonic())
        except (ClientError, ProtocolError) as error:
            self.emit(f"ERROR code={error.code} message={error.message}")
            self.exit_code = 1
        except (OSError, ValueError) as error:
            self.emit(f"ERROR code=CONNECTION_LOST message={error}")
            self.exit_code = 1
        finally:
            self.stop_input.set()
            self.connection.close()
        return self.exit_code


def port_number(value: str) -> int:
    if not value.isascii() or not value.isdecimal() or len(value) > 5 or not 1 <= int(value) <= 65535:
        raise argparse.ArgumentTypeError("port must be an integer from 1 to 65535")
    return int(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Command-line networked quiz client")
    parser.add_argument("--server", required=True, help="server hostname or IP address")
    parser.add_argument("--port", required=True, type=port_number)
    parser.add_argument("--id", required=True, dest="user_id", help="unique user ID")
    parser.add_argument("--name", required=True, help="unique display name")
    arguments = parser.parse_args(argv)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", arguments.user_id):
        parser.error("--id must contain 1–32 ASCII letters, digits, underscores or hyphens")
    if not 1 <= len(arguments.name) <= 64 or arguments.name != arguments.name.strip() or not arguments.name.isprintable():
        parser.error("--name must contain 1–64 printable characters without surrounding whitespace")
    if not arguments.server or arguments.server != arguments.server.strip():
        parser.error("--server must be a hostname or IP address without surrounding whitespace")
    try:
        connection = connect_socket(arguments.server, arguments.port)
        return QuizClient(connection, arguments.user_id, arguments.name).run()
    except KeyboardInterrupt:
        print("Connection cancelled.", file=sys.stderr)
        return 130
    except (ClientError, OSError) as error:
        code = error.code if isinstance(error, ClientError) else "CONNECTION_FAILED"
        print(f"ERROR code={code} message={terminal_text(str(error))}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
