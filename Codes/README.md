# Networked Quiz Tournament

A Python 3.12 command-line client and raw TCP server for independent multiplayer quiz rooms. The server validates the question bank, owns the game clock and scores, and releases results only after each question closes. Runtime and tests use only the Python standard library.

**Validation status:** implementation and test sources are provided. The examiner image has not yet been supplied, so coursework execution, automated tests and demonstrations have not been performed. The commands below are operating instructions, not records of successful execution.

## Required environment

Use the examiner-provided Docker image and its exact published tag for all coursework verification and demonstrations. Do not substitute a Lab image or a generic Python image. There is no package-installation step and no Dockerfile. Client and server processes run inside the official image; only the server opens the question-bank file.

From the mounted `Codes` directory, the required entry points are:

```text
python3 server.py --host 127.0.0.1 --port 5000 --questions questions.json
python3 client.py --server 127.0.0.1 --port 5000 --id alice --name Alice
python3 client.py --server 127.0.0.1 --port 5000 --id bob --name Bob
```

Run each process in a separate terminal. All three processes may share one container and its loopback interface. Both programs accept ports in 1..65535. The server prints `SERVER_READY host=... port=...` only after the complete bank is valid and listening succeeds. Invalid arguments or startup failures return a nonzero status and write a diagnostic to stderr. Paths are relative to the working directory or passed through `--questions`.

### Windows Docker template

This template must be checked against the official image's instructions when its identity is known. It deliberately refuses to use the placeholder image. The container's idle Python process only keeps its execution environment available for `docker exec`.

```powershell
$ExaminerImage = 'REPLACE_WITH_OFFICIAL_IMAGE_AND_TAG'
if ($ExaminerImage -eq 'REPLACE_WITH_OFFICIAL_IMAGE_AND_TAG') {
    throw 'Set the exact examiner-provided image name and tag first.'
}
docker run --rm -dit --name csi201-cw `
    --mount 'type=bind,source=E:\大三\CSI201\coursework1\Codes,target=/code' `
    --workdir /code --entrypoint python3 $ExaminerImage `
    -c 'import time; time.sleep(86400)'
