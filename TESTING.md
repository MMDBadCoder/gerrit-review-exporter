# Testing version 0.1.0

You can run both suites without credentials for an existing Gerrit server.
All accounts, projects, comments, and code used here are synthetic.

## 1. Get the release and check prerequisites

```bash
git clone --branch v0.1.0 https://github.com/MMDBadCoder/gerrit-review-exporter.git
cd gerrit-review-exporter
python3 --version
git --version
python3 gerrit_export.py --version
```

Use Python 3.10 or later and Git on Linux or macOS. The exporter uses POSIX file
locking and symlinks; on Windows use a Linux environment such as WSL2. No pip
dependencies are required. `--version` should print:

```text
gerrit-review-exporter 0.1.0
```

## 2. Run the local regression suite

```bash
make test
# Equivalent without make:
python3 -m unittest discover -s tests -v
```

Expected result: **22 tests, OK**, with exit code 0. Some tests deliberately print
`Export incomplete` or simulated HTTP/Git failures: these exercise failure
handling. The unittest summary determines whether the suite passed.

These tests start HTTP fixtures on random loopback ports and create real Git
repositories in temporary directories, which are cleaned up afterward. They test
pagination, resumption after interruption, Unicode, thread reconstruction, equal
timestamps, missing parents/cycles, raw preservation, incremental updates, merge
and binary patches, authentication, retries, and corruption detection.

No external network connection is required for this suite. The process must be
allowed to open loopback sockets. A sandbox that forbids sockets will report
`PermissionError: Operation not permitted`; run in an environment that allows
local test servers.

## 3. Run against actual Gerrit in Docker

Install Docker Engine/Desktop and Docker Compose v2, and start the daemon:

```bash
docker version
docker compose version
make test-real
# Equivalent without make:
bash tests/run_real_gerrit.sh
```

The first run downloads the official Gerrit image. Docker therefore needs network
access to the image registry. The image is pinned to version **3.13.4** and its
digest in `compose.gerrit-test.yaml`. It bundles the required Java runtime; you do
not need Java installed on the host. The container is limited to 1.5 GiB memory
and 2 CPUs; allow additional host resources for Docker, Python, and Git.

The runner:

1. Starts a dedicated Gerrit container with ports bound to `127.0.0.1:18080`
   (HTTP) and `127.0.0.1:29419` (SSH).
2. Waits up to 180 seconds for the expected server version.
3. Creates developer, reviewer, and CI accounts and a uniquely named test project.
4. Pushes actual Git commits for merged, abandoned, open, and private reviews.
5. Adds two patch sets, replies, Unicode/file comments, bot feedback, votes, and a draft.
6. Runs the exporter over real authenticated REST and Git HTTP connections.
7. Compares the exported data with Gerrit, checks archive integrity, tests an
   incremental update, and tests recovery after a deliberately invalid Git URL.

Expected result: **20 `PASS:` lines**, exit code 0, and `"status": "passed"` in
`.local-gerrit/latest-results.json`. To inspect the result:

```bash
python3 -m json.tool .local-gerrit/latest-results.json
```

The final primary sample has **4 changes, 5 revisions, 4 inline/file comments,
12 general messages, and 3 threads**. The report includes the exact output
directory and all assertions. Under `.local-gerrit/<run>/`, inspect:

- `real-gerrit-results.json`: evidence and coverage counts.
- `export-*.log`: exporter command output, including expected failure cases.
- `verify.log`: local integrity verification output.
- `export/`: primary raw snapshots, normalized JSONL, patches, and Git archive.
- `anonymous-export/`: narrower visibility excludes the private change.
- `resumed-export/`: successful recovery after the invalid Git remote test.

The synthetic results from the original validation run are committed at
[`tests/evidence/gerrit-3.13.4.json`](tests/evidence/gerrit-3.13.4.json). Actual
runtime data and credentials are excluded from Git. Test tokens are generated
in memory, expire after one day, and are not needed from you.

If Gerrit is already running from this Compose setup, rerun just the harness:

```bash
python3 tests/real_gerrit.py
```

Each run creates a new project and new accounts. The runner leaves the container
and data available for inspection. Open <http://localhost:18080> on the Docker
host and use the development account selector to view reviews. This login mode
is for the loopback-only test instance; do not expose it as a production service.

## 4. Stop or remove the test instance

```bash
# Stop it while preserving the container and its synthetic Gerrit data:
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml stop

# Start the same container again:
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml start

# Remove the test container and its synthetic Gerrit database:
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml down
```

Exported files under `.local-gerrit/` remain after `down`. The Gerrit database is
inside the container; deleting/recreating that container removes the test reviews.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| Docker socket permission denied | Run using an account authorized to use the local Docker daemon. |
| Port already allocated | Check what uses ports 18080 and 29419. Do not point the harness at an unrelated server. |
| Gerrit readiness timeout | Inspect `docker compose -p gerrit-export-test -f compose.gerrit-test.yaml logs`; check RAM and registry connectivity. |
| Wrong version on localhost:18080 | Ensure the pinned test container owns that port. |
| Unit tests cannot bind sockets | Allow loopback sockets in the test environment. |
| Harness fails | Read `.local-gerrit/latest-results.json`, the run's `export-*.log`, and container logs. |
| Too many tokens after many test runs | Recreate only this disposable test container with `down`, then `make test-real`. |

## What this proves

The release was tested with Python 3.14.4, Git 2.53.0, Docker, and real Gerrit
3.13.4 on Linux. The supported Python floor is 3.10; that floor and macOS have
not been separately exercised in this validation. The suite does not establish
compatibility with every Gerrit release/plugin or five-year production-scale
performance. Test a representative sample on your own server, then reconcile
the full export report with the visibility of the exporting account.
