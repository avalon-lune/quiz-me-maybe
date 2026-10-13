# Requirements traceability

Execution status for every row is NOT_RUN pending the official examiner Docker image. Source correspondence is a review map, not a claim that behavior has passed tests. Test methods are discoverable within the modules below; `test-plan.md` supplies the integration case details.

| ID | Requirement and acceptance criterion | Design or source | Verification activity |
|---|---|---|---|
| ENV-01 | Python 3.12 standard library, raw socket core, no installation step | All Codes modules; server/client raw sockets | Official-image import/startup; source dependency review |
| ENV-02 | CLI only; basic stdin/stdout/stderr usable | client.py; server.py | test_client_workflow; manual plain-terminal demonstration |
| ENV-03 | Official image used for all assessed execution/evidence | README deployment template | Record exact image/Python and execute all checks there |
| BOOT-01 | Required server/client arguments; user-selected port | Both main functions | test_server_integration; test_client_workflow |
| BOOT-02 | Entire bank valid before listening; SERVER_READY only after listen | load_bank then open_listener then run | Invalid-bank startup, hidden external bank and port checks |
| BANK-01 | UTF-8 object, nonempty, at most 1 MB, version integer1 | question_bank.py | test_question_bank; invalid external bank integration |
| BANK-02 | All fields/types/counts/ranges checked; bool not integer | question_bank.py | test_question_bank boundary/missing/type cases |
| BANK-03 | Quiz IDs unique and question IDs globally unique | load_bank | test_question_bank; duplicate hidden-bank integration |
| BANK-04 | Extra fields accepted, options exactly A/B/C/D | _question; load_bank; iterative bank_json parser; bounded integer conversion | test_question_bank including large integers and 10,000-level extensions; deep malformed JSON, field types, exact byte cap and recursion-setting checks; external bank with extensions |
| BANK-05 | Hidden bank with one quiz accepted; bundled bank two or more | questions.json; loader cardinality | test_question_bank; single-quiz integration |
| DATA-01 | Clients obtain all quiz/question data via server | Client has no bank file API | Actual clients in working directory without questions.json |
| DATA-02 | File quiz/question order retained | Ordered bank mapping; sequential question index | test_game; two full external-bank tournaments |
| ID-01 | Active user IDs and names unique; conflicts deterministic | Game._login | test_game; identity/room integration |
| ID-02 | Disconnect releases identity and membership | Game.disconnect; server._drop | test_game; EOF/host-disconnect/relogin integration |
| ROOM-01 | Catalogue/create/list/join/participants/ready/leave/start | Game action dispatch; client commands | test_game; real socket and CLI workflows |
| ROOM-02 | Host/minimum/all-ready conditions enforced | Game._start | test_game; identity/room integration |
| ROOM-03 | Host handoff and empty-room cleanup | Game._leave | test_game; before/during-match disconnect cases |
| PLAY-01 | Server controls question order and time | Game.tick; server timer scheduling | test_game exact boundaries; real full-duration games |
| PLAY-02 | At most one accepted answer; invalid/stale/late rejected | Game._answer | test_game; raw peer cases; submitted-client answer/error workflow CW-05 |
| PLAY-03 | Correct accepted answer gets exact points; score once | Game._close_question | test_game; nonuniform points in external test banks |
| PLAY-04 | Final ordered leaderboard and tie policy | Game._leaderboard | test_game; two-room and ten-client final rankings |
| SECRET-01 | Catalogue does not expose questions/options/answers | Game._quizzes | Catalogue confidentiality integration |
| SECRET-02 | No correctness or score disclosure before closure | _open_question; _answer; _score | test_game; open-question message/score assertions |
| CONC-01 | At least 10 clients remain connected | 128-connection server; independent clients | Ten actual client.py subprocesses complete two matches |
| CONC-02 | At least 2 independent rooms; no cross-room events/timers/scores | Per-room state and recipient lists | test_game; staggered/different-duration room integration |
| TCP-01 | Fragmentation/coalescing and partial writes handled | FrameDecoder; server/client queues | test_protocol; test_transport; raw fragmentation and coalesced burst integration |
| TCP-02 | Responses, pushes and errors distinguishable | Protocol kind/IDs; client matching | test_protocol; test_client; independent wire-peer assertions |
| FAIL-01 | Malformed messages and invalid actions isolated | Transport/domain error boundary | test_protocol; malformed-peer integration |
| FAIL-02 | Slow/partial peers cannot permanently block server | Nonblocking selector, frame/output/time caps | test_transport; partial-frame timeout with healthy peer; manual stalled reader |
| FAIL-03 | Missing answers do not block room progress | Per-question deadlines | test_game; idle client still receives results |
| EXIT-01 | Ctrl+C releases listener/connections | Server finally; client graceful quit | Server SIGINT and same-port restart; client SIGINT CW-06; manual terminal Ctrl+C |
| EXIT-02 | Client handles EOF, connect failure and server disappearance | Client transport lifecycle | test_client_workflow |
| DOC-01 | Exact commands, CLI summary, rules and limitations in README | Codes/README.md | Team reproduce instructions in official image |
| DOC-02 | Twelve development-documentation topics and real evidence | dev/design.md and supporting records | Final PDF authoring after actual validation |
| SUB-01 | Correct ZIP contents; valid bank; no Dockerfile/cache/environment | Planned Codes/Documentation package | Fresh unpacked-package inspection and execution |
| TEAM-01 | Real member contributions total100%; all members explain work | Pending team contribution record | Team review, video and actual repository evidence |
| VIDEO-01 | At most10min, Tasks1–5, official image, all members | Planned demonstration | Record actual session after acceptance passes |

## Outstanding acceptance work

Obtain official image identity and instructions, record environment, run all automated suites, investigate failures, perform interactive/slow-reader/IPv6/terminal checks where applicable, record actual results, and repeat from the selected submission package. Obtain team information and final report/video submission instructions before final documentation and packaging.