docker exec csi201-cw python3 --version
```

Then use separate PowerShell terminals:

```powershell
docker exec -it csi201-cw python3 server.py --host 127.0.0.1 --port 5000 --questions questions.json
docker exec -it csi201-cw python3 client.py --server 127.0.0.1 --port 5000 --id alice --name Alice
docker exec -it csi201-cw python3 client.py --server 127.0.0.1 --port 5000 --id bob --name Bob
```

Docker does not need a published host port for processes in the same container. Different containers require a shared Docker network, a server bound to an accessible address, and its reachable hostname; `127.0.0.1` always refers to the current container. No external Internet service is needed by the submitted system.

## Client commands

The client logs in using its command-line identity before accepting game commands. Commands begin with `/`. Output is plain text, without terminal cursor control, and server updates continue while input is idle.

| Command | Meaning |
|---|---|
| `/help` | Show command help |
| `/quizzes` | List quiz IDs, titles, descriptions and question counts |
| `/rooms` | List waiting rooms with available places |
| `/create <quiz_id>` | Create and join a room as its host |
| `/join <room_id>` | Join a waiting room |
| `/players` | Inspect members, host and readiness |
| `/ready` / `/unready` | Change your readiness before the quiz starts |
| `/start` | Host requests the start of the quiz |
| `/answer <A|B|C|D> [question_id]` | Answer the displayed question, or specify a question ID explicitly |
| `/score` | Show scores from closed questions |
| `/leave` | Leave the room and return to the lobby |
| `/quit` | Log out and close the client |

For example, Alice uses `/quizzes` and `/create network_fundamentals`. Bob uses `/rooms` and `/join R0001` (use the actual returned room ID). Both use `/ready`; Alice uses `/start`. Each player can submit an answer and inspect `/score`. The server displays results after closure and the final leaderboard after the last question.

For `/answer A`, the client captures the displayed question ID when the input line is completed. A queued answer is never silently rebound to a later question. `/answer A net_01` explicitly addresses `net_01`, allowing a stale-answer case to be demonstrated. An acceptance message confirms receipt, not correctness.

## Game rules

* User IDs contain 1..32 ASCII letters, digits, underscores or hyphens. Display names contain 1..64 printable Unicode characters, without leading or trailing whitespace. Active IDs and names are unique and case-sensitive. A failed login does not reserve either identity; disconnection releases both.
* A user belongs to at most one room. Rooms hold 2..32 players at start. Only the host can start, and everyone, including the host, must be ready. Joining and leaving preserve other members' readiness.
* The earliest remaining member becomes host when the host leaves. Empty rooms are removed. Running and finished rooms cannot be joined. A finished room is retained until its members leave; a new match needs a new room.
* Quizzes and questions follow file order. Every question remains open for its full `time_limit`, followed by a two-second intermission except after the last question. A delayed event loop never shortens a newly opened question.
* Server monotonic time at request dispatch is authoritative. An answer at or after the deadline is late. There is no client timestamp or network-latency compensation.
* The first valid answer per player and question is accepted. Invalid choices do not consume the attempt. Repeated or stale answers cannot change an accepted answer or add points.
* At closure, a correct accepted answer earns exactly the question's `points`; wrong, missing or late answers earn zero. Open-question scores do not change. No bonus or penalty is applied.
* Players who leave during a match retain earned scores and any answer already accepted for the current question. Their later questions receive zero; they appear as inactive on that room's leaderboard. They cannot resume the match by reconnecting.
* Scores sort descending, with user ID ascending as the deterministic secondary order. Ties use competition ranks (1, 1, 3); all rank-one contestants are listed as winners, including inactive contestants.

## Question bank

`questions.json` contains two quizzes with five questions each. It is read only by the server. Valid replacement files may contain 1..20 quizzes, each with 5..50 questions; the submission's own bank must contain at least two quizzes.

The complete input must be a nonempty UTF-8 JSON object of at most 1,000,000 bytes. The `schema_version` must be integer `1`; `quiz_id`, `title`, `description` and `questions` are required for every quiz. Every question requires `question_id`, `text`, `options`, `answer`, `time_limit` and `points`. IDs are unique as prescribed, including question IDs across all quizzes. `options` has exactly A/B/C/D. Times are integers 5..60; points are integers 1..1000. Booleans are not accepted as integers. Extra object fields are ignored except inside `options`.

| Field | JSON type | Required constraint |
|---|---|---|
| `schema_version` | integer | Exactly 1 |
| `quizzes` | array | 1..20 quiz objects |
| `quiz_id` | string | 1..32 letters/digits/underscore/hyphen; unique among quizzes |
| `title` | string | 1..100 visible characters |
| `description` | string | 0..300 visible characters; field required even when empty |
| `questions` | array | 5..50 question objects per quiz |
| `question_id` | string | 1..32 characters with the quiz-ID character set; unique across the file |
| `text` | string | 1..500 visible characters |
| `options` | object | Exactly A/B/C/D; each value a string of 1..200 characters |
| `answer` | string | Exactly A, B, C or D |
| `time_limit` | integer | 5..60 seconds |
| `points` | integer | 1..1000 points |

Any invalid required field rejects the entire bank before listening. Large integer values in ignored extension fields do not trigger Python's integer-conversion digit limit. `bank_json.py` parses containers with an explicit stack, so deeply nested extension objects and arrays do not depend on Python's recursion limit. Every extension still undergoes JSON syntax and duplicate-key validation. The file's 1,000,000-byte limit bounds the input, and the loader does not change process recursion settings.

Interpretations awaiting any course clarification: 1 MB means 1,000,000 bytes; bank ID letters/numbers include Unicode alphanumeric characters; visible text allows printable Unicode including spaces, but required nonempty visible text must contain a non-whitespace character. Options follow their separate string-length rule. Duplicate JSON keys are rejected as ambiguous input. See `question_bank.py` for field-level diagnostics.

## Protocol and implementation

Messages use a four-byte network-order payload length followed by one UTF-8 JSON object, limited to 65,536 payload bytes. Each connection has its own decoder and output buffer. Requests, responses, events and errors have distinct `kind` values. Request IDs increase strictly per connection; events carry a global increasing ID, so gaps are normal. Identity is bound to the connection. This is the team's own protocol, inspired by the informative appendix rather than a claim of exact appendix conformance.

The server uses nonblocking sockets and `selectors`. All game state belongs to one event loop; no game-state locks or worker threads are required. Bounded work, buffered writes and connection-specific limits prevent one stalled peer from blocking another room. The transport performs timer updates before request dispatch.

| Module | Responsibility |
|---|---|
| `server.py` | Arguments, listening, connection lifecycle, nonblocking I/O, timer scheduling |
| `client.py` | Terminal commands, concurrent input and network updates, rendering and exit |
| `protocol.py` | Length-prefixed framing, strict JSON decoding, request envelopes |
| `question_bank.py` | Bounded file read, complete schema validation, immutable quiz data |
| `bank_json.py` | Iterative bank JSON container parsing, standard-library string decoding |
| `game.py` | Identity, rooms, readiness, question lifecycle, scores and events |
| `tests/` | Unit, real TCP integration and submitted-client workflow tests |

## Verification commands

Run only inside the examiner image, from `/code`:

```text
python3 -m unittest discover -s tests -v
```

For selected suites:

```text
python3 -m unittest tests.test_question_bank tests.test_protocol tests.test_game tests.test_client tests.test_transport -v
python3 -m unittest tests.test_server_integration tests.test_client_workflow -v
```

Integration tests use legal question limits of at least five seconds and real subprocesses/sockets. Full-match tests take at least 33 seconds (five questions plus four intermissions); the full suite takes longer. Expected results are assertions, not claims of successful execution. Record image identity, Python version, commands, stdout/stderr, exit status and actual results when the official environment is available.

## Shutdown and limitations

Use `/quit` or Ctrl+C in a client. Input EOF also closes the client. Ctrl+C in the server releases its listener and connected sockets and returns success. Clients report a server disconnect and exit. After stopping coursework processes, `docker stop csi201-cw` removes the temporary container without removing the bind-mounted source files.

Startup/argument diagnostics go to stderr. The client's interactive responses, events and runtime error messages go to stdout; the application creates no log files. Capture both streams externally when preparing evidence. Do not publish full banks or unclosed answers through client-visible logs.

The server supports at most 128 connections and 64 rooms. Login must complete within 30 seconds. An incomplete frame times out after 10 seconds; an output queue is limited to 1 MiB, with a 15-second write-stall timeout. Graceful closing has a two-second flush limit. Idle authenticated players are allowed; they score zero on unanswered questions.

This classroom system has no encryption, persistent accounts, passwords, durable scores, reconnection recovery or tournament restart. It assumes a trusted teaching network: the server owns the authoritative state, but raw TCP does not protect traffic from network observers. Resource limits bound individual connections; they are not protection against a distributed denial-of-service attack. Official-image compatibility and timing/concurrency results remain unverified until the required execution takes place.
