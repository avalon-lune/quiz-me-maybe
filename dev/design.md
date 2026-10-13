# Software development record

Version 0.3, 13 October 2026. This working record describes the implemented design and planned verification. The examiner image, official execution evidence and team details are pending. It is source material for the final Development Documentation, not a submission-ready PDF.

## 1 Document control and executive summary

The product is a Python 3.12 raw TCP quiz tournament with separate command-line client and server programs, a validated local question bank and repeatable standard-library tests. The design supports multiple independent rooms and a documented lifecycle from login through final ranking.

Current outcome: source code, sample bank, tests and operating/design documentation have been authored and reviewed as text. No coursework program, unit test or integration test has been executed because the official image is unavailable. Windows Docker installation smoke checks are environment preparation only and are not coursework evidence.

Team name, member names, student IDs and contribution percentages must be supplied and verified by the team. Version 0.1 records the initial implementation; version 0.2 records a source-only review against the school materials, timeout/connection/extension-field fixes and stronger paired-client test sources. Version 0.3 removes the bank parser's container recursion dependency and adds deep-input regression sources. Details are in `静态复查记录.md`. No earlier commit history or test results are implied.

## 2 Requirements analysis

Stakeholders are players, team maintainers and the examiner. Players need understandable commands and timely results. Maintainers need explicit state ownership and reproducible cases. The examiner needs exact startup interfaces, replacement-bank support and behavior observable through the submitted client.

Constraints are CLI-only interaction, Python 3.12 standard-library modules, direct socket networking, no installation step, server-only bank access and the official examiner Docker environment for verification. `requirements.md` maps requirements to source modules and planned checks. Stable identifiers are retained when a requirement or test is expanded.

Assumptions requiring course clarification are the decimal interpretation of 1 MB, Unicode identifier/text boundaries, and any additional report/video submission instructions. Team-chosen rules include a two-player minimum, full-duration questions, two-second intermissions, host reassignment and retaining departed contestants' scores. These are documented choices, not claims of extra school mandates.

## 3 System architecture

Every player runs one `client.py` process. Each process maintains one persistent TCP connection to one `server.py` process. Only that server opens `questions.json`, through `question_bank.py`. The server owns all authenticated sessions, room membership, question deadlines, accepted answers and scores. Clients submit intentions and render the server's responses/events.

The deployment baseline is all processes in one official container with a bind-mounted `Codes` directory and loopback TCP. Multiple containers are optional and require suitable addressing; room isolation does not require separate containers or virtual machines. Host Windows, Docker Desktop and the source editor are outside the submitted application.

The main trust boundaries are the untrusted bank file at startup, the network stream, and the terminal input stream. Bank validation finishes before binding. Network bytes pass framing and envelope validation before domain dispatch. Terminal output escapes control characters, including those valid in bank option strings, rather than interpreting terminal sequences.

## 4 Detailed software design

`protocol.py` implements byte framing without game knowledge. `bank_json.py` decodes question-bank JSON with an explicit container stack, and `question_bank.py` validates the schema and constructs immutable question/quiz objects. `game.py` is a deterministic state machine that receives monotonic timestamps and returns response payloads plus recipient-specific event snapshots. `server.py` performs socket I/O and scheduling. `client.py` owns terminal parsing, server-message validation and rendering.

The server selector loop is the sole owner of game mutations. No worker can alter a room concurrently. Checking readiness and switching to running is one synchronous transition. Checking deadline, detecting a duplicate answer and storing the accepted answer also form one transition. Explicit ownership avoids a global lock around a blocking socket operation.

The server's outer loop handles due timers and connection expiry, rotates buffered request work, waits for readiness, then performs bounded accept/read/write operations. Timers are checked again before each request dispatch. Writes never use blocking `sendall`; a short send leaves the remaining suffix queued. Requests waiting in a decoder are processed without requiring another network packet.

The client has a bounded stdin queue filled by a daemon reader. A separate main loop handles nonblocking network traffic, pending replies and queued commands. Its input reader captures the displayed question ID for implicit answers. Main-loop shutdown does not join a thread blocked on terminal input. DNS lookup uses a bounded daemon resolution path, and connect attempts share one deadline.

