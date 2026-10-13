# Verification plan and execution record

**Status: NOT_RUN — official examiner Docker image not yet available.**

This document describes authored tests and their intended assertions. It does not
claim that the implementation, tests, Python compatibility or container behaviour
have passed verification. Source inspection is distinct from execution evidence.

The current source inventory contains 108 test methods across seven test modules,
including parameterized subcases. This is a source count, not an executed-test
count or a coverage measurement.

## Execution prerequisites

1. Obtain the exact official image reference/tag and the course launch instructions.
2. Record the image ID/digest, host architecture, Docker version and container
   `python3 --version`. The assessed runtime must be Python 3.12.
3. Mount `Codes` into the official container and run from `/code`.
   No `pip install`, package manager, external service or internet access is needed.
4. Preserve the complete command, standard output, standard error, exit status,
   timestamp and source revision with the test record. Fix defects and record the
   subsequent run separately; never replace an unsuccessful run with invented data.

The commands below are to be executed **inside the official container**:

```sh
# Validation, protocol, game/client state and mocked transport tests.
python3 -m unittest tests.test_question_bank tests.test_protocol tests.test_game tests.test_client tests.test_transport -v

# Real server processes with independent raw-TCP peers.
python3 -m unittest tests.test_server_integration -v

# Submitted client.py and server.py as separate processes, including ten clients.
python3 -m unittest tests.test_client_workflow -v

# Complete suite, used for the recorded acceptance run.
python3 -m unittest discover -s tests -v
```

The discovery command is the authoritative full-suite entry point.

This PowerShell launch template must be checked against the official image's
instructions. It refuses to use the placeholder image:

```powershell
$ExaminerImage = 'REPLACE_WITH_OFFICIAL_IMAGE_AND_TAG'
if ($ExaminerImage -eq 'REPLACE_WITH_OFFICIAL_IMAGE_AND_TAG') {
    throw 'Set the exact examiner-provided image name and tag first.'
}
docker run --rm -it --network none `
    --mount 'type=bind,source=E:\大三\CSI201\coursework1\Codes,target=/code' `
    --workdir /code --entrypoint python3 $ExaminerImage `
    -m unittest discover -s tests -v
