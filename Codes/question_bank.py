"""Load and validate the complete server-only question bank before listening."""

from dataclasses import dataclass
import json
from pathlib import Path
from types import MappingProxyType
from typing import Mapping

from bank_json import loads as decode_bank_json


MAX_BANK_BYTES = 1_000_000
CHOICES = frozenset("ABCD")
_OUT_OF_RANGE_INTEGER = object()


class BankError(ValueError):
    """The input file cannot be used as a complete question bank."""


@dataclass(frozen=True)
class Question:
    question_id: str
    text: str
    options: Mapping[str, str]
    answer: str
    time_limit: int
    points: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "options", MappingProxyType(dict(self.options)))


@dataclass(frozen=True)
class Quiz:
    quiz_id: str
    title: str
    description: str
    questions: tuple[Question, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "questions", tuple(self.questions))


def _object(value: object, path: str) -> dict:
    if not isinstance(value, dict):
        raise BankError(f"{path} must be an object")
    return value


def _field(obj: dict, key: str, path: str) -> object:
    if key not in obj:
        raise BankError(f"{path}.{key} is required")
    return obj[key]


def _integer(value: object, low: int, high: int, path: str) -> int:
    # bool is a subclass of int, but JSON true is not an integer field.
    if type(value) is not int or not low <= value <= high:
        raise BankError(f"{path} must be an integer in {low}..{high}")
    return value


def _text(value: object, low: int, high: int, path: str) -> str:
    if (
        not isinstance(value, str)
        or not low <= len(value) <= high
        or any(not char.isprintable() for char in value)
        or (low > 0 and not value.strip())
    ):
        raise BankError(f"{path} must contain {low}..{high} printable characters")
    return value


def _identifier(value: object, path: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 32
        or any(not (char.isalnum() or char in "_-") for char in value)
    ):
        raise BankError(f"{path} must contain 1..32 letters, numbers, '_' or '-'")
    return value


def _option(value: object, path: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise BankError(f"{path} must be a string containing 1..200 characters")
    # Options need not be visible text, but must be representable as UTF-8.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise BankError(f"{path} contains an invalid Unicode surrogate")
    return value


def _array(value: object, low: int, high: int, path: str) -> list:
    if not isinstance(value, list) or not low <= len(value) <= high:
        raise BankError(f"{path} must be an array containing {low}..{high} items")
    return value


def _json_integer(value: str) -> object:
    # Every required integer is at most 1000. Keep larger tokens opaque so an
    # ignored extension cannot trigger Python's integer-conversion digit limit.
    if len(value.removeprefix("-")) > 4:
        return _OUT_OF_RANGE_INTEGER
    return int(value)


def _question(value: object, path: str, seen_ids: set[str]) -> Question:
    obj = _object(value, path)
    question_id = _identifier(_field(obj, "question_id", path), f"{path}.question_id")
    if question_id in seen_ids:
        raise BankError(f"{path}.question_id duplicates {question_id!r}")
    seen_ids.add(question_id)
    text = _text(_field(obj, "text", path), 1, 500, f"{path}.text")
    options_path = f"{path}.options"
    options = _object(_field(obj, "options", path), options_path)
    if set(options) != CHOICES:
        raise BankError(f"{options_path} must have exactly the keys A, B, C, D")
    options = {
        choice: _option(options[choice], f"{options_path}.{choice}")
        for choice in "ABCD"
    }
    answer = _field(obj, "answer", path)
    if not isinstance(answer, str) or answer not in CHOICES:
        raise BankError(f"{path}.answer must be A, B, C or D")
    time_limit = _integer(_field(obj, "time_limit", path), 5, 60, f"{path}.time_limit")
    points = _integer(_field(obj, "points", path), 1, 1000, f"{path}.points")
    return Question(question_id, text, options, answer, time_limit, points)


def load_bank(path: str | Path) -> dict[str, Quiz]:
    """Validate every quiz or raise BankError; retain the order in the JSON file."""
    try:
        # A bounded read also covers files that grow after a separate size check.
        with Path(path).open("rb") as source:
            raw = source.read(MAX_BANK_BYTES + 1)
    except OSError as error:
        raise BankError(f"cannot read question bank: {error}") from error
    if not raw or len(raw) > MAX_BANK_BYTES:
        raise BankError(f"question bank must contain 1..{MAX_BANK_BYTES} bytes")
    try:
        root = decode_bank_json(
            raw.decode("utf-8"),
            parse_int=_json_integer,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise BankError(f"invalid UTF-8 JSON question bank: {error}") from error

    root = _object(root, "bank")
    _integer(_field(root, "schema_version", "bank"), 1, 1, "bank.schema_version")
    quizzes = _array(_field(root, "quizzes", "bank"), 1, 20, "bank.quizzes")
    bank = {}
    seen_question_ids: set[str] = set()
    for index, value in enumerate(quizzes):
        quiz_path = f"bank.quizzes[{index}]"
        obj = _object(value, quiz_path)
        quiz_id = _identifier(_field(obj, "quiz_id", quiz_path), f"{quiz_path}.quiz_id")
        if quiz_id in bank:
            raise BankError(f"{quiz_path}.quiz_id duplicates {quiz_id!r}")
        title = _text(_field(obj, "title", quiz_path), 1, 100, f"{quiz_path}.title")
        description = _text(
            _field(obj, "description", quiz_path), 0, 300, f"{quiz_path}.description"
        )
        questions_path = f"{quiz_path}.questions"
        questions = _array(_field(obj, "questions", quiz_path), 5, 50, questions_path)
        parsed = tuple(
            _question(question, f"{questions_path}[{number}]", seen_question_ids)
            for number, question in enumerate(questions)
        )
        bank[quiz_id] = Quiz(quiz_id, title, description, parsed)
    return bank
