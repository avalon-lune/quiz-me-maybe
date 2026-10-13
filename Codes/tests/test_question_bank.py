"""Whole-file schema validation, boundary values and immutable loaded data."""

from copy import deepcopy
from dataclasses import FrozenInstanceError
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from question_bank import BankError, MAX_BANK_BYTES, load_bank


def valid_bank() -> dict:
    return {
        "schema_version": 1,
        "quizzes": [{
            "quiz_id": "network",
            "title": "Networking",
            "description": "",
            "questions": [
                {"question_id": f"q{number}", "text": f"Question {number}?",
                 "options": {"A": "One", "B": "Two", "C": "Three", "D": "Four"},
                 "answer": "A", "time_limit": 5, "points": 1}
                for number in range(5)
            ],
        }],
    }


class QuestionBankTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "questions.json"

    def load(self, data: dict):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return load_bank(self.path)

    def reject(self, data: dict, location: str):
        with self.assertRaisesRegex(BankError, location):
            self.load(data)

    def encode_raw_value(self, scope: str, field: str, value: str,
                         data: dict | None = None) -> str:
        """Insert JSON text without asking json.dumps to walk a deep tree."""
        data = valid_bank() if data is None else data
        targets = {"bank": data, "quiz": data["quizzes"][0],
                   "question": data["quizzes"][0]["questions"][0]}
        targets[scope][field] = "__raw_json_value__"
        return json.dumps(data, ensure_ascii=False).replace('"__raw_json_value__"', value, 1)

    def test_single_quiz_is_valid_and_loaded_values_are_immutable(self):
        bank = self.load(valid_bank())
        quiz = bank["network"]
        self.assertEqual(len(quiz.questions), 5)
        self.assertIsInstance(quiz.questions, tuple)
        with self.assertRaises(FrozenInstanceError):
            quiz.title = "Changed"
        with self.assertRaises(TypeError):
            quiz.questions[0].options["A"] = "Changed"

    def test_file_and_question_order_are_preserved(self):
        data = valid_bank()
        second = deepcopy(data["quizzes"][0])
        second["quiz_id"] = "alphabetically_first"
        for question in second["questions"]:
            question["question_id"] += "b"
        data["quizzes"].append(second)
        bank = self.load(data)
        self.assertEqual(list(bank), ["network", "alphabetically_first"])
        self.assertEqual([q.question_id for q in bank["network"].questions],
                         [f"q{i}" for i in range(5)])

    def test_extra_members_are_ignored_except_inside_options(self):
        data = valid_bank()
        data["metadata"] = {"anything": [1, 2]}
        data["quizzes"][0]["extra"] = True
        data["quizzes"][0]["questions"][0]["explanation"] = "Optional"
        self.assertIn("network", self.load(data))
        data["quizzes"][0]["questions"][0]["options"]["E"] = "Extra"
        self.reject(data, "options")

    def test_large_integer_extensions_do_not_reject_an_otherwise_valid_bank(self):
        data = valid_bank()
        data["metadata"] = "large_positive"
        data["quizzes"][0]["metadata"] = "large_negative"
        data["quizzes"][0]["questions"][0]["metadata"] = "large_positive"
        raw = json.dumps(data).replace('"large_positive"', "9" * 5000)
        raw = raw.replace('"large_negative"', "-" + "9" * 5000)
        self.path.write_text(raw, encoding="utf-8")
        self.assertIn("network", load_bank(self.path))

    def test_json_extensions_and_escaped_required_strings_preserve_bank_data(self):
        text = '网络 [ ] { } " \\ / 🚀'
        option = '\b\f\n\r\t / \\ " 网络 🚀'
        data = valid_bank()
        data["quizzes"][0]["questions"][0]["text"] = text
        data["quizzes"][0]["questions"][0]["options"]["A"] = option
        expected = self.load(data)
        extensions = {
            "empty array": "[]",
            "empty object": "{}",
            "boolean true": "true",
            "boolean false": "false",
            "null": "null",
            "negative zero": "-0",
            "decimal and exponent numbers": "[0.0,-12.50,1e3,2E+2,3e-2]",
            "escaped string": r'"\u7f51\u7edc [ ] { } \" \\ \/"',
            "nested containers": '{"empty":[],"items":[{},[false,null]],"last":true}',
            "many array siblings": "[" + ",".join(str(i) for i in range(1000)) + "]",
            "many object siblings": json.dumps({f"key{i}": i for i in range(1000)}),
        }
        for label, value in extensions.items():
            with self.subTest(case=label):
                raw = self.encode_raw_value("bank", "metadata", value, deepcopy(data))
                raw = raw.replace(json.dumps(text, ensure_ascii=False),
                                  r'"\u7f51\u7edc [ ] { } \" \\ \/ \ud83d\ude80"')
                raw = raw.replace(json.dumps(option, ensure_ascii=False),
                                  r'"\b\f\n\r\t \/ \\ \" \u7f51\u7edc \ud83d\ude80"')
                self.path.write_text(raw, encoding="utf-8")
                bank = load_bank(self.path)
                self.assertEqual(bank, expected)
                self.assertEqual(bank["network"].questions[0].text, text)
                self.assertEqual(bank["network"].questions[0].options["A"], option)

    def test_invalid_json_lexemes_and_trailing_data_are_rejected(self):
        for value in ("+1", "01", "1.", ".1", "1e", "1e+", "1١", "NaN", "Infinity",
                      "\u00a0null", "[true false]", '{"a":1 "b":2}'):
            with self.subTest(extension=value):
                raw = self.encode_raw_value("bank", "metadata", value)
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaises(BankError):
                    load_bank(self.path)
        raw = json.dumps(valid_bank())
        for suffix in ("{}", " true", "\u00a0"):
            with self.subTest(trailing=suffix):
                self.path.write_text(raw + suffix, encoding="utf-8")
                with self.assertRaises(BankError):
                    load_bank(self.path)

    def test_deep_extensions_are_accepted_at_all_schema_levels(self):
        depth = 10_000
        values = {
            "array": "[" * depth + "null" + "]" * depth,
            "object": '{"child":' * depth + "true" + "}" * depth,
            "mixed": '[{"child":' * (depth // 2) + '"终点"' + "}]" * (depth // 2),
        }
        for scope in ("bank", "quiz", "question"):
            for shape, value in values.items():
                with self.subTest(scope=scope, shape=shape):
                    data = valid_bank()
                    second = deepcopy(data["quizzes"][0])
                    second["quiz_id"] = "second"
                    for question in second["questions"]:
                        question["question_id"] += "b"
                    data["quizzes"].append(second)
                    raw = self.encode_raw_value(scope, "metadata", value, data)
                    self.assertLess(len(raw.encode("utf-8")), MAX_BANK_BYTES)
                    self.path.write_text(raw, encoding="utf-8")
                    bank = load_bank(self.path)
                    self.assertEqual(list(bank), ["network", "second"])
                    for quiz_id, suffix in (("network", ""), ("second", "b")):
                        self.assertEqual([q.question_id for q in bank[quiz_id].questions],
                                         [f"q{i}{suffix}" for i in range(5)])
                    self.assertEqual(bank["network"].questions[0].options["A"], "One")
                    self.assertEqual(bank["network"].questions[4].answer, "A")

    def test_deep_extensions_still_require_valid_json(self):
        invalid_values = {
            "array trailing comma": "[0,]",
            "object trailing comma": '{"x":0,}',
            "missing colon": '{"x" 0}',
            "wrong closing bracket": "[0}",
            "invalid escape": r'"\q"',
            "invalid unicode escape": r'"\uZZZZ"',
            "escaped duplicate key": r'{"same":1,"\u0073ame":2}',
            "leading zero": "01",
            "incomplete exponent": "1e+",
            "missing value": '{"x":}',
        }
        for label, value in invalid_values.items():
            with self.subTest(case=label):
                nested = "[" * 3000 + value + "]" * 3000
                raw = self.encode_raw_value("question", "metadata", nested)
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaises(BankError):
                    load_bank(self.path)

    def test_deep_values_cannot_bypass_required_field_types(self):
        value = "[" * 3000 + "null" + "]" * 3000
        for scope, fields in (
            ("bank", ("schema_version", "quizzes")),
            ("quiz", ("quiz_id", "title", "description", "questions")),
            ("question", ("question_id", "text", "options", "answer", "time_limit", "points")),
        ):
            for field in fields:
                with self.subTest(scope=scope, field=field):
                    raw = self.encode_raw_value(scope, field, value)
                    self.path.write_text(raw, encoding="utf-8")
                    with self.assertRaisesRegex(BankError, field):
                        load_bank(self.path)

    def test_deep_extensions_obey_the_exact_file_byte_limit(self):
        nested = "[" * 10_000 + '"汉"' + "]" * 10_000
        raw = self.encode_raw_value("bank", "metadata", nested).encode("utf-8")
        self.assertLess(len(raw), MAX_BANK_BYTES)
        boundary = raw + b" " * (MAX_BANK_BYTES - len(raw))
        self.path.write_bytes(boundary)
        self.assertIn("network", load_bank(self.path))
        self.path.write_bytes(boundary + b" ")
        with self.assertRaisesRegex(BankError, "bytes"):
            load_bank(self.path)

    def test_deep_extensions_do_not_change_the_process_recursion_limit(self):
        before = sys.getrecursionlimit()
        nested = '{"child":' * 10_000 + "null" + "}" * 10_000
        raw = self.encode_raw_value("quiz", "metadata", nested)
        self.path.write_text(raw, encoding="utf-8")
        with patch("sys.setrecursionlimit", side_effect=AssertionError("process setting changed")):
            self.assertIn("network", load_bank(self.path))
        self.assertEqual(sys.getrecursionlimit(), before)

    def test_oversized_integer_tokens_remain_invalid_in_required_fields(self):
        for field in ("schema_version", "time_limit", "points", "text"):
            with self.subTest(field=field):
                data = valid_bank()
                target = data if field == "schema_version" else data["quizzes"][0]["questions"][0]
                target[field] = "oversized_integer"
                raw = json.dumps(data).replace('"oversized_integer"', "9" * 5000)
                self.path.write_text(raw, encoding="utf-8")
                with self.assertRaisesRegex(BankError, field):
                    load_bank(self.path)

    def test_every_required_field_is_required(self):
        for scope, keys in (
            ("bank", ("schema_version", "quizzes")),
            ("quiz", ("quiz_id", "title", "description", "questions")),
            ("question", ("question_id", "text", "options", "answer", "time_limit", "points")),
        ):
            for key in keys:
                with self.subTest(scope=scope, key=key):
                    data = valid_bank()
                    targets = {"bank": data, "quiz": data["quizzes"][0],
                               "question": data["quizzes"][0]["questions"][0]}
                    del targets[scope][key]
                    self.reject(data, key)

    def test_numeric_types_and_bounds_are_strict(self):
        cases = (
            ("schema_version", (True, False, 1.0, "1", 0, 2, None)),
            ("time_limit", (True, 5.0, "5", 4, 61, None)),
            ("points", (True, 1.0, "1", 0, 1001, None)),
        )
        for key, values in cases:
            for value in values:
                with self.subTest(key=key, value=value):
                    data = valid_bank()
                    target = data if key == "schema_version" else data["quizzes"][0]["questions"][0]
                    target[key] = value
                    self.reject(data, key)
        data = valid_bank()
        data["quizzes"][0]["questions"][0].update(time_limit=60, points=1000)
        self.assertEqual(self.load(data)["network"].questions[0].points, 1000)

    def test_duplicate_ids_are_rejected_across_the_entire_bank(self):
        data = valid_bank()
        data["quizzes"].append(deepcopy(data["quizzes"][0]))
        self.reject(data, "quiz_id")
        data["quizzes"][1]["quiz_id"] = "other"
        self.reject(data, "question_id")
        data = valid_bank()
        data["quizzes"][0]["questions"][4]["question_id"] = "q0"
        self.reject(data, "question_id")

    def test_ids_allow_unicode_letters_numbers_but_not_spaces_or_punctuation(self):
        data = valid_bank()
        data["quizzes"][0]["quiz_id"] = "网络_1-甲"
        self.assertIn("网络_1-甲", self.load(data))
        for value in ("", "x" * 33, "a b", "a.b", "a\n", "😀", 5, None):
            with self.subTest(value=value):
                data["quizzes"][0]["quiz_id"] = value
                self.reject(data, "quiz_id")

    def test_text_boundaries_and_printability(self):
        for field, minimum, maximum in (("title", 1, 100), ("description", 0, 300), ("text", 1, 500)):
            for value, valid in (("x" * minimum, True), ("x" * maximum, True),
                                 ("x" * (maximum + 1), False), ("a\tb", False),
                                 ("a\nb", False), ("a\x1bb", False), (None, False)):
                with self.subTest(field=field, length=len(value) if isinstance(value, str) else None):
                    data = valid_bank()
                    target = data["quizzes"][0] if field != "text" else data["quizzes"][0]["questions"][0]
                    target[field] = value
                    if valid:
                        self.load(data)
                    else:
                        self.reject(data, field)
        data = valid_bank()
        data["quizzes"][0]["title"] = " "
        self.reject(data, "title")
        data["quizzes"][0]["title"] = "网络知识"
        self.load(data)

    def test_options_and_answer_shape(self):
        for value in ("", "x" * 201, None, 1):
            with self.subTest(option=value):
                data = valid_bank()
                data["quizzes"][0]["questions"][0]["options"]["A"] = value
                self.reject(data, "options.A")
        for value in ("a", "E", "", ["A"], None, True):
            with self.subTest(answer=value):
                data = valid_bank()
                data["quizzes"][0]["questions"][0]["answer"] = value
                self.reject(data, "answer")
        data = valid_bank()
        del data["quizzes"][0]["questions"][0]["options"]["D"]
        self.reject(data, "options")

    def test_options_allow_whitespace_and_control_characters(self):
        for value in (" ", "\t", "a\nb", "\x1b[31mred", "x" * 200):
            with self.subTest(value=value):
                data = valid_bank()
                data["quizzes"][0]["questions"][0]["options"]["A"] = value
                self.assertEqual(self.load(data)["network"].questions[0].options["A"], value)
        raw = json.dumps(valid_bank()).replace('"One"', '"\\ud800"')
        self.path.write_text(raw, encoding="utf-8")
        with self.assertRaisesRegex(BankError, "surrogate"):
            load_bank(self.path)

    def test_array_count_boundaries(self):
        for count in (0, 4, 5, 50, 51):
            with self.subTest(questions=count):
                data = valid_bank()
                sample = data["quizzes"][0]["questions"][0]
                data["quizzes"][0]["questions"] = [dict(sample, question_id=f"q{i}") for i in range(count)]
                if 5 <= count <= 50:
                    self.load(data)
                else:
                    self.reject(data, "questions")
        for count in (0, 1, 20, 21):
            with self.subTest(quizzes=count):
                data = valid_bank()
                sample = data["quizzes"][0]
                data["quizzes"] = []
                for number in range(count):
                    quiz = deepcopy(sample)
                    quiz["quiz_id"] = f"quiz{number}"
                    for question in quiz["questions"]:
                        question["question_id"] += f"_{number}"
                    data["quizzes"].append(quiz)
                if 1 <= count <= 20:
                    self.load(data)
                else:
                    self.reject(data, "quizzes")

    def test_container_types_are_checked(self):
        for data in ([], None, 1, "bank"):
            with self.subTest(root=data):
                self.reject(data, "bank")
        for field, value in (("quizzes", {}), ("quizzes", [None])):
            data = valid_bank()
            data[field] = value
            self.reject(data, "quizzes")
        for value in ({}, [None] * 5):
            data = valid_bank()
            data["quizzes"][0]["questions"] = value
            self.reject(data, "questions")

    def test_invalid_encoding_json_duplicate_keys_and_nonfinite_numbers(self):
        for raw in (b"", b"\xff", b"{", b'{"schema_version":1,"schema_version":1}',
                    b'{"extra":NaN}', b'{"extra":Infinity}', b"[" * 2000):
            with self.subTest(raw=raw[:30]):
                self.path.write_bytes(raw)
                with self.assertRaises(BankError):
                    load_bank(self.path)

    def test_byte_limit_is_exact_and_counts_utf8_bytes(self):
        data = valid_bank()
        raw = json.dumps(data).encode("utf-8")
        self.path.write_bytes(raw + b" " * (MAX_BANK_BYTES - len(raw)))
        self.assertIn("network", load_bank(self.path))
        self.path.write_bytes(raw + b" " * (MAX_BANK_BYTES - len(raw) + 1))
        with self.assertRaisesRegex(BankError, "bytes"):
            load_bank(self.path)
        data["padding"] = "汉" * (MAX_BANK_BYTES // 2)
        with self.assertRaisesRegex(BankError, "bytes"):
            self.load(data)

    def test_missing_file_and_directory_are_diagnostic(self):
        for path in (self.path, Path(self.directory.name)):
            with self.subTest(path=path), self.assertRaisesRegex(BankError, "cannot read"):
                load_bank(path)

    def test_shipped_bank_meets_submission_minimum(self):
        bank = load_bank(Path(__file__).resolve().parents[1] / "questions.json")
        self.assertGreaterEqual(len(bank), 2)
        self.assertTrue(all(len(quiz.questions) >= 5 for quiz in bank.values()))


if __name__ == "__main__":
    unittest.main()
