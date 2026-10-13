"""Deterministic tournament rules; the transport supplies monotonic timestamps."""

from dataclasses import dataclass, field
import re

from question_bank import CHOICES, Question, Quiz


MAX_ROOMS = 64
MAX_PLAYERS = 32
MIN_PLAYERS = 2
INTERMISSION_SECONDS = 2.0
USER_ID = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")


class GameError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True)
class Event:
    recipients: tuple[int, ...]
    type: str
    payload: dict


@dataclass
class Outcome:
    payload: dict
    events: list[Event]
    close: bool = False


@dataclass
class _Session:
    user_id: str | None = None
    name: str | None = None
    room_id: str | None = None


@dataclass
class _Player:
    user_id: str
    name: str
    ready: bool = False
    score: int = 0
    active: bool = True


@dataclass
class _Room:
    room_id: str
    quiz: Quiz
    host: int
    members: dict[int, _Player] = field(default_factory=dict)
    contestants: dict[int, _Player] = field(default_factory=dict)
    state: str = "waiting"
    question_index: int = -1
    deadline: float | None = None
    next_question_at: float | None = None
    answers: dict[int, str] = field(default_factory=dict)

    @property
    def question(self) -> Question:
        return self.quiz.questions[self.question_index]


class Game:
    """Single-owner state machine with no sockets, threads, clocks or file access."""

    _FIELDS = {
        "login": {"user_id", "name"},
        "quizzes": set(),
        "rooms": set(),
        "create": {"quiz_id"},
        "join": {"room_id"},
        "players": set(),
        "ready": {"ready"},
        "start": set(),
        "answer": {"question_id", "choice"},
        "score": set(),
        "leave": set(),
        "quit": set(),
    }

    def __init__(self, bank: dict[str, Quiz]):
        self._bank = dict(bank)
        self._sessions: dict[int, _Session] = {}
        self._rooms_by_id: dict[str, _Room] = {}
        self._next_room_id = 1

    def connect(self, session_id: int) -> None:
        if session_id in self._sessions:
            raise GameError("SESSION_EXISTS", "Session is already connected")
        self._sessions[session_id] = _Session()

    def is_authenticated(self, session_id: int) -> bool:
        session = self._sessions.get(session_id)
        return session is not None and session.user_id is not None

    def handle(self, session_id: int, action: str, payload: dict, now: float) -> Outcome:
        if session_id not in self._sessions:
            raise GameError("SESSION_CLOSED", "Session is no longer connected")
        if not isinstance(action, str) or action not in self._FIELDS:
            raise GameError("UNKNOWN_ACTION", "Unknown request action")
        if not isinstance(payload, dict) or set(payload) != self._FIELDS[action]:
            fields = ", ".join(sorted(self._FIELDS[action])) or "no fields"
            raise GameError("BAD_PAYLOAD", f"{action} requires exactly: {fields}")
        if action == "quit":
            return Outcome({}, self.disconnect(session_id, now), close=True)
        if action == "login":
            return self._login(session_id, payload)
        if not self.is_authenticated(session_id):
            raise GameError("AUTH_REQUIRED", "Log in before using this command")

        handler = getattr(self, f"_{action}")
        return handler(session_id, payload, now)

    def _login(self, session_id: int, payload: dict) -> Outcome:
        if self.is_authenticated(session_id):
            raise GameError("ALREADY_AUTHENTICATED", "This session has already logged in")
        user_id, name = payload["user_id"], payload["name"]
        if not isinstance(user_id, str) or USER_ID.fullmatch(user_id) is None:
            raise GameError("INVALID_ID", "User ID requires 1..32 ASCII letters, digits, '_' or '-'")
        if (
            not isinstance(name, str)
            or not 1 <= len(name) <= 64
            or not name.isprintable()
            or name != name.strip()
        ):
            raise GameError("INVALID_NAME", "Name requires 1..64 printable characters without outer whitespace")
        for session in self._sessions.values():
            if session.user_id == user_id:
                raise GameError("DUPLICATE_ID", "User ID is already connected")
            if session.name == name:
                raise GameError("DUPLICATE_NAME", "Display name is already connected")
        self._sessions[session_id].user_id = user_id
        self._sessions[session_id].name = name
        return Outcome({"user_id": user_id, "name": name}, [])

    def _quizzes(self, session_id: int, payload: dict, now: float) -> Outcome:
        quizzes = [
            {"quiz_id": quiz.quiz_id, "title": quiz.title,
             "description": quiz.description, "question_count": len(quiz.questions)}
            for quiz in self._bank.values()
        ]
        return Outcome({"quizzes": quizzes}, [])

    def _rooms(self, session_id: int, payload: dict, now: float) -> Outcome:
        rooms = [
            {"room_id": room.room_id, "quiz_id": room.quiz.quiz_id,
             "title": room.quiz.title, "host_id": room.members[room.host].user_id,
             "player_count": len(room.members), "state": room.state}
            for room in self._rooms_by_id.values()
            if room.state == "waiting" and len(room.members) < MAX_PLAYERS
        ]
        return Outcome({"rooms": rooms}, [])

    def _require_lobby(self, session_id: int) -> _Session:
        session = self._sessions[session_id]
        if session.room_id is not None:
            raise GameError("ALREADY_IN_ROOM", "Leave the current room first")
        return session

    def _room_for(self, session_id: int) -> _Room:
        room_id = self._sessions[session_id].room_id
        if room_id is None:
            raise GameError("NOT_IN_ROOM", "Join or create a room first")
        return self._rooms_by_id[room_id]

    @staticmethod
    def _snapshot(room: _Room) -> dict:
        return {
            "room_id": room.room_id,
            "quiz_id": room.quiz.quiz_id,
            "title": room.quiz.title,
            "state": room.state,
            "host_id": room.members[room.host].user_id,
            "players": [
                {"user_id": player.user_id, "name": player.name, "ready": player.ready}
                for player in room.members.values()
            ],
        }

    @classmethod
    def _room_update(cls, room: _Room) -> Event:
        return Event(tuple(room.members), "room_updated", cls._snapshot(room))

    @classmethod
    def _room_outcome(cls, room: _Room) -> Outcome:
        return Outcome(cls._snapshot(room), [cls._room_update(room)])

    def _add_member(self, room: _Room, session_id: int) -> None:
        session = self._sessions[session_id]
        room.members[session_id] = _Player(session.user_id, session.name)
        session.room_id = room.room_id

    def _create(self, session_id: int, payload: dict, now: float) -> Outcome:
        self._require_lobby(session_id)
        quiz_id = payload["quiz_id"]
        if not isinstance(quiz_id, str) or quiz_id not in self._bank:
            raise GameError("NO_SUCH_QUIZ", "Quiz ID does not exist")
        if len(self._rooms_by_id) >= MAX_ROOMS:
            raise GameError("ROOM_LIMIT", "Server room capacity has been reached")
        room_id = f"R{self._next_room_id:04d}"
        self._next_room_id += 1
        room = _Room(room_id, self._bank[quiz_id], session_id)
        self._rooms_by_id[room_id] = room
        self._add_member(room, session_id)
        return self._room_outcome(room)

    def _join(self, session_id: int, payload: dict, now: float) -> Outcome:
        self._require_lobby(session_id)
        room_id = payload["room_id"]
        if not isinstance(room_id, str) or room_id not in self._rooms_by_id:
            raise GameError("ROOM_NOT_FOUND", "Room ID does not exist")
        room = self._rooms_by_id[room_id]
        if room.state != "waiting":
            raise GameError("ROOM_CLOSED", "The room has already started")
        if len(room.members) >= MAX_PLAYERS:
            raise GameError("ROOM_FULL", "The room is full")
        self._add_member(room, session_id)
        return self._room_outcome(room)

    def _players(self, session_id: int, payload: dict, now: float) -> Outcome:
        return Outcome(self._snapshot(self._room_for(session_id)), [])

    def _ready(self, session_id: int, payload: dict, now: float) -> Outcome:
        room = self._room_for(session_id)
        if room.state != "waiting":
            raise GameError("ROOM_CLOSED", "Readiness changes only before a quiz starts")
        if type(payload["ready"]) is not bool:
            raise GameError("BAD_PAYLOAD", "ready must be a boolean")
        room.members[session_id].ready = payload["ready"]
        return self._room_outcome(room)

    def _start(self, session_id: int, payload: dict, now: float) -> Outcome:
        room = self._room_for(session_id)
        if room.state != "waiting":
            raise GameError("ROOM_CLOSED", "The room has already started")
        if room.host != session_id:
            raise GameError("HOST_ONLY", "Only the host can start the quiz")
        if len(room.members) < MIN_PLAYERS:
            raise GameError("NOT_ENOUGH_PLAYERS", f"At least {MIN_PLAYERS} players are required")
        if not all(player.ready for player in room.members.values()):
            raise GameError("NOT_READY", "Every player must be ready")
        room.state = "running"
        room.contestants = dict(room.members)
        events = [self._room_update(room), Event(tuple(room.members), "quiz_started", {
            "room_id": room.room_id, "quiz_id": room.quiz.quiz_id,
            "title": room.quiz.title, "total_questions": len(room.quiz.questions),
        })]
        events.append(self._open_question(room, now))
        return Outcome(self._snapshot(room), events)

    @staticmethod
    def _open_question(room: _Room, now: float) -> Event:
        room.question_index += 1
        room.next_question_at = None
        room.answers.clear()
        question = room.question
        room.deadline = now + question.time_limit
        return Event(tuple(room.members), "question_opened", {
            "room_id": room.room_id, "question_id": question.question_id,
            "number": room.question_index + 1, "total_questions": len(room.quiz.questions),
            "text": question.text, "options": dict(question.options),
            "time_limit": question.time_limit, "points": question.points,
        })

    def _answer(self, session_id: int, payload: dict, now: float) -> Outcome:
        room = self._room_for(session_id)
        if room.state != "running" or room.deadline is None or now >= room.deadline:
            raise GameError("QUESTION_CLOSED", "There is no open question accepting answers")
        if payload["question_id"] != room.question.question_id:
            raise GameError("STALE_QUESTION", "Answer must identify the currently open question")
        choice = payload["choice"]
        if not isinstance(choice, str) or choice not in CHOICES:
            raise GameError("INVALID_CHOICE", "Answer must be A, B, C or D")
        if session_id in room.answers:
            raise GameError("ALREADY_ANSWERED", "An answer has already been accepted for this question")
        room.answers[session_id] = choice
        return Outcome({"accepted": True, "question_id": room.question.question_id}, [])

    @staticmethod
    def _leaderboard(room: _Room) -> list[dict]:
        players = room.contestants.values() if room.state != "waiting" else room.members.values()
        ordered = sorted(players, key=lambda player: (-player.score, player.user_id))
        result = []
        previous_score = None
        rank = 0
        for position, player in enumerate(ordered, 1):
            if player.score != previous_score:
                rank = position
            previous_score = player.score
            result.append({"rank": rank, "user_id": player.user_id, "name": player.name,
                           "score": player.score, "active": player.active})
        return result

    def _score(self, session_id: int, payload: dict, now: float) -> Outcome:
        room = self._room_for(session_id)
        return Outcome({"room_id": room.room_id, "leaderboard": self._leaderboard(room)}, [])

    def _leave(self, session_id: int, payload: dict, now: float) -> Outcome:
        room = self._room_for(session_id)
        player = room.members.pop(session_id)
        player.active = False
        self._sessions[session_id].room_id = None
        if not room.members:
            del self._rooms_by_id[room.room_id]
            return Outcome({"room_id": room.room_id}, [])
        if room.host == session_id:
            room.host = next(iter(room.members))
        return Outcome({"room_id": room.room_id}, [self._room_update(room)])

    def disconnect(self, session_id: int, now: float) -> list[Event]:
        session = self._sessions.get(session_id)
        if session is None:
            return []
        events = self._leave(session_id, {}, now).events if session.room_id is not None else []
        del self._sessions[session_id]
        return events

    def _close_question(self, room: _Room, now: float) -> list[Event]:
        question = room.question
        awarded = {}
        for session_id, player in room.contestants.items():
            points = question.points if room.answers.get(session_id) == question.answer else 0
            awarded[session_id] = points
            player.score += points
        room.deadline = None
        events = [
            Event((session_id,), "question_closed", {
                "room_id": room.room_id, "question_id": question.question_id,
                "correct_answer": question.answer, "your_answer": room.answers.get(session_id),
                "points_awarded": awarded[session_id], "leaderboard": self._leaderboard(room),
            })
            for session_id in room.members
        ]
        if room.question_index + 1 == len(room.quiz.questions):
            room.state = "finished"
            leaderboard = self._leaderboard(room)
            events.append(Event(tuple(room.members), "quiz_finished", {
                "room_id": room.room_id, "quiz_id": room.quiz.quiz_id,
                "final_leaderboard": leaderboard,
                "winners": [entry["user_id"] for entry in leaderboard if entry["rank"] == 1],
            }))
            events.append(self._room_update(room))
        else:
            room.next_question_at = now + INTERMISSION_SECONDS
        return events

    def tick(self, now: float) -> list[Event]:
        events = []
        for room in self._rooms_by_id.values():
            if room.state != "running":
                continue
            if room.deadline is not None and now >= room.deadline:
                events.extend(self._close_question(room, now))
            elif room.next_question_at is not None and now >= room.next_question_at:
                # A delayed loop still grants the newly broadcast question its full time.
                events.append(self._open_question(room, now))
        return events

    def next_deadline(self) -> float | None:
        deadlines = [
            deadline
            for room in self._rooms_by_id.values()
            for deadline in (room.deadline, room.next_question_at)
            if deadline is not None
        ]
        return min(deadlines, default=None)
