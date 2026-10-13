"""Tournament state transitions using explicit times instead of sleeping."""

import unittest

from game import Game, GameError, MAX_PLAYERS, MAX_ROOMS
from question_bank import Question, Quiz


def example_bank() -> dict[str, Quiz]:
    return {
        quiz_id: Quiz(quiz_id, f"Quiz {quiz_id}", "Test fixture", tuple(
            Question(f"{quiz_id}_{number}", f"Question {number}",
                     {"A": "First", "B": "Second", "C": "Third", "D": "Fourth"},
                     "A" if number % 2 == 0 else "B", duration, 10 * (number + 1))
            for number in range(5)
        ))
        for quiz_id, duration in (("fast", 5), ("slow", 9))
    }


class GameTests(unittest.TestCase):
    def setUp(self):
        self.game = Game(example_bank())
        self.login(1, "alice", "Alice")
        self.login(2, "bob", "Bob")
        self.login(3, "carol", "Carol")

    def login(self, session_id: int, user_id: str, name: str):
        self.game.connect(session_id)
        return self.command(session_id, "login", {"user_id": user_id, "name": name})

    def command(self, session_id: int, action: str, payload: dict | None = None, now: float = 0):
        return self.game.handle(session_id, action, {} if payload is None else payload, now)

    def expect_error(self, code: str, session_id: int, action: str,
                     payload: dict | None = None, now: float = 0):
        with self.assertRaises(GameError) as raised:
            self.command(session_id, action, payload, now)
        self.assertEqual(raised.exception.code, code)

    def create_room(self, players=(1, 2), quiz_id="fast") -> str:
        room_id = self.command(players[0], "create", {"quiz_id": quiz_id}).payload["room_id"]
        for player in players[1:]:
            self.command(player, "join", {"room_id": room_id})
        return room_id

    def start_room(self, players=(1, 2), quiz_id="fast", now=10):
        self.create_room(players, quiz_id)
        for player in players:
            self.command(player, "ready", {"ready": True})
        return self.command(players[0], "start", now=now)

    def answer(self, session_id: int, choice: str, now: float, question_id="fast_0"):
        return self.command(session_id, "answer", {"question_id": question_id, "choice": choice}, now)

    def scores(self, session_id=1):
        return {row["user_id"]: row["score"] for row in self.command(session_id, "score").payload["leaderboard"]}

    def test_authentication_uniqueness_validation_and_release(self):
        self.game.connect(4)
        self.expect_error("AUTH_REQUIRED", 4, "quizzes")
        self.expect_error("DUPLICATE_ID", 4, "login", {"user_id": "alice", "name": "Unique"})
        self.expect_error("DUPLICATE_NAME", 4, "login", {"user_id": "unique", "name": "Alice"})
        self.expect_error("INVALID_ID", 4, "login", {"user_id": "用户", "name": "Unique"})
        for name in ("", " Alice", "Alice ", "x" * 65, "Bad\x1bName", True):
            with self.subTest(name=name):
                self.expect_error("INVALID_NAME", 4, "login", {"user_id": "unique", "name": name})
        self.assertFalse(self.game.is_authenticated(4))
        self.game.disconnect(1, 0)
        self.command(4, "login", {"user_id": "alice", "name": "Alice"})
        self.assertTrue(self.game.is_authenticated(4))
        self.expect_error("ALREADY_AUTHENTICATED", 4, "login", {"user_id": "new", "name": "New"})

    def test_case_sensitive_identity_and_unicode_name(self):
        self.login(4, "Alice", "爱丽丝")
        self.assertTrue(self.game.is_authenticated(4))
        self.assertTrue(self.game.is_authenticated(1))

    def test_quit_before_login_and_repeat_disconnect_are_safe(self):
        self.game.connect(4)
        result = self.command(4, "quit")
        self.assertTrue(result.close)
        self.assertEqual(result.payload, {})
        self.assertEqual(self.game.disconnect(4, 1), [])
        self.expect_error("SESSION_CLOSED", 4, "quizzes")

    def test_bad_payload_and_unknown_action_do_not_change_state(self):
        self.expect_error("UNKNOWN_ACTION", 1, "launch")
        self.expect_error("BAD_PAYLOAD", 1, "create", {})
        self.expect_error("BAD_PAYLOAD", 1, "quizzes", {"user_id": "bob"})
        self.expect_error("NO_SUCH_QUIZ", 1, "create", {"quiz_id": []})
        self.expect_error("ROOM_NOT_FOUND", 1, "join", {"room_id": {}})
        self.assertEqual(self.command(1, "rooms").payload, {"rooms": []})

    def test_catalogue_exposes_metadata_only_and_snapshots_are_detached(self):
        first = self.command(1, "quizzes").payload
        for quiz in first["quizzes"]:
            self.assertEqual(set(quiz), {"quiz_id", "title", "description", "question_count"})
        first["quizzes"][0]["title"] = "Changed"
        self.assertEqual(self.command(1, "quizzes").payload["quizzes"][0]["title"], "Quiz fast")

    def test_host_transfer_readiness_and_empty_room_cleanup(self):
        room_id = self.create_room((1, 2, 3))
        self.command(2, "ready", {"ready": True})
        events = self.game.disconnect(1, 0)
        self.assertEqual(events[0].recipients, (2, 3))
        snapshot = self.command(2, "players").payload
        self.assertEqual(snapshot["host_id"], "bob")
        self.assertTrue(snapshot["players"][0]["ready"])
        self.command(2, "leave")
        self.command(3, "leave")
        self.assertEqual(self.command(3, "rooms").payload["rooms"], [])
        self.expect_error("ROOM_NOT_FOUND", 3, "join", {"room_id": room_id})
        self.assertIsNone(self.game.next_deadline())

    def test_one_room_per_player_and_snapshots_are_detached(self):
        created = self.command(1, "create", {"quiz_id": "fast"})
        created.payload["players"][0]["ready"] = True
        created.events[0].payload["players"].clear()
        self.assertFalse(self.command(1, "players").payload["players"][0]["ready"])
        self.expect_error("ALREADY_IN_ROOM", 1, "create", {"quiz_id": "slow"})
        self.expect_error("ALREADY_IN_ROOM", 1, "join", {"room_id": created.payload["room_id"]})
        self.expect_error("NOT_IN_ROOM", 2, "players")
        self.expect_error("NOT_IN_ROOM", 2, "score")

    def test_start_requires_host_minimum_players_and_all_ready(self):
        room_id = self.create_room((1,))
        self.command(1, "ready", {"ready": True})
        self.expect_error("NOT_ENOUGH_PLAYERS", 1, "start")
        self.command(2, "join", {"room_id": room_id})
        self.expect_error("HOST_ONLY", 2, "start")
        self.expect_error("NOT_READY", 1, "start")
        self.expect_error("BAD_PAYLOAD", 2, "ready", {"ready": 1})
        self.command(2, "ready", {"ready": True})
        self.command(2, "ready", {"ready": False})
        self.expect_error("NOT_READY", 1, "start")
        self.command(2, "ready", {"ready": True})
        result = self.command(1, "start", now=10)
        self.assertEqual(result.payload["state"], "running")
        self.assertEqual([event.type for event in result.events],
                         ["room_updated", "quiz_started", "question_opened"])
        self.expect_error("ROOM_CLOSED", 3, "join", {"room_id": room_id})
        self.expect_error("ROOM_CLOSED", 1, "ready", {"ready": False})
        self.assertEqual(self.command(3, "rooms").payload["rooms"], [])

    def test_answers_are_private_until_closure_and_only_first_valid_counts(self):
        result = self.start_room()
        question = result.events[-1].payload
        self.assertNotIn("answer", question)
        self.assertNotIn("correct_answer", question)
        self.expect_error("INVALID_CHOICE", 1, "answer", {"question_id": "fast_0", "choice": "Z"}, 11)
        self.expect_error("STALE_QUESTION", 1, "answer", {"question_id": "slow_0", "choice": "A"}, 11)
        accepted = self.answer(1, "A", 12)
        self.assertEqual(accepted.payload, {"accepted": True, "question_id": "fast_0"})
        self.assertEqual(accepted.events, [])
        self.expect_error("ALREADY_ANSWERED", 1, "answer", {"question_id": "fast_0", "choice": "B"}, 13)
        self.answer(2, "C", 13)
        self.assertEqual(self.scores(), {"alice": 0, "bob": 0})
        self.assertEqual(self.game.tick(14.999), [])
        events = self.game.tick(15)
        results = {event.recipients[0]: event.payload for event in events}
        self.assertEqual(results[1]["your_answer"], "A")
        self.assertEqual(results[1]["points_awarded"], 10)
        self.assertEqual(results[2]["your_answer"], "C")
        self.assertEqual(results[2]["points_awarded"], 0)
        self.assertEqual(results[1]["correct_answer"], "A")
        self.assertEqual(self.scores(), {"alice": 10, "bob": 0})
        self.assertEqual(self.game.tick(15), [])

    def test_exact_deadline_and_stale_question_during_next_question(self):
        self.start_room()
        self.answer(1, "A", 14.999)
        self.expect_error("QUESTION_CLOSED", 2, "answer", {"question_id": "fast_0", "choice": "A"}, 15)
        self.game.tick(15)
        self.assertEqual(self.game.next_deadline(), 17)
        self.expect_error("QUESTION_CLOSED", 2, "answer", {"question_id": "fast_0", "choice": "A"}, 16)
        self.assertEqual(self.game.tick(16.999), [])
        events = self.game.tick(17)
        self.assertEqual(events[0].payload["question_id"], "fast_1")
        self.assertEqual(self.game.next_deadline(), 22)
        self.expect_error("STALE_QUESTION", 2, "answer", {"question_id": "fast_0", "choice": "A"}, 18)
        self.answer(2, "B", 18, "fast_1")

    def test_delayed_tick_never_skips_unseen_questions(self):
        self.start_room()
        closed = self.game.tick(100)
        self.assertTrue(all(event.type == "question_closed" for event in closed))
        self.assertEqual(self.game.next_deadline(), 102)
        opened = self.game.tick(200)
        self.assertEqual(opened[0].payload["question_id"], "fast_1")
        self.assertEqual(self.game.next_deadline(), 205)

    def test_departure_preserves_accepted_answer_and_does_not_pause_match(self):
        self.start_room()
        self.answer(1, "A", 11)
        self.game.disconnect(1, 12)
        self.assertEqual(self.command(2, "players").payload["host_id"], "bob")
        self.login(4, "alice", "Alice")
        self.create_room((4, 3), "slow")
        results = self.game.tick(15)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].recipients, (2,))
        rows = results[0].payload["leaderboard"]
        self.assertEqual(rows[0]["user_id"], "alice")
        self.assertEqual(rows[0]["score"], 10)
        self.assertFalse(rows[0]["active"])
        self.assertEqual(self.scores(4), {"alice": 0, "carol": 0})
        self.game.tick(17)
        self.answer(2, "B", 18, "fast_1")

    def test_last_departure_removes_running_room_and_its_timer(self):
        self.start_room()
        self.game.disconnect(1, 11)
        self.game.disconnect(2, 11)
        self.assertIsNone(self.game.next_deadline())
        self.assertEqual(self.game.tick(100), [])
        self.assertEqual(self.game.disconnect(2, 101), [])

    def test_two_rooms_have_separate_members_answers_scores_and_deadlines(self):
        self.login(4, "dave", "Dave")
        fast = self.start_room((1, 2), "fast", 10).payload["room_id"]
        slow = self.start_room((3, 4), "slow", 12).payload["room_id"]
        self.answer(1, "A", 13)
        self.answer(3, "C", 13, "slow_0")
        self.expect_error("STALE_QUESTION", 4, "answer", {"question_id": "fast_0", "choice": "A"}, 13)
        fast_events = self.game.tick(15)
        self.assertTrue(all(event.payload["room_id"] == fast for event in fast_events))
        self.assertTrue(all(set(event.recipients) <= {1, 2} for event in fast_events))
        self.assertEqual(self.scores(1), {"alice": 10, "bob": 0})
        self.assertEqual(self.scores(3), {"carol": 0, "dave": 0})
        self.game.tick(17)
        self.assertEqual(self.game.next_deadline(), 21)
        slow_events = self.game.tick(21)
        self.assertTrue(all(event.payload["room_id"] == slow for event in slow_events))
        self.assertTrue(all(set(event.recipients) <= {3, 4} for event in slow_events))

    def test_complete_quiz_competition_ranks_and_no_rematch(self):
        self.start_room((1, 2, 3))
        final_events = []
        for number in range(5):
            opened_at = 10 + number * 7
            choice = "A" if number % 2 == 0 else "B"
            self.answer(1, choice, opened_at + 1, f"fast_{number}")
            self.answer(2, choice, opened_at + 1, f"fast_{number}")
            final_events = self.game.tick(opened_at + 5)
            if number < 4:
                self.game.tick(opened_at + 7)
        finished = next(event.payload for event in final_events if event.type == "quiz_finished")
        self.assertEqual(finished["winners"], ["alice", "bob"])
        self.assertEqual([row["rank"] for row in finished["final_leaderboard"]], [1, 1, 3])
        self.assertEqual([row["score"] for row in finished["final_leaderboard"]], [150, 150, 0])
        carol_result = next(event.payload for event in final_events
                            if event.type == "question_closed" and event.recipients == (3,))
        self.assertIsNone(carol_result["your_answer"])
        self.assertEqual(self.command(1, "players").payload["state"], "finished")
        self.assertIsNone(self.game.next_deadline())
        self.assertEqual(self.game.tick(1000), [])
        self.expect_error("ROOM_CLOSED", 1, "start")
        self.expect_error("QUESTION_CLOSED", 1, "answer", {"question_id": "fast_4", "choice": "A"}, 1000)

    def test_room_and_member_capacity_recover_after_departure(self):
        room_id = self.create_room((1, 2, 3))
        for session_id in range(4, MAX_PLAYERS + 2):
            self.login(session_id, f"user{session_id}", f"Name{session_id}")
            if session_id <= MAX_PLAYERS:
                self.command(session_id, "join", {"room_id": room_id})
        outsider = MAX_PLAYERS + 1
        self.expect_error("ROOM_FULL", outsider, "join", {"room_id": room_id})
        self.assertEqual(self.command(outsider, "rooms").payload["rooms"], [])
        self.command(2, "leave")
        self.command(outsider, "join", {"room_id": room_id})
        self.assertEqual(len(self.command(1, "players").payload["players"]), MAX_PLAYERS)

        separate = Game(example_bank())
        for session_id in range(MAX_ROOMS + 1):
            separate.connect(session_id)
            separate.handle(session_id, "login", {"user_id": f"u{session_id}", "name": f"N{session_id}"}, 0)
            if session_id < MAX_ROOMS:
                separate.handle(session_id, "create", {"quiz_id": "fast"}, 0)
        with self.assertRaises(GameError) as raised:
            separate.handle(MAX_ROOMS, "create", {"quiz_id": "fast"}, 0)
        self.assertEqual(raised.exception.code, "ROOM_LIMIT")
        separate.disconnect(0, 0)
        self.assertEqual(separate.handle(MAX_ROOMS, "create", {"quiz_id": "fast"}, 0).payload["state"], "waiting")


if __name__ == "__main__":
    unittest.main()
