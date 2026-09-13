#!/usr/bin/env bash
# Start the dedicated localhost test instance, wait for it, and run real tests.
set -euo pipefail
cd "$(dirname "$0")/.."
test_python="${PYTHON:-python3}"
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml up -d
"$test_python" - <<'PY'
import time
import urllib.error
import urllib.request

url = "http://127.0.0.1:18080/config/server/version"
deadline = time.monotonic() + 180
print("Waiting up to 180 seconds for localhost Gerrit 3.13.4...", flush=True)
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            body = response.read()
        if b'"3.13.4"' not in body:
            raise SystemExit("Unexpected server at localhost:18080; expected the dedicated Gerrit 3.13.4 instance")
        break
    except (urllib.error.URLError, TimeoutError, ConnectionError):
        time.sleep(2)
else:
    raise SystemExit("Gerrit did not become ready. Inspect: docker compose -p gerrit-export-test -f compose.gerrit-test.yaml logs")
print("Gerrit is ready.", flush=True)
PY
"$test_python" tests/real_gerrit.py
