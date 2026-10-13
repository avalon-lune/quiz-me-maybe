# Application layer protocol

Version 1 describes the submitted client and server implementation. The school appendix informed the framing approach; this protocol changes names, connection identity and several rules. It does not claim exact appendix compatibility.

## Framing and JSON

A persistent TCP connection carries successive frames. Each frame contains a four-byte unsigned big-endian length N and N bytes of UTF-8 JSON. N is 1..65,536 and counts bytes, not characters. The JSON root is an object. Duplicate object keys, invalid UTF-8, non-finite numbers, non-object roots, invalid Unicode surrogates and nesting beyond 32 levels are rejected. There are no delimiters between frames.

Each receiver retains incomplete bytes and can decode several coalesced messages. The server visits a connection's buffered requests in batches of at most 32, and pauses further reads while complete work is queued. Sending uses a per-connection queue and preserves the unsent suffix after a partial send. A single call to `recv` or `send` never defines an application-message boundary.

## Envelope

| Field | Type | Meaning |
|---|---|---|
| `version` | integer, exactly 1 | Protocol version; JSON boolean is invalid |
| `kind` | string | `request`, `response`, `event` or `error` |
| `type` | string | Action or event name; request names use 1..32 lowercase letters/underscores |
| `payload` | object | Message-specific fields |
| `request_id` | integer 1..2,147,483,647 | Required on requests and their direct replies |
| `event_id` | positive integer | Required on events; increases globally across the server |

Requests must increase their ID strictly within one connection. A syntactically valid ID is consumed even if the request is rejected semantically. Reusing an ID produces `REPEATED_REQUEST`; the original action is not repeated. There is no replay cache. Event IDs can skip because other rooms and individualized result messages also use the global sequence. Every frame's stream order remains authoritative.

Extra envelope fields are ignored. Each action's payload must contain exactly its documented fields. An empty payload is `{}`. Successful responses use the original action name and ID. Action errors echo those as well; envelope errors may use `type=protocol`. Fatal stream errors have `type=protocol` and `request_id=null` when a request cannot be identified.

There is no client-selected session token. The accepted TCP connection determines the caller's identity and room. Reconnecting creates a new session, not a continuation of a previous match.

## Requests and responses

All actions except `login` and `quit` require a successful login.

| Action | Request payload | Successful response payload |
|---|---|---|
| `login` | `user_id`, `name` | Confirmed `user_id`, `name` |
| `quizzes` | empty | `quizzes`: public metadata list |
| `rooms` | empty | `rooms`: joinable room summary list |
| `create` | `quiz_id` | Room snapshot |
| `join` | `room_id` | Room snapshot |
| `players` | empty | Room snapshot |
| `ready` | `ready`: boolean | Room snapshot |
| `start` | empty | Room snapshot in running state |
| `answer` | `question_id`, `choice` | `accepted=true`, `question_id` |
| `score` | empty | `room_id`, `leaderboard` |
| `leave` | empty | Departed `room_id` |
| `quit` | empty | Empty object, then connection closure |

User IDs are ASCII letters/digits/_/-, length 1..32. Names are printable Unicode, length 1..64, without outer whitespace. Both are unique among active sessions and case-sensitive. Bank quiz/question IDs use the question-bank rules, which also permit Unicode letters/numbers. Room IDs are server-issued strings such as `R0001`; they must be treated as opaque identifiers by clients. `choice` is exactly A/B/C/D. The server rejects a wrong type before indexing a room or quiz map.

Public quiz metadata contains `quiz_id`, `title`, `description`, `question_count`. It contains no question text, options or answers. A room summary contains `room_id`, `quiz_id`, `title`, `host_id`, `player_count`, `state`. Only waiting, nonfull rooms appear in `rooms`.

A room snapshot contains `room_id`, `quiz_id`, `title`, `state`, `host_id`, and `players`. Each player has `user_id`, `name`, `ready`. State is `waiting`, `running` or `finished`. Readiness matters only in waiting rooms.

Each leaderboard row contains integer `rank`, `user_id`, `name`, integer `score` and boolean `active`. Scores descend and IDs break display-order ties; equal scores share competition ranks. `active=false` means the contestant has left, not that their previously accepted work has been erased.

## Server events

| Event | Payload |
|---|---|
| `room_updated` | Room snapshot after membership, readiness, start or finish changes |
| `quiz_started` | `room_id`, `quiz_id`, `title`, `total_questions` |
| `question_opened` | `room_id`, `question_id`, `number`, `total_questions`, `text`, `options`, `time_limit`, `points` |
| `question_closed` | `room_id`, `question_id`, `correct_answer`, `your_answer`, `points_awarded`, `leaderboard` |
| `quiz_finished` | `room_id`, `quiz_id`, `final_leaderboard`, `winners` |