Rooms move from waiting to running to finished. Running rooms alternate an open question deadline and an intermission deadline. A question closes at most once. A room with no members is removed, making all its deadlines unreachable. After departure, the contestant record can remain for final ranking while the live membership has been removed.

## 5 Data design

The question bank preserves JSON array order. Frozen `Quiz` and `Question` records contain tuples and read-only option mappings. Quiz IDs are unique; question IDs are unique across the entire bank. The loader bounds the read to one byte beyond the file limit to detect oversize input even if the file changes during reading. Any invalid required field rejects the entire file.

The complete field/type/range table is maintained under Question bank in `Codes/README.md` and must be included in this section of the final PDF. Integer tokens beyond the required fields' possible ranges are kept opaque during parsing, so an ignored extension cannot cause rejection through Python's large-integer conversion limit.

The bank JSON parser uses a stack of object/array states rather than recursive container decoding. It checks JSON whitespace, numbers, separators, closing delimiters and decoded duplicate keys. Strings alone use the public standard-library JSON decoder. Even ignored extensions must be syntactically valid. Container depth does not consume Python call-stack depth or require changing process recursion settings; parser storage grows with the size-bounded input. The schema validator subsequently visits only the prescribed, shallow bank structure. Ten-thousand-level extension cases and malformed deep inputs have authored regression tests; they remain NOT_RUN.

Sessions link connection identity to an optional user and room. A room has a quiz reference, host session, insertion-ordered current members, fixed match-start contestants, question index, deadline/intermission time and accepted-answer map. Contestants are keyed by the original session, so reusing a disconnected user's public ID in another session cannot overwrite their previous match record.

Room and score output is built from fresh dictionaries/lists. Events retain snapshots, not references to a live member list. Only current room members receive room events. Opening a question constructs a public-field whitelist, and acceptance responses contain no correctness flag. Scores change only at closure, preventing `/score` from revealing an open answer.

## 6 Application layer protocol design

`protocol.md` defines framing, envelopes, every action/event, error categories, field meanings and event ordering. The four-byte network-order length is followed by bounded UTF-8 JSON. Request IDs increase per connection; server event IDs increase globally. Responses and asynchronous events are distinguishable without assuming one outstanding request.

Typical interaction is login, catalogue, create/join, readiness, host start, per-question open/answer/close, final ranking, leave and quit. The response to `answer` acknowledges acceptance only. At question closure, the server emits each player's own result and the shared ordered leaderboard. The final result additionally carries all tied winners.

Framing/decoding failures close the affected connection because continuing is not promised safe. Semantic request errors produce a reply and preserve the session. The protocol binds identity to the socket rather than trusting an ID supplied with each action.

## 7 Implementation record

The project is maintained under `E:\大三\CSI201\coursework1`; original school materials remain in `source1`. Docker Desktop 4.95.0 uses the WSL 2 backend, with its program and data under `E:\Docker`. Existing Ubuntu 24.04 was able to execute in WSL 2 during environment preparation. None of this identifies the school's official image.

Key algorithms are bounded stream decoding, insertion-ordered host reassignment, first-valid-answer acceptance, monotonic deadline scheduling and stable competition ranking. The important trade-off is a single-process, single-owner game engine: it simplifies correctness for classroom concurrency while foregoing distributed scale and persistent recovery.

Source review identified several edge cases during authoring: option values have a weaker text rule than question titles and must not be overvalidated; buffered complete frames must be drained before accepting unlimited additional bytes; a socket-creation failure for one address family must allow the next resolved address; and expired connections must be checked before dispatching newly readable or already buffered requests. These were addressed in the source and authored tests. They are review findings, not observed runtime test failures.

## 8 Deployment and operation

`Codes/README.md` contains the required launch commands, a clearly marked official-image Docker template, CLI command table, shutdown steps and limits. The server must print `SERVER_READY` only after validation and listening. The supplied port is never hard-coded. The sample operating port is merely an example.

