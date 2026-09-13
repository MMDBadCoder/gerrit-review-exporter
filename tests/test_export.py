import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import gerrit_export as ge


def run_git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL).decode().strip()


class ExportIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "source"
        self.source.mkdir()
        run_git(self.source, "init")
        run_git(self.source, "config", "user.name", "Example Developer")
        run_git(self.source, "config", "user.email", "dev@example.test")
        (self.source / "code.py").write_text("value = 1\n")
        run_git(self.source, "add", ".")
        run_git(self.source, "commit", "-m", "Initial version")
        self.first = run_git(self.source, "rev-parse", "HEAD")
        (self.source / "code.py").write_text("value = 2\n")
        run_git(self.source, "commit", "-am", "Address review feedback")
        self.second = run_git(self.source, "rev-parse", "HEAD")
        for number, ps, sha in [(1, 1, self.first), (1, 2, self.second), (2, 1, self.first)]:
            run_git(self.source, "update-ref", f"refs/changes/{number:02d}/{number}/{ps}", sha)
        self.output = self.root / "export"
        self.details = {
            str(i): {"_number": i, "project": "team/project", "updated": "2025-01-01 00:00:00.000000000",
                     "meta_rev_id": str(i) * 40, "status": status, "subject": "A change",
                     "messages": [{"id": f"m{i}", "message": "Please explain the change", "date": "2025-01-01"}],
                     "revisions": {self.first: {"_number": 1}}}
            for i, status in [(1, "MERGED"), (2, "ABANDONED")]
        }
        self.details["1"]["revisions"][self.second] = {"_number": 2}
        self.comments = {"1": {"code.py": [
            {"id": "a", "message": "Pourquoi cette valeur? 日本語", "patch_set": 1,
             "line": 1, "updated": "2025-01-01", "unresolved": True,
             "author": {"_account_id": 8}, "context_lines": [{"line_number": 1, "context_line": "value = 1"}]},
            {"id": "b", "in_reply_to": "a", "message": "Fixed", "patch_set": 2,
             "updated": "2025-01-02", "unresolved": False}]}, "2": {}}
        self.requests = []
        self.fail_comments = set()
        self.retry_once = False
        self.context_unsupported = False
        self.version = "3.13.0"
        self.robots = {}
        self.change_during_capture = False
        self.redirect = False
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                url = urlsplit(self.path)
                path = url.path.removeprefix("/a")
                query = parse_qs(url.query)
                owner.requests.append((path, query, self.headers.get("Authorization")))
                if owner.redirect:
                    self.send_response(302)
                    self.send_header("Location", owner.url + "/unexpected")
                    self.end_headers()
                    return
                if path == "/config/server/version":
                    data = owner.version
                elif path == "/changes/":
                    if owner.retry_once:
                        owner.retry_once = False
                        self.send_response(429)
                        self.send_header("Retry-After", "0")
                        self.end_headers()
                        return
                    rows = [{k: d[k] for k in ("_number", "project", "updated")} for d in owner.details.values()]
                    rows.sort(key=lambda item: item["_number"], reverse=True)
                    start, size = int(query["S"][0]), int(query["n"][0])
                    data = rows[start:start + size]
                    if start + size < len(rows):
                        data[-1]["_more_changes"] = True
                else:
                    parts = path.strip("/").split("/")
                    if len(parts) != 3 or parts[1] not in owner.details:
                        self.send_error(404)
                        return
                    number, kind = parts[1:]
                    if kind == "detail":
                        data = copy.deepcopy(owner.details[number])
                    elif kind == "comments":
                        if number in owner.fail_comments:
                            self.send_error(503)
                            return
                        if owner.context_unsupported and "enable-context" in query:
                            self.send_error(400)
                            return
                        data = owner.comments[number]
                        if owner.change_during_capture and number == "1":
                            owner.change_during_capture = False
                            owner.details[number]["updated"] = "2025-02-01 00:00:00.000000000"
                            owner.details[number]["meta_rev_id"] = "f" * 40
                    elif kind == "robotcomments":
                        data = owner.robots
                    else:
                        self.send_error(404)
                        return
                body = b")]}'\n" + json.dumps(data, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def export(self, *extra):
        return ge.main(["export", "--url", self.url, "--project", "team/project",
                        "--output", str(self.output), "--auth", "anonymous",
                        "--git-url", str(self.source), "--page-size", "1",
                        "--delay", "0", "--retries", "0", *extra])

    def rows(self, name):
        return [json.loads(line) for line in (self.output / "normalized" / f"{name}.jsonl").read_text().splitlines()]

    def report(self):
        return json.loads((self.output / "export-report.json").read_bytes())

    def test_complete_capture_git_context_threads_and_resume(self):
        self.assertEqual(self.export(), 0)
        self.assertEqual(self.report()["discovered_changes"], 2)
        self.assertEqual({c["status"] for c in self.rows("changes")}, {"MERGED", "ABANDONED"})
        self.assertEqual(len(self.rows("messages")), 2)
        self.assertEqual(len(self.rows("comments")), 2)
        threads = self.rows("threads")
        self.assertEqual(len(threads), 1)
        self.assertEqual([c["patch_set"] for c in threads[0]["comments"]], [1, 2])
        self.assertIn("日本語", threads[0]["comments"][0]["message"])
        state = ge.read_json(self.output / "state.json")
        snapshot = self.output / state["changes"]["1"]["snapshot"]
        self.assertTrue((snapshot / "comments.json").read_bytes().startswith(b")]}'\n"))
        patches = state["changes"]["1"]["code"][1]["patches"]
        self.assertIn(b"+value = 2", (self.output / patches[0]["path"]).read_bytes())
        self.assertEqual(ge.main(["verify", "--output", str(self.output)]), 0)
        self.requests.clear()
        self.assertEqual(self.export(), 0)
        self.assertEqual(self.report()["reused"], 2)
        self.assertFalse(any(path.endswith("/comments") for path, _, _ in self.requests))
        self.assertEqual(len(self.rows("comments")), 2)

    def test_failure_is_reported_and_resume_captures_missing_review(self):
        self.fail_comments.add("1")
        self.assertEqual(self.export(), 1)
        self.assertEqual(self.report()["status"], "incomplete")
        self.assertEqual(self.report()["failures"][0]["change"], "1")
        self.assertEqual(len(self.rows("changes")), 1)
        self.fail_comments.clear()
        self.assertEqual(self.export(), 0)
        self.assertEqual(self.report()["reused"], 1)
        self.assertEqual(len(self.rows("changes")), 2)

    def test_incremental_preserves_previous_raw_snapshot(self):
        self.assertEqual(self.export(), 0)
        previous = ge.read_json(self.output / "state.json")["changes"]["1"]["snapshot"]
        self.details["1"]["updated"] = "2025-03-01 00:00:00.000000000"
        self.comments["1"]["code.py"].append({"id": "c", "in_reply_to": "b", "message": "Thanks", "updated": "2025-03-01"})
        self.assertEqual(self.export(), 0)
        self.assertEqual(self.report()["captured"], 1)
        self.assertEqual(len(self.rows("comments")), 3)
        self.assertEqual(len(ge.load_response(self.output / previous / "comments.json")["code.py"]), 2)

    def test_integrity_detects_modified_raw_data(self):
        self.assertEqual(self.export(), 0)
        state = ge.read_json(self.output / "state.json")
        raw = self.output / state["changes"]["1"]["snapshot"] / "comments.json"
        raw.write_text("{}")
        with self.assertRaisesRegex(ge.ExportError, "checksum"):
            ge.main(["verify", "--output", str(self.output)])

    def test_git_revision_mismatch_is_not_complete(self):
        run_git(self.source, "update-ref", "refs/changes/01/1/2", self.first)
        self.assertEqual(self.export(), 1)
        self.assertIn("changed during capture", self.report()["failures"][0]["error"])

    def test_rate_limit_and_context_fallback(self):
        self.retry_once, self.context_unsupported = True, True
        with patch("gerrit_export.time.sleep"):
            self.assertEqual(self.export("--retries", "1"), 0)
        state = ge.read_json(self.output / "state.json")
        self.assertFalse(state["changes"]["1"]["context_requested"])

    def test_legacy_robot_comments_and_orphan_threads(self):
        self.version = "3.12.4"
        self.robots = {"code.py": [{"id": "robot", "message": "lint", "robot_id": "lint", "in_reply_to": "missing"}]}
        self.assertEqual(self.export(), 0)
        self.assertEqual(sum(c["kind"] == "robot" for c in self.rows("comments")), 2)
        self.assertEqual(self.report()["counts"]["threads_with_gaps"], 2)

    def test_update_while_capturing_retries_and_requires_reconciliation(self):
        self.change_during_capture = True
        self.assertEqual(self.export(), 1)
        self.assertTrue(self.report()["inventory_changed_during_capture"])
        state = ge.read_json(self.output / "state.json")
        self.assertEqual(state["changes"]["1"]["meta_rev_id"], "f" * 40)
        self.assertEqual(self.export(), 0)

    def test_lost_visibility_keeps_existing_archive(self):
        self.assertEqual(self.export(), 0)
        del self.details["2"]
        self.assertEqual(self.export(), 0)
        self.assertEqual(self.report()["previously_captured_not_visible"], ["2"])
        self.assertFalse(next(c for c in self.rows("changes") if c["change_number"] == 2)["visible_in_last_discovery"])

    def test_metadata_only_can_be_upgraded_to_full_capture(self):
        self.assertEqual(self.export("--metadata-only"), 0)
        self.assertFalse((self.output / "code.git").exists())
        self.assertEqual(self.export(), 0)
        self.assertEqual(self.report()["captured"], 2)
        self.assertTrue((self.output / "code.git").exists())

    def test_auth_uses_environment_without_writing_credentials(self):
        with patch.dict(os.environ, {"GERRIT_USER": "example", "GERRIT_HTTP_PASSWORD": "secret-value"}):
            self.assertEqual(self.export("--auth", "basic"), 0)
        self.assertTrue(all(auth.startswith("Basic ") for _, _, auth in self.requests))
        for path in self.output.rglob("*.json"):
            self.assertNotIn(b"secret-value", path.read_bytes())

    def test_interruption_resumes_from_individual_checkpoint(self):
        original = ge.capture_change

        def interrupt_second(client, args, root, number, run):
            if number == "2":
                raise KeyboardInterrupt()
            return original(client, args, root, number, run)

        with patch("gerrit_export.capture_change", side_effect=interrupt_second):
            self.assertEqual(self.export(), 1)
        self.assertEqual(self.report()["status"], "interrupted")
        self.assertTrue((self.output / "checkpoints" / "1.json").exists())
        self.assertEqual(self.export(), 0)
        self.assertEqual(self.report()["reused"], 1)

    def test_redirect_is_not_followed_with_authentication(self):
        self.redirect = True
        with patch.dict(os.environ, {"GERRIT_USER": "example", "GERRIT_HTTP_PASSWORD": "secret-value"}):
            self.assertEqual(self.export("--auth", "basic"), 1)
        self.assertEqual(len(self.requests), 1)
        self.assertIn("HTTP 302", self.report()["failures"][0]["error"])

    def test_normalized_corruption_is_detected(self):
        self.assertEqual(self.export(), 0)
        (self.output / "normalized" / "comments.jsonl").write_text("")
        with self.assertRaisesRegex(ge.ExportError, "Normalized dataset checksum"):
            ge.main(["verify", "--output", str(self.output)])

    def test_refresh_does_not_accept_corrupted_patch(self):
        self.assertEqual(self.export(), 0)
        patch_file = next((self.output / "context").glob("*/parent-1.patch"))
        patch_file.write_text("corrupted")
        self.assertEqual(self.export("--refresh"), 1)
        self.assertIn("Stored patch differs", self.report()["failures"][0]["error"])

    def test_cyclic_replies_are_grouped_and_flagged(self):
        self.comments["1"] = {"code.py": [
            {"id": "a", "in_reply_to": "b", "message": "enters cycle"},
            {"id": "b", "in_reply_to": "c", "message": "cycle"},
            {"id": "c", "in_reply_to": "b", "message": "cycle"}]}
        self.assertEqual(self.export(), 0)
        self.assertEqual(len(self.rows("threads")), 1)
        self.assertTrue(self.rows("threads")[0]["reply_cycle"])

    def test_malformed_comments_fail_with_report(self):
        self.comments["1"] = {"code.py": [{"message": "missing ID"}]}
        self.assertEqual(self.export(), 1)
        self.assertIn("Missing or duplicate comment ID", self.report()["failures"][0]["error"])

    def test_merge_and_binary_patches_preserve_each_parent(self):
        run_git(self.source, "checkout", "--detach", self.first)
        (self.source / "data.bin").write_bytes(b"\x00\x01binary\xff")
        run_git(self.source, "add", ".")
        run_git(self.source, "commit", "-m", "Binary asset")
        other = run_git(self.source, "rev-parse", "HEAD")
        run_git(self.source, "checkout", "--detach", self.second)
        run_git(self.source, "merge", "--no-ff", other, "-m", "Merge asset")
        merge = run_git(self.source, "rev-parse", "HEAD")
        run_git(self.source, "update-ref", "refs/changes/01/1/3", merge)
        self.details["1"]["revisions"][merge] = {"_number": 3}
        self.assertEqual(self.export(), 0)
        revision = next(row for row in self.rows("revisions") if row["commit"] == merge)
        self.assertEqual(len(revision["parents"]), 2)
        self.assertEqual(len(revision["patches"]), 2)
        self.assertIn(b"GIT binary patch", (self.output / revision["patches"][0]["path"]).read_bytes())

    def test_discovery_repeated_page_is_rejected(self):
        class RepeatingClient:
            def get(self, *args):
                return [{"_number": 1, "project": "team/project", "updated": "today", "_more_changes": True}]

        args = ge.parser().parse_args(["export", "--url", self.url, "--project", "team/project"])
        with self.assertRaisesRegex(ge.ExportError, "repeated a page"):
            ge.discover(RepeatingClient(), args, self.root)


class UnitTests(unittest.TestCase):
    def test_equal_timestamp_reply_follows_parent_despite_uuid_order(self):
        comments = [
            {"id": "0reply", "in_reply_to": "5parent", "updated": "2026-09-13 04:02:04.000000000", "patch_set": 2},
            {"id": "5parent", "updated": "2026-09-13 04:02:04.000000000", "patch_set": 1},
        ]
        self.assertEqual([c["patch_set"] for c in ge.order_thread(comments)], [1, 2])

    def test_html_login_is_not_treated_as_empty_export(self):
        with self.assertRaises(ge.ExportError):
            ge.parse_response(b"<html>Log in</html>")

    def test_unsafe_git_url_rejected(self):
        for url in ["https://user:password@example.com/repo", "--upload-pack=anything", "https://example.com/repo?token=secret"]:
            with self.assertRaises(ge.ExportError):
                ge.check_git_url(url)


if __name__ == "__main__":
    unittest.main()
