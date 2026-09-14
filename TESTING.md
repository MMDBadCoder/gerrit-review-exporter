# Testing version 0.3.0

Two suites. Neither needs credentials for any existing Gerrit server, and every
account, project, comment and line of code they use is synthetic.

## 1. Prerequisites

```bash
python3 --version   # 3.10 or later
git --version
python3 gerrit_rv.py --version   # 0.3.0
```

No pip dependencies. Linux or macOS; on Windows use WSL2.

## 2. Unit tests — no network, no Docker

```bash
make test
# or: python3 -m unittest discover -s tests -v
```

74 tests covering the parts that decide whether a `.rv` file is correct:

| Area | What is pinned down |
|---|---|
| Line mapping | Insertions do not move the line above them; replaced lines report the region that replaced them; deletions shift what follows; offsets accumulate across hunks |
| Comment spans | A Gerrit range ending at character 0 does not cover that line |
| Threads | Replies on later patch sets stay in one thread; line movement never splits a thread; reply cycles and invisible parents are kept and flagged; parents precede replies even when timestamps tie |
| Resolution | The last comment decides resolved state; the anchor is the first comment, not the newest |
| Formatting | Change messages survive as one CSV-safe field; metadata quotes only when needed; stale line numbers clamp to the file |
| Config | Defaults fill in, values override, a mistyped key is rejected, `null` limits mean no limit |
| Query | Empty filter lists do not restrict; multiple values become an OR group; values needing quotes are quoted |
| Limits | A budget stops at its limit, refuses partial group takes, and reports which limit stopped the run |
| TLS | Verification is on by default; `insecure_tls` disables both the certificate and the hostname check |
| Thread length filter | `0` keeps everything; a thread of only short comments is dropped; one substantial comment anywhere keeps it; the threshold is exact and whitespace does not count |
| HTTP credentials | Git gets a helper for HTTP(S) remotes under `basic`; the snippet names the environment variables and never holds the secret; SSH and the non-basic modes add nothing; `-c` settings reach Git before the subcommand, where they take effect |

The scenario tests build **real Git repositories** and let `Archive` fetch from
them, so the fetch, the SHA-addressed refs and `git show` all run for real:

- a comment on patch set 3 of a change that ran to 12, answered at 9
- two review rounds on one change resolving independently
- a thread answered in words with no code change
- a file deleted by the resolving patch set
- a file added by the patch set under review
- file-level, commit-message and `/PATCHSET_LEVEL` comments
- unresolved threads, binary files, stale line numbers, excluded paths
- an unchanged region correctly labelled as not edited

## 3. Real Gerrit — Docker

```bash
make test-real
```

This starts Gerrit 3.13.4 on `127.0.0.1:18080`, waits for readiness, creates a
uniquely named project with real accounts, and seeds reviews by pushing to
`refs/for/main` and posting published comments through the REST API.

It then runs the exporter against that server and checks 35 assertions,
including:

- every document has two metadata lines, a blank line, and the three sections in
  order, with balanced quoting and no embedded newlines
- the long-running change anchors at patch set 3 and resolves at 9 — **not** 12
- the before section holds the reviewed code and the after section holds the fix
- non-ASCII comment text survives the round trip
- a thread answered in discussion reports `resolved_in=none` and says why
- an unresolved thread is excluded by default and included with
  `enrich.include_unresolved`
- `limits.max_files` stops the run and the run says so
- **nothing on Gerrit was modified** — the open thread is still open afterwards

The harness refuses any non-loopback URL, so it cannot touch a real server.

### Bootstrap credentials

The container runs `gerrit.war init --dev`, which creates `admin` with the
well-known development password `secret`. The harness authenticates every admin
call with that password directly and mints **no** admin token: Gerrit caps an
account at ten tokens, and `admin` is the one account every run shares, so a
per-run token would make the eleventh run fail with an opaque
`Maximum number of tokens (10) already reached`. The per-run developer and
reviewer accounts are created fresh each time, so their tokens never accumulate.

A cookie session from `/login/?account_id=` is deliberately **not** used either:
Gerrit accepts it for GETs but rejects writes made with it, so seeding would fail
at the first `PUT`.

The suite is repeatable — run it as many times as you like against the same
container.

### Artefacts and cleanup

Each run writes to `.local-gerrit/<run-id>/`: the seeded working copy, the config
used, the exported `.rv` files and a `report.json`. These survive container
removal.

```bash
# Stop the container but keep its data:
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml stop

# Remove the container and its synthetic Gerrit data:
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml down
```

Each run creates a new project and does not clean up previous ones. Validation on
3.13.4 is not a claim of compatibility with every Gerrit version or plugin.
