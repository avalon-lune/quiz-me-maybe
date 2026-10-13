# Implementation contract

Status: implementation contract, not an execution record. Official examiner image is pending. Do not run coursework programs or tests outside that image.

## Boundaries

Python 3.12 standard library only. Direct TCP sockets. Plain text CLI. English identifiers and concise explanatory comments; no authorship statements in source comments. Server owns all mutable game state in one selector loop. Client never opens the question bank.

## Wire protocol

Four-byte unsigned big-endian length followed by a UTF-8 JSON object. Payload length 1..65,536 bytes. No NaN/Infinity or duplicate JSON keys. Every envelope contains version (integer 1), kind, type, payload (object). Requests: kind=request, type is action below, request_id integer 1..2,147,483,647, strictly increasing per connection. Responses/errors repeat request_id; events contain increasing event_id (global server counter; gaps allowed). Error payload is code/message. Envelope extension fields are accepted. Session identity belongs to the connection, not a client-supplied identifier.

Actions and request payloads:

* login: user_id, name
* quizzes: empty
* rooms: empty
* create: quiz_id
* join: room_id
* players: empty
* ready: ready (boolean)
* start: empty
* answer: question_id, choice (A/B/C/D)
* score: empty
* leave: empty
* quit: empty (also allowed before login)

Reply payloads: login -> user_id/name; quizzes -> quizzes list (quiz_id/title/description/question_count); rooms -> rooms list; create/join/players/ready/start -> room snapshot; answer -> accepted=true/question_id; score -> room_id/leaderboard; leave -> room_id; quit -> empty.

Room snapshot: room_id, quiz_id, title, state (waiting/running/finished), host_id, players (user_id/name/ready). Rooms listing includes room_id/quiz_id/title/host_id/player_count/state only. Leaderboard entries: rank,user_id,name,score,active; sort descending score, then user_id lexicographically; equal scores have competition ranks (1,1,3).

Events: room_updated (room snapshot); quiz_started (room_id/quiz_id/title/total_questions); question_opened (room_id/question_id/number/total_questions/text/options/time_limit/points); question_closed (room_id/question_id/correct_answer/your_answer/points_awarded/leaderboard); quiz_finished (room_id/quiz_id/final_leaderboard/winners). Every room event includes room_id. No question text/options in quiz listings, no answer or correctness/score change before closure.

Rules: ID ASCII letters/digits/_/- length 1..32; name printable Unicode 1..64 with no surrounding whitespace. Both unique among active sessions, case-sensitive. One room per player. Minimum 2 players, maximum 32 per room, 64 rooms, host only starts, all ready. No joins after start. Earliest remaining member inherits host. Empty room removed. Departed contestants retain earned scores and already accepted answers; no reconnect/resume. Finished room remains until members leave; no rematch. Waiting room joins/leaves preserve other readiness. Full time_limit always used, then 2-second intermission before next question. Receipt means server dispatch time; now >= deadline is late. Monotonic clock only for deadlines. First valid answer wins; invalid submissions do not use the attempt. Correct answer earns exact points; otherwise zero; no bonuses.

## Shared Python interfaces

question_bank.py: BankError(ValueError); frozen Question(question_id,text,options,answer,time_limit,points), Quiz(quiz_id,title,description,questions); load_bank(path)->dict[str,Quiz] (in file order). Quiz.questions tuple; Question.options mapping. Strict integer type excluding bool. MAX_BANK_BYTES=1_000_000 (documented interpretation pending clarification). Unicode letters/numbers and _/- for bank IDs; visible text allows printable Unicode and spaces, requires a non-space when minimum > 0. All extra object members ignored except options exact four keys. Entire input validated, bounded read before socket creation.

bank_json.py: loads(document, parse_int=callback)->object. Explicit-stack JSON container parser used only by the bank loader after its bounded UTF-8 read. Public standard-library decoding for strings, strict JSON numbers/whitespace/separators, duplicate-key rejection, no recursive container calls or changes to process recursion settings. Ignored extension fields still require valid JSON. The integer callback preserves the loader's handling of oversized extension integers.

game.py: GameError with code/message; Event(recipients:tuple[int,...],type:str,payload:dict); Outcome(payload:dict,events:list[Event],close:bool=False). Game(bank); connect(session_id:int); is_authenticated(session_id)->bool; handle(session_id,action,payload,now:float)->Outcome; tick(now)->list[Event]; next_deadline()->float|None; disconnect(session_id,now)->list[Event]. handle never performs I/O. Server calls tick immediately before each request; answer independently rejects expired deadlines. disconnect must be safe more than once. Event payloads are snapshots, never shared mutable live structures. Root implements transport only.

protocol.py: ProtocolError(ValueError) with code/message; MAX_FRAME_BYTES; encode_message(message:dict)->bytes; FrameDecoder.feed(data:bytes)->None; FrameDecoder.pop()->dict|None, buffered_bytes property; validate_request(message)->tuple[int,str,dict]. FrameDecoder pop decodes one frame, preserves tail. Recoverable semantic request validation errors versus fatal frame/UTF-8/JSON errors. Client uses the same codec and verifies server envelope itself.

server.py: required --host/--port/--questions; strict CLI port 1..65535; IPv4 or IPv6 host resolved with getaddrinfo. Prints SERVER_READY after validation and listen, flush. stderr diagnostics. Max 128 connections; unauthenticated login deadline 30s; incomplete frame timeout 10s from first pending bytes; send queue cap 1MiB; write-stall timeout 15s; selector polling at most 0.25s. Bounded accept/read/write/request work per turn. Disconnected peers removed from selector/session/room; output overflow isolates that peer. Ctrl+C graceful stop, close sockets, no tracebacks. Read half-close treated as full departure; quit is the graceful acknowledgement route.

client.py: required --server/--port/--id/--name; stdin daemon reader + bounded queue; network loop nonblocking or select with bounded connect/write timeouts; asynchronous events visible while input is idle. /help /quizzes /rooms /create ID /join ID /players /ready /unready /start /answer A [QUESTION_ID] /score /leave /quit. /answer without explicit ID binds to the question shown at input capture, not a later question; explicit ID allows stale-answer demonstration. No terminal escape sequences or external dependencies. Output uses stable human-readable markers LOGIN_OK, ROOM, QUESTION, ANSWER_ACCEPTED, RESULT, LEADERBOARD, FINISHED, ERROR for CLI integration testing. Network identity fields never accepted from interactive commands.

## Work allocation

Root: protocol.py, server.py, protocol tests, integration review, README and design documentation.
Domain agent: question_bank.py, game.py, questions.json, question bank and game tests.
Client agent: client.py, client unit tests (no network/server execution).
Verification agent: real-socket integration tests and actual CLI subprocess tests, dev test plan. Read all implementation interfaces before finalizing. Do not execute tests until the official image is available.
