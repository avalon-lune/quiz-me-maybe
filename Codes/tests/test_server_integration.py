"""Real TCP/server tests. Run only in the official examiner Docker image."""

from contextlib import ExitStack
import json
import os
import signal
import socket
import struct
import time
import unittest

from tests.support import HOST, ServerProcess, frame, request_message, sample_bank


class ServerIntegrationTests(unittest.TestCase):
    def assert_success(self, response: dict) -> dict:
        self.assertEqual(response["version"], 1)
        self.assertEqual(response["kind"], "response", response)
        return response["payload"]

    def assert_error(self, response: dict, code: str | None = None) -> None:
        self.assertEqual(response["kind"], "error", response)
        self.assertTrue(response["payload"]["message"])
        if code is not None:
            self.assertEqual(response["payload"]["code"], code)

    def test_invalid_external_banks_fail_before_listening(self):
        invalid_version = sample_bank()
        invalid_version["schema_version"] = True
        duplicate = sample_bank()
        duplicate["quizzes"][1]["questions"][0]["question_id"] = "q1_1"
        missing = sample_bank()
        del missing["quizzes"][0]["description"]
        cases = {
            "empty": b"",
            "invalid_json": b"{",
            "invalid_utf8": b"\xff",
            "boolean_version": json.dumps(invalid_version).encode(),
            "global_duplicate": json.dumps(duplicate).encode(),
            "missing_description": json.dumps(missing).encode(),
            "oversized": b" " * 1_000_001,
        }
        for label, contents in cases.items():
            with self.subTest(case=label), ServerProcess(
                raw_bank=contents, expect_ready=False
            ) as server:
                self.assertNotEqual(server.probe.wait_exit(), 0)
                self.assertNotIn("SERVER_READY", "\n".join(server.probe.stdout))
                self.assertTrue(server.probe.stderr, server.probe.dump())
                self.assertNotIn("Traceback", server.probe.dump())
                with self.assertRaises(OSError):
                    with socket.create_connection((HOST, server.port), timeout=0.5):
                        pass

    def test_single_quiz_external_bank_and_public_catalogue(self):
        bank = sample_bank(1)
        bank["extension"] = {"ignored": True}
        quiz = bank["quizzes"][0]
        quiz["quiz_id"] = "测验一"
        quiz["title"] = "网络基础 — 试题"
        quiz["extension"] = "allowed"
        quiz["questions"][0]["extension"] = "allowed"
        with ServerProcess(bank) as server, server.peer("reader", "读者") as reader:
            catalogue = self.assert_success(reader.request("quizzes"))["quizzes"]
            self.assertEqual(len(catalogue), 1)
            self.assertEqual(
                catalogue[0],
                {
                    "quiz_id": "测验一", "title": "网络基础 — 试题",
                    "description": "", "question_count": 5,
                },
            )
            serialized = json.dumps(reader.received, ensure_ascii=False)
            for question in quiz["questions"]:
                self.assertNotIn(question["text"], serialized)
                self.assertNotIn(question["question_id"], serialized)
                for option in question["options"].values():
                    self.assertNotIn(option, serialized)
            self.assertNotIn('"answer"', serialized)
            self.assertNotIn('"correct_answer"', serialized)
            room = self.assert_success(reader.request("create", {"quiz_id": "测验一"}))
            self.assertEqual(room["quiz_id"], "测验一")

    def test_missing_bank_invalid_ports_and_occupied_port_fail_cleanly(self):
        cases = (
            {"missing_bank": True},
            {"port": 0},
            {"port": 65_536},
        )
        for configuration in cases:
            with self.subTest(configuration=configuration), ServerProcess(
                expect_ready=False, **configuration
            ) as server:
                self.assertNotEqual(server.probe.wait_exit(), 0)
                self.assertNotIn("SERVER_READY", server.probe.dump())
                self.assertTrue(server.probe.stderr, server.probe.dump())
                self.assertNotIn("Traceback", server.probe.dump())
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
            occupied.bind((HOST, 0))
            occupied.listen(1)
            with ServerProcess(port=occupied.getsockname()[1], expect_ready=False) as server:
                self.assertNotEqual(server.probe.wait_exit(), 0)
                self.assertNotIn("SERVER_READY", server.probe.dump())
                self.assertTrue(server.probe.stderr, server.probe.dump())
                self.assertNotIn("Traceback", server.probe.dump())

    def test_fragmented_coalesced_requests_and_recoverable_errors(self):
        with ServerProcess() as server, server.peer() as peer:
            self.assert_error(peer.request("quizzes"), "AUTH_REQUIRED")
            login = frame(request_message(2, "login", {"user_id": "wire", "name": "Wire"}))
            # A header split across writes exercises the stream boundary explicitly.
            peer.socket.sendall(login[:1])
            time.sleep(0.02)
            peer.socket.sendall(login[1:3])
            time.sleep(0.02)
            peer.socket.sendall(login[3:11])
            peer.socket.sendall(login[11:])
            self.assert_success(peer.reply(2))
            peer.socket.sendall(
                frame(request_message(3, "quizzes"))
                + frame(request_message(4, "rooms"))
                + frame(request_message(5, "unsupported_action"))
            )
            self.assertEqual(len(self.assert_success(peer.reply(3))["quizzes"]), 2)
            self.assertEqual(self.assert_success(peer.reply(4))["rooms"], [])
            self.assert_error(peer.reply(5))
            # Exceed one dispatch budget, then send nothing until all replies arrive.
            response_start = len(peer.received)
            peer.socket.sendall(b"".join(
                frame(request_message(request_id, "quizzes")) for request_id in range(6, 86)
            ))
            for request_id in range(6, 86):
                self.assertEqual(len(self.assert_success(peer.reply(request_id))["quizzes"]), 2)
            self.assertEqual(
                [message["request_id"] for message in peer.received[response_start:]],
                list(range(6, 86)),
            )
            peer.socket.sendall(frame(request_message(85, "rooms")))
            self.assert_error(peer.reply(85), "REPEATED_REQUEST")
            wrong_version = request_message(86, "quizzes")
            wrong_version["version"] = 2
            peer.socket.sendall(frame(wrong_version))
            self.assert_error(peer.reply(86))
            wrong_payload = request_message(87, "quizzes")
            wrong_payload["payload"] = []
            peer.socket.sendall(frame(wrong_payload))
            self.assert_error(peer.reply(87))
            peer.next_id = 88
            self.assert_success(peer.request("quizzes"))
            self.assert_success(peer.request("quit"))
            peer.wait_closed()
            self.assertIsNone(server.probe.process.poll())

    def test_malformed_and_slow_peers_are_isolated(self):
        duplicate_keys = b'{"version":1,"version":1}'
        invalid_frames = {
            "zero_length": struct.pack("!I", 0),
            "oversized_length": struct.pack("!I", 65_537),
            "invalid_utf8": struct.pack("!I", 1) + b"\xff",
            "invalid_json": struct.pack("!I", 1) + b"{",
            "duplicate_keys": struct.pack("!I", len(duplicate_keys)) + duplicate_keys,
        }
        with ServerProcess() as server, server.peer("healthy") as healthy:
            for label, contents in invalid_frames.items():
                with self.subTest(case=label), server.peer() as malformed:
                    malformed.socket.sendall(contents)
                    malformed.wait_closed()
                    self.assert_success(healthy.request("quizzes"))
            with server.peer() as slow:
                slow.socket.sendall(b"\x00")
                self.assert_success(healthy.request("rooms"))
                slow.wait_closed(timeout=13)
                self.assert_success(healthy.request("quizzes"))
            self.assertIsNone(server.probe.process.poll())

    def test_identity_room_rules_and_host_disconnect(self):
        with ExitStack() as stack:
            server = stack.enter_context(ServerProcess())
            host = stack.enter_context(server.peer("host", "Host"))
            second = stack.enter_context(server.peer("second", "Second"))
            third = stack.enter_context(server.peer("third", "Third"))
            duplicate = stack.enter_context(server.peer())
            self.assert_error(
                duplicate.request("login", {"user_id": "host", "name": "Unique"}),
                "DUPLICATE_ID",
            )
            self.assert_error(
                duplicate.request("login", {"user_id": "unique", "name": "Host"}),
                "DUPLICATE_NAME",
            )
            self.assert_success(duplicate.request("login", {"user_id": "unique", "name": "Unique"}))
            room = self.assert_success(host.request("create", {"quiz_id": "quiz_1"}))
            room_id = room["room_id"]
            self.assert_error(host.request("start"), "NOT_ENOUGH_PLAYERS")
            self.assert_success(second.request("join", {"room_id": room_id}))
            self.assert_error(second.request("start"), "HOST_ONLY")
            self.assert_error(host.request("start"), "NOT_READY")
            self.assert_success(host.request("ready", {"ready": True}))
            self.assert_success(second.request("ready", {"ready": True}))
            joined = self.assert_success(third.request("join", {"room_id": room_id}))
            readiness = {row["user_id"]: row["ready"] for row in joined["players"]}
            self.assertEqual(readiness, {"host": True, "second": True, "third": False})
            host.close()
            updated = second.event("room_updated", room_id=room_id, host_id="second")
            self.assertEqual(len(updated["payload"]["players"]), 2)
            self.assert_success(third.request("ready", {"ready": True}))
            self.assert_success(second.request("start"))
            self.assert_error(duplicate.request("join", {"room_id": room_id}), "ROOM_CLOSED")
            self.assert_success(second.request("leave"))
            self.assert_success(third.request("leave"))
            self.assertEqual(self.assert_success(duplicate.request("rooms"))["rooms"], [])
            # A processed departure releases both identity fields for a new session.
            with server.peer("host", "Host") as replacement:
                self.assert_success(replacement.request("quizzes"))

    def test_two_complete_rooms_authoritative_scoring_and_confidentiality(self):
        bank = sample_bank()
        bank["quizzes"][1]["questions"][0]["time_limit"] = 6
        with ExitStack() as stack:
            server = stack.enter_context(ServerProcess(bank))
            groups = []
            for quiz_number, identities in enumerate((("alice", "bob"), ("carol", "dana")), 1):
                peers = [stack.enter_context(server.peer(identity)) for identity in identities]
                room = self.assert_success(peers[0].request("create", {"quiz_id": f"quiz_{quiz_number}"}))
                room_id = room["room_id"]
                self.assert_success(peers[1].request("join", {"room_id": room_id}))
                for peer in peers:
                    self.assert_success(peer.request("ready", {"ready": True}))
                groups.append((room_id, peers, identities))
            started = {}
            for room_id, peers, _identities in groups:
                started[room_id] = time.monotonic()
                self.assert_success(peers[0].request("start"))
                for peer in peers:
                    peer.event("quiz_started", room_id=room_id)

            for index in range(5):
                for group_index, (room_id, peers, _identities) in enumerate(groups):
                    expected = bank["quizzes"][group_index]["questions"][index]
                    for peer_index, peer in enumerate(peers):
                        opened = peer.event("question_opened", room_id=room_id)["payload"]
                        self.assertEqual(opened["question_id"], expected["question_id"])
                        for key in ("text", "options", "time_limit", "points"):
                            self.assertEqual(opened[key], expected[key])
                        self.assertNotIn("answer", opened)
                        self.assertNotIn("correct_answer", opened)
                        self.assertEqual(opened["number"], index + 1)
                        if group_index == 0 and peer_index == 0 and index == 1:
                            self.assert_error(peer.request("answer", {
                                "question_id": "q1_1", "choice": "A",
                            }), "STALE_QUESTION")
                        if group_index == 0 and peer_index == 1:
                            if index == 0:
                                self.assert_error(peer.request("answer", {
                                    "question_id": expected["question_id"], "choice": "E",
                                }), "INVALID_CHOICE")
                                self.assert_success(peer.request("answer", {
                                    "question_id": expected["question_id"], "choice": "D",
                                }))
                            continue
                        accepted = self.assert_success(peer.request("answer", {
                            "question_id": expected["question_id"], "choice": expected["answer"],
                        }))
                        self.assertEqual(accepted, {"accepted": True, "question_id": expected["question_id"]})
                        if group_index == 0 and index == 0:
                            self.assert_error(peer.request("answer", {
                                "question_id": expected["question_id"], "choice": "D",
                            }), "ALREADY_ANSWERED")
                    if index == 0:
                        scores = self.assert_success(peers[0].request("score"))["leaderboard"]
                        self.assertTrue(all(row["score"] == 0 for row in scores))
                        for peer in peers:
                            for future in bank["quizzes"][group_index]["questions"][1:]:
                                self.assertNotIn(future["text"], json.dumps(peer.received))

                for group_index, (room_id, peers, _identities) in enumerate(groups):
                    expected = bank["quizzes"][group_index]["questions"][index]
                    for peer_index, peer in enumerate(peers):
                        closed = peer.event("question_closed", room_id=room_id)["payload"]
                        self.assertEqual(closed["question_id"], expected["question_id"])
                        self.assertEqual(closed["correct_answer"], expected["answer"])
                        correct = group_index == 1 or peer_index == 0
                        self.assertEqual(closed["points_awarded"], expected["points"] if correct else 0)
                        if not correct and index > 0:
                            self.assertIsNone(closed["your_answer"])
                    if index == 0:
                        self.assertGreaterEqual(
                            time.monotonic() - started[room_id], expected["time_limit"] - 0.1
                        )
                        self.assert_error(peers[0].request("answer", {
                            "question_id": expected["question_id"], "choice": "A",
                        }))

            for group_index, (room_id, peers, identities) in enumerate(groups):
                total = sum(question["points"] for question in bank["quizzes"][group_index]["questions"])
                for peer in peers:
                    finished = peer.event("quiz_finished", room_id=room_id)["payload"]
                    rows = finished["final_leaderboard"]
                    self.assertEqual([row["user_id"] for row in rows], list(identities))
                    self.assertEqual([row["score"] for row in rows], [total, total if group_index else 0])
                    self.assertEqual([row["rank"] for row in rows], [1, 1 if group_index else 2])
                    self.assertEqual(finished["winners"], list(identities) if group_index else ["alice"])
                    self.assertTrue(all(row["active"] for row in rows))
                    score = self.assert_success(peer.request("score"))
                    self.assertEqual(score["leaderboard"], rows)
                    events = [message for message in peer.received if message["kind"] == "event"]
                    self.assertTrue(all(message["payload"]["room_id"] == room_id for message in events))
                    event_ids = [message["event_id"] for message in events]
                    self.assertEqual(event_ids, sorted(set(event_ids)))
                duration = sum(q["time_limit"] for q in bank["quizzes"][group_index]["questions"]) + 8
                self.assertGreaterEqual(time.monotonic() - started[room_id], duration - 0.1)

    def test_mid_question_disconnect_retains_an_accepted_answer(self):
        with ServerProcess() as server, server.peer("host") as host, server.peer("guest") as guest:
            room_id = self.assert_success(host.request("create", {"quiz_id": "quiz_1"}))["room_id"]
            self.assert_success(guest.request("join", {"room_id": room_id}))
            self.assert_success(host.request("ready", {"ready": True}))
            self.assert_success(guest.request("ready", {"ready": True}))
            self.assert_success(host.request("start"))
            host.event("question_opened", question_id="q1_1")
            guest.event("question_opened", question_id="q1_1")
            self.assert_success(host.request("answer", {"question_id": "q1_1", "choice": "A"}))
            host.close()
            guest.event("room_updated", room_id=room_id, host_id="guest")
            closed = guest.event("question_closed", question_id="q1_1")["payload"]
            rows = closed["leaderboard"]
            self.assertEqual([row["user_id"] for row in rows], ["host", "guest"])
            self.assertEqual([row["score"] for row in rows], [11, 0])
            self.assertEqual([row["active"] for row in rows], [False, True])
            self.assertIsNone(closed["your_answer"])
            self.assert_success(guest.request("leave"))
            self.assertEqual(self.assert_success(guest.request("rooms"))["rooms"], [])

    @unittest.skipIf(os.name == "nt", "SIGINT lifecycle is verified in the official Linux container")
    def test_interrupt_exits_cleanly_and_releases_port(self):
        with ServerProcess() as server, server.peer("connected") as peer:
            port = server.port
            server.probe.process.send_signal(signal.SIGINT)
            self.assertEqual(server.probe.wait_exit(), 0, server.probe.dump())
            self.assertNotIn("Traceback", server.probe.dump())
            peer.wait_closed()
        with ServerProcess(port=port) as restarted, restarted.peer("new_session") as peer:
            self.assert_success(peer.request("quizzes"))


if __name__ == "__main__":
    unittest.main()