Final deployment records must capture the image reference, immutable image ID/digest, architecture, Python version, mount path, work directory and commands actually used. There is no package download, package installation, database or external service requirement in the application. It writes no runtime log files; stdout/stderr can be captured by the operator.

## 9 Verification and validation

`test-plan.md` describes standard-library unit tests, independent wire peers, actual server subprocesses and actual submitted CLI subprocesses. `requirements.md` connects these activities to requirements. Unit tests supply explicit clock values to the game engine. Integration tests use legal limits of at least five seconds. Paired-client coverage includes rejected commands, answer errors, readiness changes and process-level SIGINT as well as two complete concurrent rooms.

All current execution statuses are NOT_RUN. Expected assertions are not actual results. Once the official image is supplied, run the full suite and record failures and fixes, then perform the manual CLI/video checklist. An independent unpacked submission must be run using only its own contents. Selected real terminal evidence should support, rather than replace, result tables and explanations.

## 10 Reliability and security

Admission is limited to 128 connections and 64 rooms, with at most 32 members per room. Per-connection framing, output and time budgets isolate malformed and slow clients. Session removal updates membership and host ownership; repeating disconnection is harmless. Client-input and output queues are bounded. An unauthenticated peer cannot create or join rooms.

No password authentication or encryption is offered; IDs are temporary session identities in a teaching network. Network observers can inspect plaintext, and a malicious participant can reserve unused identities. There is no durable game recovery after a server crash. These limitations are explicit and should not be presented as production security.

Current question answers are kept in the server's bank and scoring state. CLI output escapes terminal control characters. Routine error messages do not include complete request objects, entire banks, or unopened answers. The user can inspect server-side source/bank files as the trusted operator; player clients never open them.

## 11 Project management and contribution

Milestones are requirements/protocol, bank validation, paired connectivity, room lifecycle, complete play, concurrency/failure handling, official verification, then report/video/package completion. Source and test files have been authored in parallel against a shared contract; all interface changes require cross-module review.

The repository begins with actual files from this work, without invented historical commits. The team must record real reviews, modifications, tests and report contributions. Member names, student IDs and percentages are pending; percentages must sum to 100 and match verifiable work. Every member must be able to explain the complete architecture and key correctness decisions, not only a single assigned file.

AI assistance disclosure for team review: OpenAI Codex was used to analyze the supplied requirements and author source code, test code and documentation drafts. The team must critically review and verify these materials, record any corrections and use the course's required disclosure/reference format. No official test outcomes, screenshots, member contributions or execution evidence have been generated or claimed here. This disclosure belongs in documentation, not source comments.

## 12 Limitations future work and references

Official-image verification remains outstanding. The exact Docker operating instructions may require adjustment after reading the examiner image's published instructions. The current design does not support reconnection, persistent scores, restarting a finished room, encrypted transport, accounts or a GUI. Possible future work should follow measured needs, and must not displace the required CLI workflow.

The final PDF needs team information, actual test/evidence tables, final image commands and conventional context/component/deployment views, room-state models and protocol sequence diagrams. The current prose does not replace those required design views. The school prohibits generative visual evidence; this draft does not create submission diagrams or screenshots. Format the final document as A4 single-column, 11 pt body text, numbered sections and pages, consistent captions and IEEE references. Recommended body length is 12–18 pages, excluding cover, contents, references and appendices.

Reference inputs for final citation formatting:

1. CSI201, Coursework I Specification, Networked Quiz Tournament, supplied 12-page PDF, 2026.
2. CAN201, Networked Quiz Tournament Application Layer Protocol Reference, Appendix A, supplied 7-page informative PDF, 2026.
3. Python Software Foundation, Python 3.12 Standard Library documentation: socket, selectors, json, time, threading, queue and unittest.
4. Docker Inc., Docker Desktop Windows installation and WSL 2 backend documentation, consulted during local environment preparation, 13 October 2026.
5. OpenAI, Codex, software-development assistance, 13 October 2026. Complete the institution's required acknowledgement format before submission.