```

All automated network tests use loopback within one container, so published host
ports and internet access are unnecessary. Loopback remains available with
`--network none`. If the official image has a custom entrypoint, follow its launch
instructions instead of assuming this template applies.

## Layered coverage

| Layer | Location | Intended coverage | Execution status |
|---|---|---|---|
| Bank validation | `Codes/tests/test_question_bank.py` | Schema, boundaries, duplicate IDs, Unicode/escapes, large integer extensions, 10,000-level containers, malformed deep JSON, strict JSON lexemes, exact byte cap and unchanged recursion settings | NOT_RUN |
| Wire codec | `Codes/tests/test_protocol.py` | Framing, incremental decoding, semantic envelopes and malformed input | NOT_RUN |
| Game rules | `Codes/tests/test_game.py` | Deterministic state transitions, scoring, time boundaries and room membership | NOT_RUN |
| Client helpers | `Codes/tests/test_client.py` | Command syntax, client-side protocol/state handling, timeout checks around readiness waits and address fallback | NOT_RUN |
| Server transport | `Codes/tests/test_transport.py` | Partial writes, bounded output, read/write/close timeout boundaries, failed peer setup, buffered-request fairness and address fallback using mocked I/O | NOT_RUN |
| Server process and raw TCP | `Codes/tests/test_server_integration.py` | Real startup, sockets, scheduling, connection failures and cleanup | NOT_RUN |
| Paired submitted programs | `Codes/tests/test_client_workflow.py` | Actual CLI processes, asynchronous display and ten-client/two-room completion | NOT_RUN |

Unit-test coverage is specified in each module. The integration cases below are
black-box checks through the documented CLI and wire protocol. Their framing
helper deliberately uses its own `struct`/`json` implementation rather than
importing the application's encoder/decoder.

The deep-bank cases build nested JSON text directly, so the test fixture does not
itself depend on recursive serialization. They exercise `load_bank`, including
the file-size guard, iterative JSON parser and required-field validation. Array,
object and mixed extensions cover bank/quiz/question scopes; malformed deep
values must raise `BankError`. Additional cases check escaped required strings,
wide collections and strict numbers, whitespace, duplicate keys and trailing
data. No test changes the process recursion limit. These are authored assertions,
not execution results.

## Real-server integration cases

| ID | Test method | Required observation | Status |
|---|---|---|---|
| SI-01 | `test_invalid_external_banks_fail_before_listening` | Empty, malformed JSON, malformed UTF-8, boolean version, cross-quiz duplicate question ID, missing description and oversized file all exit nonzero, explain the error, never announce `SERVER_READY`, and do not listen | NOT_RUN |
| SI-02 | `test_single_quiz_external_bank_and_public_catalogue` | A legal external single-quiz bank with Unicode and permitted extensions starts; catalogue exposes metadata only; the selected Unicode quiz can be used to create a room | NOT_RUN |
| SI-03 | `test_fragmented_coalesced_requests_and_recoverable_errors` | A split frame and multiple frames in one write decode correctly; 80 coalesced requests exceed the per-turn budget and all receive replies without further client writes; unauthenticated/unsupported actions, repeated request IDs, wrong version and invalid payload fail recoverably; subsequent commands and graceful quit succeed | NOT_RUN |
| SI-04 | `test_malformed_and_slow_peers_are_isolated` | Invalid lengths, UTF-8, JSON and duplicate keys close only that peer; an unfinished header times out; an independent authenticated peer remains usable | NOT_RUN |
| SI-05 | `test_identity_room_rules_and_host_disconnect` | Duplicate ID/name rejected, minimum player count and host/all-ready rules enforced, existing readiness preserved, host transferred on abrupt disconnect, running room rejects joins, empty room removed and departed identity released | NOT_RUN |
| SI-06 | `test_two_complete_rooms_authoritative_scoring_and_confidentiality` | Two rooms complete five questions each with independent 5/6-second first deadlines; real point values used; invalid, duplicate, stale, late, wrong and missing answers handled; no early correctness or future-question disclosure; final ties/ranks/winners correct; every event belongs to the recipient's room | NOT_RUN |
| SI-07 | `test_mid_question_disconnect_retains_an_accepted_answer` | A departed player's accepted answer still earns the correct points; retained leaderboard row is inactive; remaining player receives results and can leave | NOT_RUN |
| SI-08 | `test_interrupt_exits_cleanly_and_releases_port` | SIGINT with an active peer exits zero without a traceback; sockets close; a new server binds the same port and accepts a new session | NOT_RUN |
| SI-09 | `test_missing_bank_invalid_ports_and_occupied_port_fail_cleanly` | A missing bank, ports outside 1–65535 and an already listening port all produce a diagnostic and nonzero exit without `SERVER_READY` or a traceback | NOT_RUN |

SI-08 targets the official Linux container and is skipped on Windows. A skipped
case is not a pass. SI-06 checks each room's elapsed duration from before sending
its own `start`; the precise `now == deadline` rule is additionally tested by the
deterministic game tests rather than attempting an unreliable network race.

## Submitted-client workflow cases

| ID | Test method | Required observation | Status |
|---|---|---|---|
| CW-01 | `test_ten_actual_clients_complete_two_isolated_quizzes` | Ten simultaneously connected `client.py` processes form two five-player rooms, ready, start, answer all five questions and receive correct final scores; each client sees only its room's questions; all remain alive until `/quit`; rooms disappear afterward | NOT_RUN |
| CW-02 | `test_idle_input_still_receives_events_and_eof_quits` | Client receives question/results while stdin is idle; stdin EOF initiates orderly departure; identity is reusable | NOT_RUN |
| CW-03 | `test_connection_failure_exits_with_readable_error` | An unavailable local port produces a readable error, nonzero exit and no traceback/hang | NOT_RUN |
| CW-04 | `test_server_disappearance_does_not_leave_client_hanging` | Terminating the server is detected without further user input; client reports an error and exits | NOT_RUN |
| CW-05 | `test_actual_clients_recover_from_commands_and_answer_errors` | Two submitted clients demonstrate catalogue/rooms/players, invalid commands, host-only start, ready/unready, correct/wrong/duplicate/late answers, hidden open-question scores, leave and quit; rejected input does not prevent later valid actions | NOT_RUN |
| CW-06 | `test_client_interrupt_releases_identity_and_transfers_host` | SIGINT in a submitted client exits cleanly, transfers host ownership, releases its identity and leaves the server usable | NOT_RUN |

CW-06 targets the official Linux container and is skipped on Windows. Actual
terminal Ctrl+C remains a manual check in addition to process-level SIGINT.

The CLI tests keep client working directories separate from the server's external
question-bank file. Clients are launched with the prescribed arguments only;
question data and room IDs come from the documented protocol/output. stdout and
stderr are drained in background threads, with bounded waits and diagnostic
output on failure. Cleanup terminates remaining test-owned processes and removes
temporary directories even after a failure.

Expected elapsed time for the complete suite is approximately **100–165 seconds**
on a normal local container. This is an estimate, not a measured result. Most time
is the two complete tournaments (33 and 34 seconds), a 10-second partial-frame
timeout, two first-question result waits and the ten-second CLI error-recovery
question. Run the suite sequentially; it
already creates concurrent clients internally. Each case requests an ephemeral
loopback port, with a small unavoidable bind-after-probe race; a collision will
surface as a startup diagnostic rather than an indefinite wait.

## Manual acceptance and evidence still required

Automated checks do not replace the following official-environment checks:

- Launch the exact required server/client commands interactively in separate
  terminals, including a bad path, invalid port and occupied server port.
- Demonstrate quiz catalogue, room discovery, member listing, ready/unready,
  host-only start, valid answer, duplicate/late answer, score, leave and quit.
- Verify readable real-terminal behaviour when a question arrives during typed
  input; check normal Ctrl+C in both programs and reusing the listening port.
- Record visible evidence of at least ten connected submitted clients and two
  independent rooms. The automated subprocess test supplies functional coverage;
  the video still needs a clear, human-readable demonstration.
- Exercise nonreading-client/write-backpressure handling, the 30-second login
  timeout and declared connection/room/player caps. These defensive limits need
  an additional controlled stress run; do not imply that the default suite
  measures throughput or provides a denial-of-service guarantee.
- Review the bundled question bank's factual answers and the final submission's
  minimum quiz/question counts; validate permitted Python imports and CLI-only
  implementation during source review.
- Unpack the final submission into a clean directory, repeat the complete suite
  and interactive launch in the exact image, and record results against the
  packaged source rather than only the working copy.

## Result record template

| Field | Value |
|---|---|
| Status | NOT_RUN |
| Date/time and timezone | Pending |
| Official image name/tag and image ID/digest | Pending |
| Host / Docker / container Python versions | Pending |
| Source revision or submission archive hash | Pending |
| Exact command | Pending |
| Expected result | See the case above |
| Actual result and exit code | Not observed |
| stdout/stderr evidence path | Pending |
| Defect and retest reference | Pending |

Do not mark a case PASS until its assertion/observation has actually been checked
in the official environment. Keep failures, skips and environment limitations
visible in the execution record.