Question numbers are one-based. `options` is the exact A/B/C/D mapping from the bank; time and points use the bank's integer values. `your_answer` is the accepted choice, or null when no answer was accepted. Result events are individualized: another player's accepted choice is not included. `winners` is a list of all rank-one user IDs. Every room event includes its room ID and is delivered only to current members of that room.

On a successful start, the direct response precedes `room_updated`, `quiz_started` and `question_opened` for the requester. The server never waits for a client acknowledgement to advance a timer. At closure, results precede the next question. The last result precedes `quiz_finished` and the finished room snapshot. Unrelated rooms can interleave on the server but never share recipients or mutable scores.

## Time and state rules

The server opens a question with `deadline = monotonic_now + time_limit`. It checks timers before dispatching every request and separately rejects an answer with `now >= deadline`. Therefore a packet sent before the deadline but processed afterward can be late. Client timestamps are ignored, and no clock synchronization is assumed.

The full time limit is used even if everyone answers early. The closure transition scores exactly once and clears the deadline. Nonfinal questions schedule a two-second intermission; opening the next question creates a new full-duration deadline. A large timer jump closes only the currently open question, rather than silently skipping undisplayed questions.

Before start, only the host may start, at least two members must be present, and all must be ready. Joining is restricted to waiting rooms. In any state, leaving removes the membership; the earliest remaining member becomes host. The last departure removes the room and its pending timer. Match-start contestants retain their score records until the room disappears. Readiness of other members is unchanged by a join or departure.

## Error catalogue

Errors contain `payload={"code": "...", "message": "..."}`. Clients use the code for interpretation and display the diagnostic as plain text.

| Code | Meaning |
|---|---|
| `INVALID_FRAME` | Zero or oversized frame length |
| `INVALID_MESSAGE` | Invalid encoding/JSON/envelope/version/type |
| `REPEATED_REQUEST` | Request ID did not increase |
| `LOGIN_TIMEOUT`, `FRAME_TIMEOUT` | Login or partial-frame deadline exceeded |
| `UNKNOWN_ACTION`, `BAD_PAYLOAD` | Unknown action or wrong action fields/types |
| `AUTH_REQUIRED`, `ALREADY_AUTHENTICATED` | Incorrect login state |
| `INVALID_ID`, `INVALID_NAME` | Invalid identity format |
| `DUPLICATE_ID`, `DUPLICATE_NAME` | Active identity already exists |
| `ALREADY_IN_ROOM`, `NOT_IN_ROOM` | Incorrect membership state |
| `NO_SUCH_QUIZ`, `ROOM_NOT_FOUND` | Requested resource does not exist |
| `ROOM_LIMIT`, `ROOM_FULL`, `ROOM_CLOSED` | Capacity or room-state restriction |
| `HOST_ONLY`, `NOT_ENOUGH_PLAYERS`, `NOT_READY` | Start conditions are unmet |
| `QUESTION_CLOSED`, `STALE_QUESTION` | No accepting question or wrong question ID |
| `INVALID_CHOICE`, `ALREADY_ANSWERED` | Invalid choice or an answer already accepted |
| `SESSION_EXISTS`, `SESSION_CLOSED` | Internal connection lifecycle misuse |

Invalid complete request envelopes or game actions normally leave the connection usable. Invalid framing, UTF-8 or JSON closes only that connection after a best-effort error flush. Login and incomplete-frame timeouts also close it. Abrupt EOF/reset and send failure remove the session immediately. A peer that exceeds the output cap or stalls writes is disconnected. Errors are not promised when the transport is already unusable or server connection capacity has been reached.

## Examples

The following are JSON bodies; the length prefix still precedes each body on the wire.

```json
{"version":1,"kind":"request","request_id":1,"type":"login","payload":{"user_id":"alice","name":"Alice"}}
{"version":1,"kind":"response","request_id":1,"type":"login","payload":{"user_id":"alice","name":"Alice"}}
{"version":1,"kind":"request","request_id":8,"type":"answer","payload":{"question_id":"NET001","choice":"B"}}
{"version":1,"kind":"response","request_id":8,"type":"answer","payload":{"accepted":true,"question_id":"NET001"}}
{"version":1,"kind":"error","request_id":9,"type":"answer","payload":{"code":"ALREADY_ANSWERED","message":"An answer has already been accepted for this question"}}
```

These are protocol examples, not recorded network captures or test results. The verification plan is in `test-plan.md`.
