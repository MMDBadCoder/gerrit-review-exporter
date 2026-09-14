#!/usr/bin/env python3
"""Unit tests for the .rv exporter.

The Git tests use a real repository rather than a stub: `Archive` fetches from a
remote, and a local path is a perfectly good remote, so the fetch, the archive
refs and `git show` are all genuinely exercised.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import gerrit_rv as rv


def run(cwd, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@e",
           "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@e", "LC_ALL": "C"}
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=env)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {result.stderr.decode()}")
    return result.stdout.decode().strip()


class Repo:
    """A source repo whose commits are published as Gerrit patch set refs."""

    def __init__(self, directory, change_number=4242):
        self.path = Path(directory)
        self.path.mkdir(parents=True, exist_ok=True)
        self.change = change_number
        self.shas = {}
        run(self.path, "init", "-q", "-b", "main", ".")

    def patchset(self, number, files, delete=()):
        for name, content in files.items():
            target = self.path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            run(self.path, "add", name)
        for name in delete:
            run(self.path, "rm", "-q", name)
        run(self.path, "commit", "-q", "-m", f"patch set {number}", "--allow-empty")
        sha = run(self.path, "rev-parse", "HEAD")
        ref = f"refs/changes/{self.change % 100:02d}/{self.change}/{number}"
        run(self.path, "update-ref", ref, sha)
        self.shas[number] = sha
        return sha


def comment(cid, patch_set, path="src/app.py", line=None, message="", parent=None,
            unresolved=None, author="Reviewer", updated="2026-09-01 10:00:00.000000000",
            span=None):
    item = {"id": cid, "patch_set": patch_set, "path": path, "message": message,
            "updated": updated, "author": {"name": author}}
    if line is not None:
        item["line"] = line
    if span is not None:
        item["range"] = span
    if parent:
        item["in_reply_to"] = parent
    if unresolved is not None:
        item["unresolved"] = unresolved
    return item


def detail(shas, project="demo", number=4242, subject="Add totals"):
    return {
        "_number": number, "project": project, "branch": "main", "status": "MERGED",
        "subject": subject, "change_id": "I0123456789", "owner": {"name": "Ali R."},
        "revisions": {sha: {"_number": ps, "kind": "REWORK"} for ps, sha in shas.items()},
        "_url": "http://gerrit.example/c/demo/+/4242",
    }


# --- line mapping ----------------------------------------------------------

class LineMapping(unittest.TestCase):
    def test_pure_insertion_does_not_move_the_line_above_it(self):
        # `@@ -10,0 +11,3 @@` inserts AFTER old line 10, so line 10 is untouched.
        hunks = rv.parse_hunks("@@ -10,0 +11,3 @@\n")
        self.assertEqual(rv.map_line(10, hunks), (10, False))
        self.assertEqual(rv.map_line(11, hunks), (14, False))

    def test_replaced_line_reports_the_region_that_replaced_it(self):
        hunks = rv.parse_hunks("@@ -42,1 +42,4 @@\n")
        self.assertEqual(rv.map_line(42, hunks), (42, True))

    def test_deleted_lines_map_to_the_gap_and_shift_what_follows(self):
        hunks = rv.parse_hunks("@@ -20,3 +19,0 @@\n")
        self.assertEqual(rv.map_line(21, hunks), (19, True))
        self.assertEqual(rv.map_line(30, hunks), (27, False))

    def test_offsets_accumulate_across_several_hunks(self):
        hunks = rv.parse_hunks("@@ -10,0 +11,3 @@\n@@ -42,1 +46,4 @@\n")
        self.assertEqual(rv.map_line(5, hunks), (5, False))
        self.assertEqual(rv.map_line(43, hunks), (49, False))
        self.assertEqual(rv.map_line(100, hunks), (106, False))

    def test_single_line_hunks_omit_the_count(self):
        self.assertEqual(rv.parse_hunks("@@ -7 +7 @@\n"), [(7, 1, 7, 1)])

    def test_range_widens_to_cover_a_hunk_inside_it(self):
        hunks = rv.parse_hunks("@@ -12,2 +12,6 @@\n")
        start, end, touched = rv.map_range(10, 20, hunks)
        self.assertTrue(touched)
        self.assertLessEqual(start, 12)
        self.assertGreaterEqual(end, 17)

    def test_range_far_from_any_hunk_is_untouched(self):
        hunks = rv.parse_hunks("@@ -80,1 +80,2 @@\n")
        self.assertEqual(rv.map_range(10, 12, hunks), (10, 12, False))


class CommentSpan(unittest.TestCase):
    def test_range_ending_at_character_zero_excludes_that_line(self):
        span = {"start_line": 4, "end_line": 7, "end_character": 0}
        self.assertEqual(rv.comment_span({"range": span}), (4, 6))

    def test_range_ending_mid_line_includes_it(self):
        span = {"start_line": 4, "end_line": 7, "end_character": 9}
        self.assertEqual(rv.comment_span({"range": span}), (4, 7))

    def test_plain_line_and_file_level(self):
        self.assertEqual(rv.comment_span({"line": 12}), (12, 12))
        self.assertIsNone(rv.comment_span({}))


# --- threads ---------------------------------------------------------------

class Threads(unittest.TestCase):
    def test_reply_on_a_later_patch_set_stays_in_one_thread(self):
        # The author answers on patch set 5 a comment written on patch set 2.
        comments = {"src/app.py": [
            comment("a", 2, message="rename this"),
            comment("b", 5, message="done", parent="a"),
        ]}
        threads = rv.group_threads(comments)
        self.assertEqual(len(threads), 1)
        self.assertEqual([c["id"] for c in threads[0]["comments"]], ["a", "b"])

    def test_threads_are_not_split_by_line_movement(self):
        # Gerrit moves a comment's line as the file changes; same thread regardless.
        comments = {"src/app.py": [
            comment("a", 2, line=10),
            comment("b", 3, line=88, parent="a"),
        ]}
        self.assertEqual(len(rv.group_threads(comments)), 1)

    def test_separate_roots_are_separate_threads(self):
        comments = {"src/app.py": [comment("a", 1), comment("b", 1)]}
        self.assertEqual(len(rv.group_threads(comments)), 2)

    def test_reply_cycle_is_flagged_and_keeps_every_comment(self):
        comments = {"src/app.py": [
            comment("a", 1, parent="b"),
            comment("b", 1, parent="a"),
        ]}
        threads = rv.group_threads(comments)
        self.assertEqual(len(threads), 1)
        self.assertTrue(threads[0]["reply_cycle"])
        self.assertEqual(len(threads[0]["comments"]), 2)

    def test_reply_to_an_invisible_parent_is_kept_and_flagged(self):
        comments = {"src/app.py": [comment("b", 1, parent="hidden")]}
        threads = rv.group_threads(comments)
        self.assertTrue(threads[0]["orphan_root"])
        self.assertEqual(len(threads[0]["comments"]), 1)

    def test_parent_precedes_reply_even_when_timestamps_tie(self):
        same = "2026-09-01 10:00:00.000000000"
        comments = {"src/app.py": [
            comment("zzz", 1, updated=same),
            comment("aaa", 1, parent="zzz", updated=same),
        ]}
        ordered = rv.group_threads(comments)[0]["comments"]
        self.assertEqual([c["id"] for c in ordered], ["zzz", "aaa"])

    def test_comments_on_two_files_are_two_threads(self):
        comments = {"a.py": [comment("a", 1, path="a.py")],
                    "b.py": [comment("b", 1, path="b.py")]}
        self.assertEqual(len(rv.group_threads(comments)), 2)


class Resolution(unittest.TestCase):
    def test_last_comment_decides_resolved_state(self):
        thread = {"comments": [comment("a", 1, unresolved=True), comment("b", 2, unresolved=False)]}
        self.assertTrue(rv.thread_is_resolved(thread))
        thread = {"comments": [comment("a", 1, unresolved=False), comment("b", 2, unresolved=True)]}
        self.assertFalse(rv.thread_is_resolved(thread))

    def test_absent_flag_counts_as_resolved(self):
        self.assertTrue(rv.thread_is_resolved({"comments": [comment("a", 1)]}))

    def test_anchor_is_the_first_comment_not_the_newest(self):
        thread = {"comments": [comment("a", 3, line=10), comment("b", 11, line=90, parent="a")]}
        anchor = rv.thread_anchor(thread)
        self.assertEqual(anchor["patchset"], 3)
        self.assertEqual(anchor["line"], 10)


# --- formatting ------------------------------------------------------------

class Formatting(unittest.TestCase):
    def test_change_message_becomes_one_safe_csv_field(self):
        value = rv.csv_safe('Fix "race"\nin refresh\ttoken')
        self.assertEqual(value, '"Fix ""race"" in refresh token"')
        self.assertNotIn("\n", value)

    def test_metadata_quotes_only_when_needed(self):
        self.assertEqual(rv.meta_field("file", "src/a.py"), "file=src/a.py")
        self.assertEqual(rv.meta_field("owner", "Ali R."), 'owner="Ali R."')

    def test_code_is_numbered_from_the_right_line(self):
        rendered = rv.render_code(["a", "b"], 9)
        self.assertEqual(rendered.splitlines()[0].strip(), "9  a".strip())
        self.assertIn("10  b", rendered)

    def test_slice_clamps_a_line_past_the_end_of_the_file(self):
        lines, first = rv.slice_lines("one\ntwo\n", 900, 900, 3, 50)
        self.assertEqual(first, 1)
        self.assertEqual(lines, ["one", "two"])

    def test_slice_respects_the_maximum(self):
        text = "\n".join(str(i) for i in range(1, 200))
        lines, _ = rv.slice_lines(text, 50, 150, 5, 20)
        self.assertEqual(len(lines), 20)

    def test_filename_is_stable_and_sortable(self):
        record = {"thread_id": "abc-123", "change_number": 42, "anchor": {"patchset": 3}}
        self.assertEqual(rv.rv_filename(record, ".rv"), "0000042-ps03-abc123.rv")


# --- configuration ---------------------------------------------------------

class Config(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "c.json"
        self.addCleanup(self.dir.cleanup)

    def write(self, data):
        self.path.write_text(json.dumps(data), encoding="utf-8")
        return str(self.path)

    def test_defaults_fill_in_and_values_override(self):
        config = rv.load_config(self.write({"workers": 9, "filters": {"projects": ["p"]}}))
        self.assertEqual(config["workers"], 9)
        self.assertEqual(config["filters"]["projects"], ["p"])
        self.assertEqual(config["filters"]["status"], "merged")

    def test_a_mistyped_key_is_rejected_rather_than_ignored(self):
        with self.assertRaises(rv.RvError) as caught:
            rv.load_config(self.write({"filters": {"project": ["p"]}}))
        self.assertIn("project", str(caught.exception))

    def test_invalid_values_are_rejected(self):
        for bad in ({"workers": 0}, {"filters": {"status": "sideways"}},
                    {"limits": {"max_files": 0}}, {"filters": {"recent_days": -3}},
                    {"gerrit": {"auth": "magic"}}):
            with self.assertRaises(rv.RvError):
                rv.load_config(self.write(bad))

    def test_null_limits_mean_no_limit(self):
        config = rv.load_config(self.write({"limits": {"max_files": None}}))
        self.assertIsNone(config["limits"]["max_files"])

    def test_the_generated_config_is_valid(self):
        rv.validate_config(rv.merge_defaults(rv.DEFAULT_CONFIG, json.loads(json.dumps(rv.DEFAULT_CONFIG))))


class Query(unittest.TestCase):
    def base(self, **overrides):
        return {**rv.DEFAULT_CONFIG["filters"], **overrides}

    def test_empty_lists_do_not_restrict(self):
        query = rv.build_query(self.base(projects=[], reviewers=[], branches=[], recent_days=None))
        self.assertEqual(query, "status:merged")

    def test_several_projects_become_an_or_group(self):
        query = rv.build_query(self.base(projects=["a", "b"]))
        self.assertIn("(project:a OR project:b)", query)

    def test_reviewers_and_recency_are_included(self):
        query = rv.build_query(self.base(reviewers=["sara@example.com"], recent_days=14))
        self.assertIn("reviewer:sara@example.com", query)
        self.assertIn("-age:14d", query)

    def test_values_needing_quotes_are_quoted(self):
        self.assertIn('project:"my project"', rv.build_query(self.base(projects=["my project"])))

    def test_status_any_is_omitted(self):
        self.assertNotIn("status:", rv.build_query(self.base(status="any", recent_days=None)))


class Limits(unittest.TestCase):
    def test_budget_stops_at_the_limit_and_reports_which_one(self):
        budget = rv.Budget({"max_files": 2, "max_comments": None})
        self.assertTrue(budget.take("max_files"))
        self.assertTrue(budget.take("max_files"))
        self.assertFalse(budget.take("max_files"))
        self.assertEqual(budget.reason, "limits.max_files reached")

    def test_a_group_larger_than_the_remaining_budget_is_not_partially_taken(self):
        budget = rv.Budget({"max_comments": 3})
        self.assertFalse(budget.take("max_comments", 4))
        self.assertEqual(budget.used["max_comments"], 0)

    def test_null_limit_never_stops(self):
        budget = rv.Budget({"max_files": None})
        for _ in range(1000):
            self.assertTrue(budget.take("max_files"))
        self.assertIsNone(budget.reason)

    def test_once_stopped_every_further_take_fails(self):
        budget = rv.Budget({"max_files": 1, "max_comments": 100})
        budget.take("max_files")
        budget.take("max_files")
        self.assertFalse(budget.take("max_comments"))


if __name__ == "__main__":
    unittest.main()


# --- end to end over a real repository -------------------------------------

class Scenarios(unittest.TestCase):
    """The irregular shapes a real review takes.

    Reviews are not one round of comments on one patch set. An author pushes
    eight patch sets before anyone looks, a reviewer comments, the author pushes
    four more, and a second round lands on top of that.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = Repo(self.root / "source")
        self.config = rv.validate_config(rv.merge_defaults(rv.DEFAULT_CONFIG, {}))

    def archive(self):
        return rv.Archive(self.root / "code.git", str(self.source.path))

    def build(self, thread_comments, **enrich):
        self.config["enrich"].update(enrich)
        threads = rv.group_threads({thread_comments[0]["path"]: thread_comments})
        self.assertEqual(len(threads), 1, "fixture should produce exactly one thread")
        return rv.build_record(self.archive(), detail(self.source.shas), threads[0], self.config)

    def test_comment_mid_stream_is_answered_by_the_next_patch_set_that_touches_the_file(self):
        # Patch sets 1-8 with no review, a comment on 3, work continues to 12.
        # Only patch set 9 touches the file; 4-8 and 10-12 change something else.
        for number in range(1, 9):
            self.source.patchset(number, {"src/app.py": "def total(v):\n    return sum(v)\n",
                                          "other.py": f"# {number}\n"})
        self.source.patchset(9, {"src/app.py": "def total(v):\n    return round(sum(v), 2)\n"})
        for number in range(10, 13):
            self.source.patchset(number, {"other.py": f"# {number}\n"})

        record = self.build([comment("a", 3, line=2, message="round this"),
                             comment("b", 9, message="done", parent="a")])

        self.assertEqual(record["anchor"]["patchset"], 3, "anchored where the reviewer pointed")
        self.assertEqual(record["resolved_in"], 9, "first later patch set that touched the file")
        self.assertTrue(record["code_changed"])
        self.assertIn("return sum(v)", record["before_code"])
        self.assertIn("round(sum(v), 2)", record["after_code"])

    def test_two_review_rounds_on_one_change_resolve_independently(self):
        self.source.patchset(1, {"src/app.py": "a = 1\nb = 2\nc = 3\n"})
        self.source.patchset(2, {"src/app.py": "a = 10\nb = 2\nc = 3\n"})
        self.source.patchset(3, {"src/app.py": "a = 10\nb = 2\nc = 30\n"})

        first = self.build([comment("r1", 1, line=1, message="a is wrong")])
        second = self.build([comment("r2", 2, line=3, message="c is wrong too")])

        self.assertEqual(first["resolved_in"], 2)
        self.assertEqual(second["resolved_in"], 3)
        self.assertIn("a = 1", first["before_code"])
        self.assertIn("a = 10", first["after_code"])

    def test_thread_answered_in_words_reports_no_code_change(self):
        self.source.patchset(1, {"src/app.py": "x = 1\n"})
        self.source.patchset(2, {"other.py": "y = 2\n"})

        record = self.build([comment("a", 1, line=1, message="why?"),
                             comment("b", 2, message="legacy, leaving as is", parent="a")])

        self.assertIsNone(record["resolved_in"])
        self.assertFalse(record["code_changed"])
        self.assertIn("answered in discussion", record["after_header"])
        self.assertIn("latest patch set is 2", record["after_header"])

    def test_reply_only_threads_can_be_excluded(self):
        self.source.patchset(1, {"src/app.py": "x = 1\n"})
        self.source.patchset(2, {"other.py": "y = 2\n"})
        record = self.build([comment("a", 1, line=1, message="why?")], include_reply_only=False)
        self.assertIsNone(record)

    def test_file_deleted_by_the_resolving_patch_set(self):
        self.source.patchset(1, {"src/app.py": "x = 1\n", "keep.py": "k = 1\n"})
        self.source.patchset(2, {"keep.py": "k = 2\n"}, delete=["src/app.py"])

        record = self.build([comment("a", 1, line=1, message="just delete this")])
        self.assertEqual(record["resolved_in"], 2)
        self.assertIn("deleted", record["after_header"])
        self.assertEqual(record["after_code"], "")

    def test_file_added_by_the_patch_set_being_reviewed(self):
        self.source.patchset(1, {"other.py": "o = 1\n"})
        self.source.patchset(2, {"src/new.py": "n = 1\n"})
        self.source.patchset(3, {"src/new.py": "n = 2\n"})

        record = self.build([comment("a", 2, path="src/new.py", line=1, message="use 2")])
        self.assertIn("n = 1", record["before_code"])
        self.assertIn("n = 2", record["after_code"])

    def test_file_level_comment_has_no_line_and_shows_the_head_of_the_file(self):
        body = "".join(f"line{i}\n" for i in range(1, 40))
        self.source.patchset(1, {"src/app.py": body})
        self.source.patchset(2, {"src/app.py": body.replace("line1\n", "line one\n")})

        record = self.build([comment("a", 1, message="this file needs a docstring")])
        self.assertIn("whole-file comment", record["before_header"])
        self.assertIn("line1", record["before_code"])

    def test_comment_on_the_commit_message(self):
        self.source.patchset(1, {"src/app.py": "x = 1\n"})
        record = self.build([comment("a", 1, path=rv.COMMIT_MSG, line=1, message="bad subject")])
        self.assertIsNotNone(record)
        self.assertIn("patch set 1", record["before_code"] and record["before_header"])
        self.assertIn("patch set 1", record["before_header"])

    def test_commit_message_comments_can_be_excluded(self):
        self.source.patchset(1, {"src/app.py": "x = 1\n"})
        record = self.build([comment("a", 1, path=rv.COMMIT_MSG, message="x")],
                            include_commit_message_comments=False)
        self.assertIsNone(record)

    def test_patchset_level_comments_are_never_rendered(self):
        self.source.patchset(1, {"src/app.py": "x = 1\n"})
        record = self.build([comment("a", 1, path=rv.PATCHSET_LEVEL, message="LGTM overall")])
        self.assertIsNone(record)

    def test_unresolved_threads_are_skipped_unless_requested(self):
        self.source.patchset(1, {"src/app.py": "x = 1\n"})
        thread = [comment("a", 1, line=1, message="still wrong", unresolved=True)]
        self.assertIsNone(self.build(list(thread)))
        self.assertIsNotNone(self.build(list(thread), include_unresolved=True))

    def test_binary_file_is_reported_rather_than_rendered(self):
        (self.source.path / "logo.png").write_bytes(bytes(range(256)) * 4)
        run(self.source.path, "add", "logo.png")
        run(self.source.path, "commit", "-q", "-m", "ps1")
        sha = run(self.source.path, "rev-parse", "HEAD")
        run(self.source.path, "update-ref", f"refs/changes/42/4242/1", sha)
        self.source.shas[1] = sha

        record = self.build([comment("a", 1, path="logo.png", message="optimise this")])
        self.assertIn("binary", record["before_header"])

    def test_a_line_beyond_the_end_of_the_file_still_renders(self):
        self.source.patchset(1, {"src/app.py": "one\ntwo\n"})
        self.source.patchset(2, {"src/app.py": "one\nTWO\n"})
        record = self.build([comment("a", 1, line=900, message="stale line number")])
        self.assertIn("one", record["before_code"])

    def test_unchanged_region_is_labelled_as_not_edited(self):
        self.source.patchset(1, {"src/app.py": "".join(f"l{i}\n" for i in range(1, 60))})
        body = "".join(f"l{i}\n" for i in range(1, 60)).replace("l59\n", "l59 changed\n")
        self.source.patchset(2, {"src/app.py": body})
        record = self.build([comment("a", 1, line=2, message="unrelated to the edit")])
        self.assertEqual(record["resolved_in"], 2)
        self.assertFalse(record["code_changed"])
        self.assertIn("were not edited", record["after_header"])

    def test_rendered_document_has_the_agreed_shape(self):
        self.source.patchset(1, {"src/app.py": "def total(v):\n    return sum(v)\n"})
        self.source.patchset(2, {"src/app.py": "def total(v):\n    return round(sum(v), 2)\n"})
        record = self.build([comment("a", 1, line=2, message="round it", author="Sara M."),
                             comment("b", 2, message="done", parent="a", author="Ali R.")])
        text = rv.render_rv(record, self.config["enrich"])
        lines = text.splitlines()

        self.assertTrue(lines[0].startswith("id="))
        self.assertIn("change=4242", lines[0])
        self.assertIn("patchset=1", lines[0])
        self.assertIn("resolved_in=2", lines[0])
        self.assertIn("file=src/app.py", lines[0])
        self.assertTrue(lines[1].startswith('"Add totals"'))
        self.assertIn('owner="Ali R."', lines[1])
        self.assertEqual(lines[2], "", "blank line after the two metadata lines")

        # Neither metadata line may contain a raw newline or an unbalanced quote.
        for line in lines[:2]:
            self.assertNotIn("\n", line)
            self.assertEqual(line.count('"') % 2, 0)

        self.assertIn("--- before", text)
        self.assertIn("--- review", text)
        self.assertIn("--- after", text)
        self.assertIn("[Sara M. · ps1]", text)
        self.assertIn("[Ali R. · ps2]", text)
        self.assertLess(text.index("--- before"), text.index("--- review"))
        self.assertLess(text.index("--- review"), text.index("--- after"))

    def test_archive_refetch_is_a_no_op(self):
        self.source.patchset(1, {"src/app.py": "x = 1\n"})
        archive = self.archive()
        first = archive.fetch(4242, 1, self.source.shas[1])
        second = archive.fetch(4242, 1, self.source.shas[1])
        self.assertEqual(first, second)
        self.assertEqual(archive.read_file(self.source.shas[1], "src/app.py"), "x = 1\n")

    def test_excluded_paths_are_skipped(self):
        self.source.patchset(1, {"vendor/lib.py": "x = 1\n"})
        self.config["filters"]["exclude_paths"] = ["vendor/*"]
        record = self.build([comment("a", 1, path="vendor/lib.py", line=1, message="x")])
        self.assertIsNone(record)


