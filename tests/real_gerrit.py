#!/usr/bin/env python3
"""Seed a dedicated localhost Gerrit with irregular reviews and check the .rv output.

The unit tests build patch sets with plain Git. This one drives a real Gerrit:
real accounts, real `refs/for/main` pushes, real published comments with real
`unresolved` flags, and the tool's real HTTP and Git paths.

It creates its own uniquely named project and refuses any non-loopback URL, so
it can never touch a production server.
"""
import argparse
import base64
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gerrit_rv as rv


class API:
    """Gerrit REST over HTTP basic auth.

    Not the cookie session `/login/?account_id=` offers in development mode: a
    cookie authenticates GETs but Gerrit rejects writes made with it, so seeding
    a project would fail at the first PUT.
    """

    def __init__(self, url, user, token):
        self.url, self.user, self.token = url, user, token
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))

    def call(self, method, path, data=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{self.user}:{self.token}".encode()).decode()
            path = "/a" + path
        body = json.dumps(data).encode() if data is not None else None
        request = urllib.request.Request(self.url + path, data=body, headers=headers, method=method)
        with self.opener.open(request, timeout=60) as response:
            raw = response.read()
        text = raw.decode()
        if text.startswith(")]}'"):
            text = text.split("\n", 1)[1]
        return json.loads(text) if text.strip() else None


def execute(command, cwd=None, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise AssertionError(f"{command[:2]} failed ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:18080")
    parser.add_argument("--output", type=Path, default=ROOT / ".local-gerrit")
    args = parser.parse_args()
    if urllib.parse.urlsplit(args.url).hostname not in ("localhost", "127.0.0.1", "::1"):
        parser.error("Only a dedicated loopback test Gerrit is allowed")

    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    work = args.output.resolve() / run_id
    work.mkdir(parents=True)
    os.chmod(work, 0o700)
    passed = []

    def check(condition, message):
        if not condition:
            raise AssertionError(message)
        passed.append(message)
        print("PASS:", message, flush=True)

    # `gerrit.war init --dev` creates admin/secret. The entrypoint in
    # compose.gerrit-test.yaml uses exactly that, and this is a throwaway
    # loopback instance, so the well-known development password is the
    # bootstrap credential.
    # `gerrit.war init --dev` creates admin/secret, and the entrypoint in
    # compose.gerrit-test.yaml uses exactly that. The password authenticates every
    # admin call directly, so no token is minted: Gerrit caps an account at ten
    # tokens, and `admin` is the one account every run shares, so minting one per
    # run makes the eleventh run fail with an opaque "Maximum number of tokens".
    admin = API(args.url, "admin", "secret")
    version = admin.call("GET", "/config/server/version")

    accounts = {}
    for role in ("developer", "reviewer"):
        username = f"{role}-{run_id}"
        account = admin.call("PUT", "/accounts/" + username,
                             {"name": role.title(), "email": f"{username}@example.com"})
        token = admin.call("PUT", f"/accounts/{account['_account_id']}/tokens/rv", {"lifetime": "1d"})["token"]
        accounts[role] = (API(args.url, username, token), account)
    developer, dev_account = accounts["developer"]
    reviewer, _ = accounts["reviewer"]

    project = f"rv-e2e/{run_id}"
    encoded = urllib.parse.quote(project, safe="")
    admin.call("PUT", "/projects/" + encoded, {"create_empty_commit": True, "branches": ["main"]})
    admin.call("POST", f"/projects/{encoded}/access", {"add": {"refs/heads/*": {"permissions": {
        "label-Code-Review": {"rules": {"global:Registered-Users": {"action": "ALLOW", "min": -2, "max": 2}}}}}}})

    askpass = work / "askpass.py"
    askpass.write_text("#!/usr/bin/env python3\nimport os, sys\n"
                       "print(os.environ['GERRIT_USER'] if 'username' in sys.argv[1].lower() "
                       "else os.environ['GERRIT_HTTP_PASSWORD'])\n")
    askpass.chmod(0o700)
    env = {**os.environ, "GERRIT_USER": developer.user, "GERRIT_HTTP_PASSWORD": developer.token,
           "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
           "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": ""}

    repo, remote = work / "working-copy", f"{args.url}/a/{project}"
    execute(["git", "clone", "--branch", "main", remote, str(repo)], env=env)

    def git(*command):
        return execute(["git", *command], cwd=repo, env=env)

    git("config", "user.name", "Developer")
    git("config", "user.email", dev_account["email"])

    def push(subject, files, change_id=None):
        """Pushes one patch set. Reusing a Change-Id adds a patch set to that change."""
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            git("add", "--", name)
        identifier = change_id or "I" + hashlib.sha1(f"{run_id}{subject}".encode()).hexdigest()
        if change_id:
            git("commit", "--amend", "--allow-empty", "-m", f"{subject}\n\nChange-Id: {identifier}")
        else:
            git("commit", "--allow-empty", "-m", f"{subject}\n\nChange-Id: {identifier}")
        git("push", "origin", "HEAD:refs/for/main")
        found = developer.call("GET", "/changes/?q=" + urllib.parse.quote("change:" + identifier))
        return found[0], identifier

    def start_change(subject, files):
        git("fetch", "origin", "main")
        git("reset", "--hard", "origin/main")
        return push(subject, files)

    def comment_on(number, patch_set, path, line, message, unresolved=True):
        reviewer.call("POST", f"/changes/{number}/revisions/{patch_set}/review", {
            "comments": {path: [{"line": line, "message": message, "unresolved": unresolved}]}})

    def reply_all(number, patch_set, message, unresolved=False):
        """Replies to the newest comment on each file, as an author answers a round."""
        existing = developer.call("GET", f"/changes/{number}/comments")
        replies = {}
        for path, entries in existing.items():
            newest = sorted(entries, key=lambda c: c["updated"])[-1]
            replies[path] = [{"line": newest.get("line"), "in_reply_to": newest["id"],
                              "message": message, "unresolved": unresolved}]
        if replies:
            developer.call("POST", f"/changes/{number}/revisions/{patch_set}/review", {"comments": replies})

    # --- scenario 1: a long change with the comment in the middle -----------
    # Patch sets 1-8 before anyone looks, a comment on 3, work continues to 12,
    # and the file the comment is about only changes at patch set 9.
    change, change_id = start_change("Long running change",
                                     {"src/engine.py": "def total(values):\n    return sum(values)\n",
                                      "notes.txt": "1\n"})
    number = str(change["_number"])
    for i in range(2, 9):
        push("Long running change", {"notes.txt": f"{i}\n"}, change_id)
    comment_on(number, 3, "src/engine.py", 2, "Round to two decimals. 日本語 — précision.")
    push("Long running change", {"src/engine.py": "def total(values):\n    return round(sum(values), 2)\n"},
         change_id)                                                     # patch set 9
    reply_all(number, 9, "Done in patch set 9.", unresolved=False)
    for i in range(10, 13):
        push("Long running change", {"notes.txt": f"{i}\n"}, change_id)
    long_change = number

    # --- scenario 2: answered in words, no code change ----------------------
    change2, id2 = start_change("Discussed but unchanged", {"src/keep.py": "LIMIT = 10\n"})
    number2 = str(change2["_number"])
    comment_on(number2, 1, "src/keep.py", 1, "Why 10?")
    push("Discussed but unchanged", {"notes2.txt": "unrelated\n"}, id2)  # patch set 2
    reply_all(number2, 2, "Legacy contract; leaving as is.", unresolved=False)

    # --- scenario 3: still unresolved ---------------------------------------
    change3, _ = start_change("Open question", {"src/open.py": "value = 1\n"})
    number3 = str(change3["_number"])
    comment_on(number3, 1, "src/open.py", 1, "This still needs an answer.", unresolved=True)

    print(f"seeded project {project} on Gerrit {version}", flush=True)

    # --- run the exporter ---------------------------------------------------
    runner_env = {**os.environ, "GERRIT_USER": developer.user,
                  "GERRIT_HTTP_PASSWORD": developer.token,
                  "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0"}

    def run_export(directory, **overrides):
        config = {
            "gerrit": {"url": args.url, "auth": "basic", "git_url": f"{args.url}/a/{project}"},
            "filters": {"projects": [project], "status": "any", "recent_days": 1},
            "limits": {"max_changes": 50, "max_comments": 500, "max_files": 100},
            "workers": 4,
            "output": {"dir": str(directory)},
        }
        for key, value in overrides.items():
            config.setdefault(key, {})
            config[key] = {**config.get(key, {}), **value} if isinstance(value, dict) else value
        path = work / f"config-{directory.name}.json"
        path.write_text(json.dumps(config, indent=2))
        result = subprocess.run([sys.executable, str(ROOT / "gerrit_rv.py"), "export", "--config", str(path)],
                                capture_output=True, text=True, timeout=900, env=runner_env)
        if result.returncode != 0:
            raise AssertionError(f"export failed:\n{result.stdout}\n{result.stderr}")
        return result

    output = work / "rv-out"
    exported = run_export(output)
    print(exported.stdout, flush=True)

    files = sorted(output.glob("*.rv"))
    check(files, f"exported at least one .rv file ({len(files)} found)")
    documents = {path.name: path.read_text(encoding="utf-8") for path in files}
    summary = json.loads((output / "summary.json").read_text())
    check(summary["files_written"] == len(files), "summary agrees with what is on disk")

    for name, text in documents.items():
        lines = text.splitlines()
        check(lines[0].startswith("id="), f"{name}: first line is metadata")
        check("change=" in lines[0] and "patchset=" in lines[0], f"{name}: first line identifies the comment")
        check(lines[1].startswith('"'), f"{name}: second line starts with the quoted change subject")
        check(lines[1].count('"') % 2 == 0, f"{name}: second line quoting is balanced")
        check(lines[2] == "", f"{name}: blank line separates metadata from the body")
        check("--- before" in text and "--- review" in text and "--- after" in text,
              f"{name}: has all three sections")
        check(text.index("--- before") < text.index("--- review") < text.index("--- after"),
              f"{name}: sections are in order")

    long_docs = [text for text in documents.values() if f"change={long_change} " in text.splitlines()[0]]
    check(long_docs, "the long-running change produced a document")
    long_doc, header = long_docs[0], long_docs[0].splitlines()[0]
    check("patchset=3" in header, "anchored on the patch set the reviewer commented on, not the newest")
    check("resolved_in=9" in header, "resolved by the first later patch set that touched the file, not 12")
    check("return sum(values)" in long_doc, "before section shows the code the reviewer saw")
    check("round(sum(values), 2)" in long_doc, "after section shows the code that answered the comment")
    check("Round to two decimals" in long_doc, "review section carries the comment text")
    check("日本語" in long_doc, "non-ASCII comment text survives the round trip")
    check("Done in patch set 9." in long_doc, "the author's reply is part of the same thread")
    check("code_changed=yes" in header, "the commented lines were genuinely edited")

    talk_docs = [text for text in documents.values() if f"change={number2} " in text.splitlines()[0]]
    check(talk_docs, "the discussion-only change produced a document")
    talk = talk_docs[0]
    check("resolved_in=none" in talk.splitlines()[0], "no patch set is claimed when the file never changed")
    check("answered in discussion" in talk, "the after section says why there is no new code")
    check("Legacy contract" in talk, "the author's reasoning is captured")

    open_docs = [text for text in documents.values() if f"change={number3} " in text.splitlines()[0]]
    check(not open_docs, "an unresolved thread is not exported by default")

    second = work / "rv-with-open"
    run_export(second, enrich={"include_unresolved": True})
    with_open = [text for text in (path.read_text() for path in second.glob("*.rv"))
                 if f"change={number3} " in text.splitlines()[0]]
    check(with_open, "enrich.include_unresolved exports the open thread")
    check("resolved=no" in with_open[0].splitlines()[0], "the open thread is labelled unresolved")

    third = work / "rv-limited"
    limited = run_export(third, limits={"max_files": 1})
    produced = list(third.glob("*.rv"))
    check(len(produced) <= 1, f"limits.max_files stopped the export ({len(produced)} file)")
    check("stopped early" in limited.stdout, "the run reports that a limit stopped it")

    still_open = developer.call("GET", f"/changes/{number3}/comments")
    check(any(c.get("unresolved") for entries in still_open.values() for c in entries),
          "the exporter did not resolve or modify anything on Gerrit")

    report = {"run_id": run_id, "gerrit_version": version, "project": project,
              "files": len(files), "assertions": passed, "status": "passed"}
    (work / "report.json").write_text(json.dumps(report, indent=2))
    print(f"\n{len(passed)} assertions passed. Artefacts in {work}", flush=True)
    print("Sample document:\n")
    print(long_doc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
