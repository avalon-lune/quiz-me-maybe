"""Bounded subprocess and socket helpers for black-box integration tests."""

from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable


CODES = Path(__file__).resolve().parents[1]
HOST = "127.0.0.1"


def sample_bank(quiz_count: int = 2) -> dict:
    """Use minimum legal question counts and distinct, observable point values."""
    return {
        "schema_version": 1,
        "quizzes": [
            {
                "quiz_id": f"quiz_{quiz_index}",
                "title": f"Test quiz {quiz_index}",
                "description": "",
                "questions": [
                    {
                        "question_id": f"q{quiz_index}_{number}",
                        "text": f"PRIVATE_QUESTION_{quiz_index}_{number}",
                        "options": {
                            choice: f"Option {choice} for {quiz_index}/{number}"
                            for choice in "ABCD"
                        },
                        "answer": "ABCD"[(number - 1) % 4],
                        "time_limit": 5,
                        "points": 10 * quiz_index + number,
                    }
                    for number in range(1, 6)
                ],
            }
            for quiz_index in range(1, quiz_count + 1)
        ],
    }


def available_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((HOST, 0))
        return probe.getsockname()[1]


def frame(message: dict) -> bytes:
    payload = json.dumps(message, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return struct.pack("!I", len(payload)) + payload


def request_message(request_id: int, action: str, payload: dict | None = None) -> dict:
    return {
        "version": 1,
        "kind": "request",
        "type": action,
        "request_id": request_id,
        "payload": {} if payload is None else payload,
    }


class ProcessProbe:
    """Drain both pipes continuously so deadlines never depend on readline()."""

    def __init__(self, arguments: list[str], cwd: Path):
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.condition = threading.Condition()
        environment = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")
        self.process = subprocess.Popen(
            arguments,
            cwd=cwd,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.readers = [
            threading.Thread(target=self._read, args=(pipe, lines), daemon=True)
            for pipe, lines in (
                (self.process.stdout, self.stdout),
                (self.process.stderr, self.stderr),
            )
        ]
        for reader in self.readers:
            reader.start()

    def _read(self, pipe, lines: list[str]) -> None:
        try:
            for line in pipe:
                with self.condition:
                    lines.append(line.rstrip("\r\n"))
                    self.condition.notify_all()
        finally:
            with self.condition:
                self.condition.notify_all()

    def mark(self) -> int:
        with self.condition:
            return len(self.stdout)

    def wait_stdout(
        self, predicate: Callable[[str], bool], timeout: float = 4, start: int = 0
    ) -> tuple[int, str]:
        deadline = time.monotonic() + timeout
        with self.condition:
            cursor = start
            while True:
                for index in range(cursor, len(self.stdout)):
                    if predicate(self.stdout[index]):
                        return index, self.stdout[index]
                cursor = len(self.stdout)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AssertionError(f"Timed out waiting for subprocess output.\n{self.dump()}")
                if self.process.poll() is not None and not any(
                    reader.is_alive() for reader in self.readers
                ):
                    raise AssertionError(f"Subprocess exited before expected output.\n{self.dump()}")
                self.condition.wait(min(remaining, 0.1))

    def wait_line(self, prefix: str, timeout: float = 4, start: int = 0) -> tuple[int, str]:
        return self.wait_stdout(lambda line: line.startswith(prefix), timeout, start)

    def write(self, command: str) -> None:
        if self.process.poll() is not None:
            raise AssertionError(f"Cannot send input to exited subprocess.\n{self.dump()}")
        self.process.stdin.write(command + "\n")
        self.process.stdin.flush()

    def wait_exit(self, timeout: float = 4) -> int:
        try:
            return_code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired as error:
            raise AssertionError(f"Subprocess did not exit.\n{self.dump()}") from error
        for reader in self.readers:
            reader.join(timeout=1)
        return return_code

    def dump(self) -> str:
        with self.condition:
            return "stdout:\n" + "\n".join(self.stdout) + "\nstderr:\n" + "\n".join(self.stderr)

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        for reader in self.readers:
            reader.join(timeout=1)
        for pipe in (self.process.stdin, self.process.stdout, self.process.stderr):
            pipe.close()


class ServerProcess:
    """Run a real server against an isolated external bank, never the bundled bank."""

    def __init__(
        self,
        bank: dict | None = None,
        *,
        raw_bank: bytes | None = None,
        port: int | None = None,
        expect_ready: bool = True,
        missing_bank: bool = False,
    ):
        self.bank = sample_bank() if bank is None else bank
        self.raw_bank = raw_bank
        self.port = available_port() if port is None else port
        self.expect_ready = expect_ready
        self.missing_bank = missing_bank
        self.temporary: tempfile.TemporaryDirectory | None = None
        self.probe: ProcessProbe | None = None

    def __enter__(self) -> ServerProcess:
        self.temporary = tempfile.TemporaryDirectory(prefix="quiz integration ")
        self.directory = Path(self.temporary.name)
        bank_path = self.directory / "题库 fixture.json"
        contents = (
            json.dumps(self.bank, ensure_ascii=False).encode("utf-8")
            if self.raw_bank is None
            else self.raw_bank
        )
        if not self.missing_bank:
            bank_path.write_bytes(contents)
        try:
            self.probe = ProcessProbe(
                [
                    sys.executable, "-u", str(CODES / "server.py"),
                    "--host", HOST, "--port", str(self.port), "--questions", str(bank_path),
                ],
                cwd=self.directory,
            )
            if self.expect_ready:
                self.probe.wait_line("SERVER_READY", timeout=5)
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def peer(self, user_id: str | None = None, name: str | None = None) -> RawPeer:
        peer = RawPeer(self.port)
        if user_id is not None:
            try:
                response = peer.request("login", {"user_id": user_id, "name": name or user_id})
                if response["kind"] != "response":
                    raise AssertionError(f"Fixture login failed: {response}")
            except BaseException:
                peer.close()
                raise
        return peer

    def __exit__(self, *_exc) -> None:
        if self.probe is not None:
            if os.name != "nt" and self.probe.process.poll() is None:
                self.probe.process.send_signal(signal.SIGINT)
                try:
                    self.probe.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
            self.probe.close()
        if self.temporary is not None:
            self.temporary.cleanup()


class RawPeer:
    """An independent wire peer that preserves unsolicited events while requesting."""

    def __init__(self, port: int):
        self.socket = socket.create_connection((HOST, port), timeout=3)
        self.buffer = bytearray()
        self.inbox: list[dict] = []
        self.received: list[dict] = []
        self.next_id = 1

    def send(self, action: str, payload: dict | None = None) -> int:
        request_id = self.next_id
        self.next_id += 1
        self.socket.sendall(frame(request_message(request_id, action, payload)))
        return request_id

    def request(self, action: str, payload: dict | None = None, timeout: float = 3) -> dict:
        return self.reply(self.send(action, payload), timeout)

    def reply(self, request_id: int, timeout: float = 3) -> dict:
        return self.wait_for(
            lambda message: message.get("request_id") == request_id
            and message.get("kind") in ("response", "error"),
            timeout,
        )

    def event(self, event_type: str, timeout: float = 8, **fields) -> dict:
        return self.wait_for(
            lambda message: message.get("kind") == "event"
            and message.get("type") == event_type
            and all(message["payload"].get(key) == value for key, value in fields.items()),
            timeout,
        )

    def wait_for(self, predicate: Callable[[dict], bool], timeout: float) -> dict:
        for index, message in enumerate(self.inbox):
            if predicate(message):
                return self.inbox.pop(index)
        deadline = time.monotonic() + timeout
        while True:
            message = self._receive(deadline)
            self.received.append(message)
            if predicate(message):
                return message
            self.inbox.append(message)

    def _receive(self, deadline: float) -> dict:
        while True:
            if len(self.buffer) >= 4:
                length = struct.unpack("!I", self.buffer[:4])[0]
                if not 1 <= length <= 65_536:
                    raise AssertionError(f"Invalid server frame length {length}")
                if len(self.buffer) >= length + 4:
                    body = bytes(self.buffer[4:length + 4])
                    del self.buffer[:length + 4]
                    message = json.loads(body.decode("utf-8"))
                    if not isinstance(message, dict):
                        raise AssertionError(f"Server sent non-object message: {message}")
                    return message
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"Timed out waiting for server. Pending messages: {self.inbox}")
            self.socket.settimeout(remaining)
            try:
                data = self.socket.recv(65_536)
            except socket.timeout as error:
                raise AssertionError("Timed out waiting for server message") from error
            if not data:
                raise AssertionError("Server closed connection before expected message")
            self.buffer.extend(data)

    def wait_closed(self, timeout: float = 3) -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("Server did not close the connection")
            self.socket.settimeout(remaining)
            try:
                if not self.socket.recv(65_536):
                    return
            except ConnectionResetError:
                return
            except socket.timeout as error:
                raise AssertionError("Server did not close the connection") from error

    def close(self) -> None:
        self.socket.close()

    def __enter__(self) -> RawPeer:
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