class HttpCredentials(unittest.TestCase):
    """Git must authenticate with the same HTTP credential the REST client uses.

    Without this the REST half authenticates and the Git half does not, so a run
    configured with nothing but a Gerrit HTTP password discovers changes and then
    fails to fetch a single patch set.
    """

    def basic(self, **overrides):
        return {**rv.DEFAULT_CONFIG["gerrit"], "auth": "basic", **overrides}

    def test_http_remote_gets_a_credential_helper(self):
        config = rv.http_credential_config(self.basic(), "https://review.example.com/a/p")
        self.assertEqual(len(config), 1)
        self.assertTrue(config[0].startswith("credential.helper="))

    def test_the_helper_names_the_variables_and_never_holds_the_secret(self):
        # The snippet reaches Git on the command line, which `ps` can read.
        config = rv.http_credential_config(self.basic(), "http://review.example.com/a/p")
        self.assertIn("$GERRIT_USER", config[0])
        self.assertIn("$GERRIT_HTTP_PASSWORD", config[0])
        self.assertNotIn("secret", config[0])

    def test_configured_variable_names_are_honoured(self):
        config = rv.http_credential_config(
            self.basic(user_env="MY_USER", password_env="MY_PASS"), "https://h/p")
        self.assertIn("$MY_USER", config[0])
        self.assertIn("$MY_PASS", config[0])

    def test_ssh_remotes_are_left_alone(self):
        self.assertEqual(rv.http_credential_config(self.basic(), "ssh://h:29418/p"), ())

    def test_anonymous_and_token_auth_add_nothing(self):
        for auth in ("anonymous", "bearer", "netrc"):
            settings = {**rv.DEFAULT_CONFIG["gerrit"], "auth": auth}
            self.assertEqual(rv.http_credential_config(settings, "https://h/p"), ())

    def test_git_passes_the_config_to_the_command(self):
        # -c settings must precede the subcommand or Git ignores them.
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory) / "bare.git"
            rv.git(repo, "init", "--bare", "-q", str(repo))
            result = rv.git(repo, "config", "--get", "rv.marker", check=False,
                            config=("rv.marker=applied",))
            self.assertEqual(result.stdout.decode().strip(), "applied")
