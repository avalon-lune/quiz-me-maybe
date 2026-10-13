"""End-to-end checks of the submitted client and server as separate processes."""

from contextlib import ExitStack
import os
import re
import signal
import sys
import time
import unittest

from tests.support import CODES, HOST, ProcessProbe, ServerProcess, available_port, sample_bank


class ClientWorkflowTests(unittest.TestCase):
    def start_client(self, server: ServerProcess, stack: ExitStack, user_id: str) -> ProcessProbe:
        client_directory = server.directory / "client working directory"
        client_directory.mkdir(exist_ok=True)
        self.assertFalse((client_directory / "questions.json").exists())
        client = ProcessProbe(
            [
                sys.executable, "-u", str(CODES / "client.py"),
                "--server", HOST, "--port", str(server.port),
                "--id", user_id, "--name", f"Player {user_id}",
            ],
            cwd=client_directory,
        )
        stack.callback(client.close)
        client.wait_line(f"LOGIN_OK user_id={user_id}", timeout=5)
        return client

    def command(self, client: ProcessProbe, command: str, prefix: str) -> tuple[int, str]:
        mark = client.mark()
        client.write(command)
        return client.wait_line(prefix, start=mark)

    def test_ten_actual_clients_complete_two_isolated_quizzes(self):
        bank = sample_bank()
        with ExitStack() as stack:
            server = stack.enter_context(ServerProcess(bank))
            groups = []
            for quiz_index in range(1, 3):
                members = [
                    (f"p{quiz_index}_{number}", self.start_client(server, stack, f"p{quiz_index}_{number}"))
                    for number in range(1, 6)
                ]
                host = members[0][1]
                mark = host.mark()
                host.write(f"/create quiz_{quiz_index}")
                _, room_line = host.wait_line("ROOM room_id=", start=mark)
                room_id = re.search(r"\broom_id=(\S+)", room_line).group(1)
                for _user_id, client in members[1:]:
                    mark = client.mark()
                    client.write(f"/join {room_id}")
                    client.wait_line(f"ROOM room_id={room_id}", start=mark)
                groups.append((room_id, members))

            # All ten submitted clients stay connected for the full tournament.
            clients = [client for _room_id, members in groups for _user_id, client in members]
            self.assertEqual(len(clients), 10)
            for _room_id, members in groups:
                for user_id, client in members:
                    mark = client.mark()
                    client.write("/ready")
                    client.wait_stdout(
                        lambda line, identity=user_id: line.startswith(f"PLAYER user_id={identity} ")
                        and "ready=true" in line,
                        start=mark,
                    )
            self.assertTrue(all(client.process.poll() is None for client in clients))
            cursors = {client: client.mark() for client in clients}
            started = time.monotonic()
            for _room_id, members in groups:
                members[0][1].write("/start")

            for question_index in range(5):
                for group_index, (_room_id, members) in enumerate(groups):
                    question = bank["quizzes"][group_index]["questions"][question_index]
                    question_id = question["question_id"]
                    for _user_id, client in members:
                        line_index, _ = client.wait_line(
                            f"QUESTION id={question_id} ", timeout=10, start=cursors[client]
                        )
                        client.write(f"/answer {question['answer']} {question_id}")
                        client.wait_line(f"ANSWER_ACCEPTED question_id={question_id}", start=line_index)
                        cursors[client] = line_index + 1
                for group_index, (_room_id, members) in enumerate(groups):
                    question = bank["quizzes"][group_index]["questions"][question_index]
                    for _user_id, client in members:
                        line_index, result = client.wait_line(
                            f"RESULT question_id={question['question_id']} ",
                            timeout=8,
                            start=cursors[client],
                        )
                        self.assertIn(f"correct={question['answer']}", result)
                        self.assertIn(f"your_answer={question['answer']}", result)
                        self.assertIn(f"points_awarded={question['points']}", result)
                        cursors[client] = line_index + 1

            self.assertGreaterEqual(time.monotonic() - started, 32.9)
            for group_index, (room_id, members) in enumerate(groups):
                questions = bank["quizzes"][group_index]["questions"]
                total = sum(question["points"] for question in questions)
                expected_questions = [question["question_id"] for question in questions]
                for _user_id, client in members:
                    finished_index, _ = client.wait_line(
                        f"FINISHED room_id={room_id} ", start=cursors[client]
                    )
                    for ranked_id, _ranked_client in members:
                        client.wait_line(
                            f"RANK rank=1 user_id={ranked_id} score={total} active=true ",
                            start=finished_index,
                        )
                    final_rows = [
                        re.search(r"\buser_id=(\S+)", line).group(1)
                        for line in client.stdout[finished_index + 1:]
                        if line.startswith("RANK ")
                    ]
                    self.assertEqual(final_rows, sorted(user_id for user_id, _ in members))
                    observed_questions = [
                        re.search(r"\bid=(\S+)", line).group(1)
                        for line in client.stdout
                        if line.startswith("QUESTION id=")
                    ]
                    self.assertEqual(observed_questions, expected_questions, client.dump())
                    self.assertFalse(any(line.startswith("ERROR") for line in client.stdout), client.dump())
                    self.assertNotIn("Traceback", client.dump())
                    self.assertIsNone(client.process.poll())

            for client in clients:
                client.write("/quit")
            for client in clients:
                self.assertEqual(client.wait_exit(), 0, client.dump())
            with server.peer("observer") as observer:
                self.assertEqual(observer.request("rooms")["payload"]["rooms"], [])

    def test_actual_clients_recover_from_commands_and_answer_errors(self):
        bank = sample_bank()
        bank["quizzes"][0]["questions"][0]["time_limit"] = 10
        with ExitStack() as stack:
            server = stack.enter_context(ServerProcess(bank))
            host = self.start_client(server, stack, "host")
            guest = self.start_client(server, stack, "guest")
            self.command(host, "/unsupported", "ERROR code=INVALID_COMMAND ")
            self.command(host, "/quizzes", "QUIZZES count=2")
            host.wait_line("QUIZ quiz_id=quiz_1 ")
            self.assertNotIn("PRIVATE_QUESTION", host.dump())
            _, room_line = self.command(host, "/create quiz_1", "ROOM room_id=")
            room_id = re.search(r"\broom_id=(\S+)", room_line).group(1)
            self.command(guest, "/rooms", "ROOMS count=1")
            self.command(guest, f"/join {room_id}", f"ROOM room_id={room_id} ")
            players_index, _ = self.command(guest, "/players", f"ROOM room_id={room_id} ")
            for identity in ("host", "guest"):
                guest.wait_line(f"PLAYER user_id={identity} ", start=players_index)
            self.command(guest, "/start", "ERROR code=HOST_ONLY ")
            self.command(host, "/ready", "PLAYER user_id=host ready=true ")
            self.command(guest, "/ready", "PLAYER user_id=guest ready=true ")
            self.command(guest, "/unready", "PLAYER user_id=guest ready=false ")
            self.command(host, "/start", "ERROR code=NOT_READY ")
            self.command(guest, "/ready", "PLAYER user_id=guest ready=true ")
            self.command(host, "/start", "QUESTION id=q1_1 ")
            guest.wait_line("QUESTION id=q1_1 ")
            self.command(guest, "/answer E q1_1", "ERROR code=INVALID_COMMAND ")
            self.command(host, "/answer A", "ANSWER_ACCEPTED question_id=q1_1")
            self.command(host, "/answer D q1_1", "ERROR code=ALREADY_ANSWERED ")
            self.command(guest, "/answer D q1_1", "ANSWER_ACCEPTED question_id=q1_1")
            self.command(guest, "/answer A q1_1", "ERROR code=ALREADY_ANSWERED ")
            score_index, _ = self.command(host, "/score", "LEADERBOARD")
            for identity in ("guest", "host"):
                host.wait_line(f"RANK rank=1 user_id={identity} score=0 ", start=score_index)
            _, host_result = host.wait_line("RESULT question_id=q1_1 ", timeout=13)
            _, guest_result = guest.wait_line("RESULT question_id=q1_1 ", timeout=13)
            self.assertIn("your_answer=A points_awarded=11", host_result)
            self.assertIn("your_answer=D points_awarded=0", guest_result)
            _, late_error = self.command(guest, "/answer A q1_1", "ERROR code=")
            # Either the intermission or the next question may be current by receipt.
            self.assertRegex(late_error, r"^ERROR code=(QUESTION_CLOSED|STALE_QUESTION) ")
            score_index, _ = self.command(guest, "/score", "LEADERBOARD")
            guest.wait_line("RANK rank=1 user_id=host score=11 ", start=score_index)
            guest.wait_line("RANK rank=2 user_id=guest score=0 ", start=score_index)
            self.command(guest, "/leave", f"LEFT room_id={room_id}")
            self.command(host, "/leave", f"LEFT room_id={room_id}")
            self.command(host, "/rooms", "ROOMS count=0")
            for client in (host, guest):
                self.command(client, "/quit", "GOODBYE")
                self.assertEqual(client.wait_exit(), 0, client.dump())
                self.assertNotIn("Traceback", client.dump())

    @unittest.skipIf(os.name == "nt", "SIGINT lifecycle is verified in the official Linux container")
    def test_client_interrupt_releases_identity_and_transfers_host(self):
        with ExitStack() as stack:
            server = stack.enter_context(ServerProcess())
            client = self.start_client(server, stack, "interrupted")
            guest = stack.enter_context(server.peer("guest"))
            _, room_line = self.command(client, "/create quiz_1", "ROOM room_id=")
            room_id = re.search(r"\broom_id=(\S+)", room_line).group(1)
            self.assertEqual(guest.request("join", {"room_id": room_id})["kind"], "response")
            client.process.send_signal(signal.SIGINT)
            self.assertEqual(client.wait_exit(), 0, client.dump())
            self.assertNotIn("Traceback", client.dump())
            room = guest.event("room_updated", room_id=room_id, host_id="guest")["payload"]
            self.assertEqual([player["user_id"] for player in room["players"]], ["guest"])
            with server.peer("interrupted", "Player interrupted") as replacement:
                self.assertEqual(replacement.request("quizzes")["kind"], "response")
            self.assertIsNone(server.probe.process.poll())

    def test_idle_input_still_receives_events_and_eof_quits(self):
        with ExitStack() as stack:
            server = stack.enter_context(ServerProcess())
            client = self.start_client(server, stack, "idle")
            host = stack.enter_context(server.peer("host"))
            room_id = host.request("create", {"quiz_id": "quiz_1"})["payload"]["room_id"]
            mark = client.mark()
            client.write(f"/join {room_id}")
            client.wait_line(f"ROOM room_id={room_id}", start=mark)
            mark = client.mark()
            client.write("/ready")
            client.wait_line("PLAYER user_id=idle ready=true ", start=mark)
            self.assertEqual(host.request("ready", {"ready": True})["kind"], "response")
            self.assertEqual(host.request("start")["kind"], "response")
            client.wait_line("QUESTION id=q1_1 ", timeout=5)
            # No further stdin input is required for the deadline/result push.
            _, result = client.wait_line("RESULT question_id=q1_1 ", timeout=8)
            self.assertIn("your_answer=- points_awarded=0", result)
            client.process.stdin.close()
            self.assertEqual(client.wait_exit(), 0, client.dump())
            remaining = host.request("players")["payload"]["players"]
            self.assertEqual([player["user_id"] for player in remaining], ["host"])
            with server.peer("idle", "Player idle") as replacement:
                self.assertEqual(replacement.request("quizzes")["kind"], "response")

    def test_connection_failure_exits_with_readable_error(self):
        with ServerProcess() as server:
            client = ProcessProbe(
                [
                    sys.executable, "-u", str(CODES / "client.py"),
                    "--server", HOST, "--port", str(available_port()),
                    "--id", "unreachable", "--name", "Unreachable",
                ],
                cwd=server.directory,
            )
            try:
                self.assertNotEqual(client.wait_exit(timeout=10), 0)
                self.assertIn("ERROR", client.dump())
                self.assertNotIn("Traceback", client.dump())
            finally:
                client.close()

    def test_server_disappearance_does_not_leave_client_hanging(self):
        with ExitStack() as stack:
            server = stack.enter_context(ServerProcess())
            client = self.start_client(server, stack, "survivor")
            server.probe.process.terminate()
            server.probe.wait_exit()
            self.assertNotEqual(client.wait_exit(), 0, client.dump())
            self.assertIn("ERROR", client.dump())
            self.assertNotIn("Traceback", client.dump())


if __name__ == "__main__":
    unittest.main()
