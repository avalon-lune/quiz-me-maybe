# Quiz Me Maybe

Small packets, big bragging rights.

A multiplayer command-line quiz tournament built with Python 3.12 and raw TCP
sockets. Players discover quizzes, form rooms, ready up and compete against a
server-controlled clock. The server owns question order, accepted answers and
scores, with independent rooms and results revealed only after each question closes.

**Status:** implementation and 108 test methods have been authored and statically
reviewed. Runtime verification is pending the official examiner-provided Docker
image. No application or test execution is claimed, and this repository has no
automatic test workflow.

## Start here

- [Run instructions and client commands](Codes/README.md)
- [Chinese project guide](dev/开发说明.md)
- [Architecture and development record](dev/design.md)
- [Application-layer protocol](dev/protocol.md)
- [Requirements traceability](dev/requirements.md)
- [Test plan and pending execution record](dev/test-plan.md)
- [Static review findings and corrections](dev/静态复查记录.md)

`Codes/` contains the client, server, supporting modules, question bank and tests.
`dev/` contains working design and verification notes. The final development PDF
and recorded demonstration will be prepared after official-environment validation
and the team's contribution information are complete.

## Collaboration

Keep changes in short, descriptive commits. Use a branch and pull request for
team review, and keep test results tied to the source revision actually executed.
Run coursework verification only in the official image once it is available.
