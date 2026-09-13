#!/usr/bin/env python3
"""Seed and verify the dedicated localhost Gerrit from compose.gerrit-test.yaml.

This deliberately creates accounts, a uniquely named test project and reviews.
It refuses non-loopback URLs. It never uses an existing production project.
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
import gerrit_export as ge


class API:
    def __init__(self, url, user=None, token=None, account=None):
        self.url, self.user, self.token = url, user, token
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        if account is not None:
            self.opener.open(url + f"/login/?account_id={account}").close()

    def call(self, method, path, data=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Basic " + base64.b64encode(f"{self.user}:{self.token}".encode()).decode()
            path = "/a" + path
        else:
            for cookie in self.jar:
                if cookie.name == "XSRF_TOKEN":
                    headers["X-Gerrit-Auth"] = cookie.value
        body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
        try:
            with self.opener.open(urllib.request.Request(self.url + path, body, headers, method=method), timeout=60) as response:
                raw = response.read()
            return ge.parse_response(raw) if raw else None
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")
            error.close()
            raise RuntimeError(f"Test setup {method} {path}: HTTP {error.code}: {detail[:1000]}") from None


def execute(command, cwd=None, env=None, expected=0, logfile=None):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    if logfile:
        logfile.write_text(result.stdout + result.stderr)
    if result.returncode != expected:
        raise AssertionError(f"Command {command[0:2]} returned {result.returncode}, expected {expected}:\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="http://127.0.0.1:18080")
    p.add_argument("--output", type=Path, default=ROOT / ".local-gerrit")
    args = p.parse_args()
    parsed = urllib.parse.urlsplit(args.url)
    if parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        p.error("Only a dedicated loopback test Gerrit is allowed")
    run_id = time.strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]
    work = args.output.resolve() / run_id
    work.mkdir(parents=True)
    os.chmod(work, 0o700)
    results = {"started_at": ge.now(), "server_url": args.url, "assertions": [], "status": "running"}

    def check(condition, message):
        if not condition:
            raise AssertionError(message)
        results["assertions"].append(message)
        print("PASS:", message, flush=True)

    try:
        bootstrap = API(args.url, account=1000000)
        results["gerrit_version"] = bootstrap.call("GET", "/config/server/version")
        admin_token = bootstrap.call("PUT", f"/accounts/self/tokens/test-{run_id}", {"lifetime": "1d"})["token"]
        admin = API(args.url, "admin", admin_token)
        accounts = {}
        for role in ("developer", "reviewer", "ci-bot"):
            username = role + "-" + run_id
            account = admin.call("PUT", "/accounts/" + username,
                                 {"name": role.title(), "email": username + "@example.com"})
            token = admin.call("PUT", f"/accounts/{account['_account_id']}/tokens/test", {"lifetime": "1d"})["token"]
            accounts[role] = (API(args.url, username, token), account)
        developer, dev_account = accounts["developer"]
        reviewer, reviewer_account = accounts["reviewer"]
        bot, _ = accounts["ci-bot"]
        project = "export-e2e/" + run_id
        encoded_project = urllib.parse.quote(project, safe="")
        admin.call("PUT", "/projects/" + encoded_project, {"create_empty_commit": True, "branches": ["main"]})
        admin.call("POST", f"/projects/{encoded_project}/access", {"add": {
            "refs/heads/*": {"permissions": {"label-Code-Review": {"rules": {
                "global:Registered-Users": {"action": "ALLOW", "min": -2, "max": 2}}}}}}})
        results["project"] = project
        # Git exercises actual smart HTTP authentication, not local filesystem fetches.
        askpass = work / "askpass.py"
        askpass.write_text("#!/usr/bin/env python3\nimport os, sys\nprint(os.environ['GERRIT_USER'] if 'username' in sys.argv[1].lower() else os.environ['GERRIT_HTTP_PASSWORD'])\n")
        askpass.chmod(0o700)

        def environment(api):
            return {**os.environ, "GERRIT_USER": api.user, "GERRIT_HTTP_PASSWORD": api.token,
                    "GIT_ASKPASS": str(askpass), "GIT_TERMINAL_PROMPT": "0",
                    "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "credential.helper", "GIT_CONFIG_VALUE_0": ""}

        repo, remote = work / "working-copy", args.url + "/a/" + project
        execute(["git", "clone", "--branch", "main", remote, str(repo)], env=environment(developer))

        def git(*command):
            return execute(["git", *command], cwd=repo, env=environment(developer))

        git("config", "user.name", "Developer")
        git("config", "user.email", dev_account["email"])

        def new_change(subject, filename, content, private=False):
            git("fetch", "origin", "main")
            git("reset", "--hard", "origin/main")
            path = repo / filename
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            git("add", "--", filename)
            change_id = "I" + hashlib.sha1((run_id + subject).encode()).hexdigest()
            git("commit", "-m", subject + "\n\nChange-Id: " + change_id)
            git("push", "origin", "HEAD:refs/for/main" + ("%private" if private else ""))
            return developer.call("GET", "/changes/?q=" + urllib.parse.quote("change:" + change_id))[0]

        review = new_change("Round computed totals", "src/engine.py", "def total(values):\n    return sum(values)\n")
        number = str(review["_number"])
        first_sha = git("rev-parse", "HEAD")
        reviewer.call("POST", f"/changes/{number}/revisions/current/review", {
            "message": "Please round the output consistently.", "labels": {"Code-Review": -1},
            "comments": {"src/engine.py": [
                {"line": 2, "message": "Round to two decimals. 日本語 — précision.", "unresolved": True},
                {"message": "Keep this module focused on aggregation.", "unresolved": False}]}})
        original = reviewer.call("GET", f"/changes/{number}/comments")
        root_comment = next(c for c in original["src/engine.py"] if c.get("line") == 2)
        developer.call("PUT", f"/changes/{number}/revisions/current/drafts", {
            "path": "src/engine.py", "line": 2, "message": "DRAFT_NOT_EXPORTED"})
        (repo / "src/engine.py").write_text("def total(values):\n    return round(sum(values), 2)\n")
        git("commit", "-am", "Round computed totals\n\nChange-Id: " + review["change_id"])
        # This is a second commit with the same Change-Id; amend its parent to avoid
        # pushing two commits with that ID in one review stack.
        git("reset", "--soft", first_sha + "^")
        git("commit", "-m", "Round computed totals\n\nChange-Id: " + review["change_id"])
        git("push", "origin", "HEAD:refs/for/main")
        second_sha = git("rev-parse", "HEAD")
        developer.call("POST", f"/changes/{number}/revisions/current/review", {
            "message": "Updated the implementation.", "drafts": "KEEP",
            "comments": {"src/engine.py": [{"line": 2, "in_reply_to": root_comment["id"],
                "message": "Fixed in patch set 2.", "unresolved": False}]}})
        bot.call("POST", f"/changes/{number}/revisions/current/review", {
            "message": "All checks passed", "tag": "autogenerated:ci-test",
            "comments": {"src/engine.py": [{"line": 2, "message": "CI checked this line", "unresolved": False}]}})
        reviewer.call("POST", f"/changes/{number}/revisions/current/review", {"labels": {"Code-Review": 2}, "message": "Approved"})
        admin.call("POST", f"/changes/{number}/submit", {})
        abandoned = new_change("Rejected approach", "rejected.txt", "Rejected approach\n")
        developer.call("POST", f"/changes/{abandoned['_number']}/abandon", {"message": "Prefer the existing design"})
        opened = new_change("Open work", "open.txt", "Still under review\n")
        private = new_change("Private work", "private.txt", "Private review context\n", private=True)
        results["changes"] = {"merged": int(number), "abandoned": abandoned["_number"],
                              "open": opened["_number"], "private": private["_number"]}
        print("Created real reviews:", results["changes"], flush=True)
        output = work / "export"

        def export(destination=output, actor=admin, *extra, expected=0):
            command = [sys.executable, str(ROOT / "gerrit_export.py"), "export", "--url", args.url,
                       "--project", project, "--git-url", remote, "--output", str(destination),
                       "--page-size", "1", "--delay", "0", "--retries", "1", *extra]
            execute(command, env=environment(actor), expected=expected,
                    logfile=work / f"export-{len(list(work.glob('export-*.log')))}.log")
            return ge.read_json(destination / "export-report.json")

        def rows(name):
            return [json.loads(line) for line in (output / "normalized" / f"{name}.jsonl").read_text().splitlines()]

        report = export()
        check(report["status"] == "complete" and report["discovered_changes"] == 4, "Authenticated export captures all four real reviews with one-item pagination")
        check({row["status"] for row in rows("changes")} == {"MERGED", "ABANDONED", "NEW"}, "Merged, abandoned, and open changes are included")
        check(any(row.get("is_private") or row.get("private") for row in rows("changes")), "Administrator export includes the private review")
        exported_comments = rows("comments")
        server_comments = []
        for change in results["changes"].values():
            for path, comments in admin.call("GET", f"/changes/{change}/comments").items():
                server_comments.extend((change, c["id"], c["message"], path) for c in comments)
        check(sorted((c["change_number"], c["id"], c["message"], c["path"]) for c in exported_comments) == sorted(server_comments),
              "Every published comment ID, body, and path matches the real Gerrit API")
        check("DRAFT_NOT_EXPORTED" not in json.dumps(exported_comments), "Unpublished drafts remain excluded")
        thread = next(t for t in rows("threads") if t["thread_id"] == root_comment["id"])
        check([c["patch_set"] for c in thread["comments"]] == [1, 2], "Reply thread spans both real patch sets")
        check(thread["comments"][-1]["unresolved"] is False, "Resolved reply state is preserved")
        check(any("日本語" in c["message"] and c.get("context_lines") for c in exported_comments), "Unicode and actual source context are preserved")
        check(any(m.get("tag") == "autogenerated:ci-test" for m in rows("messages")), "CI messages and tags are retained")
        check({c["author"]["_account_id"] for c in thread["comments"]} == {dev_account["_account_id"], reviewer_account["_account_id"]},
              "Developer and reviewer identities remain distinct")
        for sha, source in [(first_sha, "    return sum(values)"), (second_sha, "    return round(sum(values), 2)")]:
            code = execute(["git", "--git-dir", str(output / "code.git"), "show", sha + ":src/engine.py"])
            check(source in code, f"Original code is retrievable for patch set {1 if sha == first_sha else 2}")
        execute([sys.executable, str(ROOT / "gerrit_export.py"), "verify", "--output", str(output)], logfile=work / "verify.log")
        check(True, "Raw checksums, normalized checksums, patch checksums, and git fsck pass")
        snapshots_before = ge.load_state(output)
        report = export()
        check(report["reused"] == 4 and report["captured"] == 0, "Unchanged rerun reuses all four snapshots")
        reviewer.call("POST", f"/changes/{opened['_number']}/revisions/current/review", {"message": "Incremental review update"})
        report = export()
        check(report["captured"] == 1 and report["reused"] == 3, "Incremental rerun captures only the modified real review")
        check(any(m["message"].endswith("Incremental review update") for m in rows("messages")), "Incremental message is present in the normalized dataset")
        old_snapshot = output / snapshots_before["changes"][str(opened["_number"])]["snapshot"]
        check((old_snapshot / "detail.json").exists(), "Earlier raw snapshot survives an incremental update")
        anonymous = work / "anonymous-export"
        report = export(anonymous, admin, "--auth", "anonymous", "--metadata-only")
        check(report["discovered_changes"] == 3, "Anonymous API visibility excludes the private review")
        # Exercise a real Git transport failure, then resume the incomplete export.
        resumed = work / "resumed-export"
        report = export(resumed, admin, "--git-url", args.url + "/a/nonexistent-test-project", expected=1)
        check(report["status"] == "incomplete" and len(report["failures"]) == 4, "Real Git fetch failures produce an incomplete report")
        report = export(resumed)
        check(report["status"] == "complete" and report["captured"] == 4, "Rerun recovers all reviews after restoring the real Git remote")
        # Consolidate independent server counts for the saved evidence report.
        results["final_counts"] = ge.read_json(output / "export-report.json")["counts"]
        results["export_directory"] = str(output)
        results["status"] = "passed"
    except Exception as error:
        results["status"] = "failed"
        results["error"] = str(error)
        raise
    finally:
        results["finished_at"] = ge.now()
        ge.atomic(work / "real-gerrit-results.json", ge.encode(results))
        ge.atomic(args.output.resolve() / "latest-results.json", ge.encode(results))
        print("Real Gerrit results:", work / "real-gerrit-results.json", flush=True)


if __name__ == "__main__":
    main()
