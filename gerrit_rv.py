#!/usr/bin/env python3
"""Turn Gerrit review discussions into `.rv` learning files.

One file per resolved review comment thread. Each file holds the code as the
reviewer saw it, the discussion itself, and the code after the author acted on
it -- the three things needed to learn what a good review looks like.

Python 3.10+ and Git. No pip packages. Every REST call is a GET and every Git
operation fetches into a local bare archive: nothing on Gerrit is modified.
"""
import argparse
import base64
import collections
import concurrent.futures
import copy
import datetime as dt
import email.utils
import fnmatch
import hashlib
import heapq
import http.client
import json
import netrc
import os
import re
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

__version__ = "0.2.1"

# Gerrit's synthetic paths. Neither names a file in the tree, so neither has
# code to show; they still carry review discussion worth keeping.
COMMIT_MSG = "/COMMIT_MSG"
PATCHSET_LEVEL = "/PATCHSET_LEVEL"
SYNTHETIC = (COMMIT_MSG, PATCHSET_LEVEL)


class RvError(Exception):
    """Anything that should stop the run with a readable message."""


class HTTPFailure(RvError):
    def __init__(self, code):
        super().__init__(f"Gerrit returned HTTP {code}")
        self.code = code


# --- configuration ---------------------------------------------------------

DEFAULT_CONFIG = {
    "gerrit": {
        "url": "http://localhost:8080",
        "git_url": None,
        "auth": "anonymous",
        "user_env": "GERRIT_USER",
        "password_env": "GERRIT_HTTP_PASSWORD",
        "token_env": "GERRIT_TOKEN",
        "netrc_file": None,
        "ca_file": None,
        "timeout": 30,
        "retries": 3,
        "delay": 0.0,
    },
    "filters": {
        # Empty list means "no filter" for every one of these.
        "projects": [],
        "reviewers": [],
        "branches": [],
        "status": "merged",
        "recent_days": 90,
        "exclude_paths": [],
    },
    "limits": {
        # Any limit reached stops the export; whatever was written stays valid.
        "max_changes": 200,
        "max_comments": 1000,
        "max_files": 1000,
    },
    "workers": 4,
    "output": {
        "dir": "rv-out",
        "extension": ".rv",
        "overwrite": True,
    },
    "enrich": {
        "context_lines": 8,
        "max_code_lines": 120,
        "include_unresolved": False,
        "include_reply_only": True,
        "include_commit_message_comments": True,
        "author_names": True,
        "max_comment_chars": 4000,
    },
}


def merge_defaults(defaults, supplied, path=""):
    """Config values override defaults key by key; unknown keys are an error.

    A silently ignored typo in a config file is a setting that appears to work
    and does not, which is the failure mode this whole file is meant to avoid.
    """
    result = {}
    for key, fallback in defaults.items():
        where = f"{path}.{key}" if path else key
        if key not in supplied:
            # Deep copy, not the default object itself. Sharing it would make a
            # caller's edit - `config["output"]["dir"] = ...` - rewrite the
            # module-level defaults for the rest of the process.
            result[key] = copy.deepcopy(fallback)
        elif isinstance(fallback, dict):
            if not isinstance(supplied[key], dict):
                raise RvError(f"Config key {where} must be an object")
            result[key] = merge_defaults(fallback, supplied[key], where)
        else:
            result[key] = supplied[key]
    unknown = sorted(set(supplied) - set(defaults))
    if unknown:
        known = ", ".join(sorted(defaults))
        raise RvError(f"Unknown config key(s) under {path or 'root'}: {', '.join(unknown)}. Known: {known}")
    return result


def validate_config(config):
    """Rejects a configuration that cannot produce sensible output."""
    gerrit, filters = config["gerrit"], config["filters"]
    limits, enrich = config["limits"], config["enrich"]

    if not gerrit["url"]:
        raise RvError("gerrit.url is required")
    if gerrit["auth"] not in ("anonymous", "basic", "bearer", "netrc"):
        raise RvError("gerrit.auth must be anonymous, basic, bearer or netrc")
    if filters["status"] not in ("merged", "open", "abandoned", "any"):
        raise RvError("filters.status must be merged, open, abandoned or any")
    for key in ("recent_days",):
        if filters[key] is not None and int(filters[key]) <= 0:
            raise RvError(f"filters.{key} must be a positive number of days, or null for no limit")
    for key, value in limits.items():
        if value is not None and int(value) <= 0:
            raise RvError(f"limits.{key} must be positive, or null for no limit")
    if int(config["workers"]) < 1:
        raise RvError("workers must be at least 1")
    if int(enrich["context_lines"]) < 0:
        raise RvError("enrich.context_lines cannot be negative")
    if int(enrich["max_code_lines"]) < 1:
        raise RvError("enrich.max_code_lines must be at least 1")
    return config


def load_config(path):
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RvError(f"Config file not found: {path}. Run `init-config` to write a starting point.") from None
    except json.JSONDecodeError as exc:
        raise RvError(f"Config file is not valid JSON: {exc}") from None
    if not isinstance(raw, dict):
        raise RvError("Config file must contain a JSON object")
    return validate_config(merge_defaults(DEFAULT_CONFIG, raw))


# --- Gerrit REST -----------------------------------------------------------

def parse_response(raw):
    """Gerrit prefixes JSON bodies with )]}' to make them non-executable."""
    text = raw.decode("utf-8")
    if text.startswith(")]}'"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    return json.loads(text) if text.strip() else None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect from an API call means a login page, not data."""

    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, settings):
        self.base = settings["url"].rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise RvError("gerrit.url must be an HTTP(S) Gerrit base URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise RvError("Use a base URL without credentials, query or fragment")
        if self.base.endswith("/a"):
            raise RvError("Use the Gerrit base URL without the /a authentication prefix")

        auth = settings["auth"]
        self.headers = {"Accept": "application/json", "User-Agent": f"gerrit-rv/{__version__}"}
        if auth == "basic":
            user, password = os.getenv(settings["user_env"]), os.getenv(settings["password_env"])
            if not user or not password:
                raise RvError(f"Set {settings['user_env']} and {settings['password_env']}, or use another auth mode")
            self.headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        elif auth == "bearer":
            token = os.getenv(settings["token_env"])
            if not token:
                raise RvError(f"Set {settings['token_env']}")
            self.headers["Authorization"] = "Bearer " + token
        elif auth == "netrc":
            try:
                credentials = netrc.netrc(settings["netrc_file"]).authenticators(parsed.hostname)
            except (OSError, netrc.NetrcParseError) as exc:
                raise RvError("Cannot read netrc credentials") from exc
            if not credentials or not credentials[2]:
                raise RvError("No netrc credentials for the Gerrit host")
            self.headers["Authorization"] = "Basic " + base64.b64encode(
                f"{credentials[0]}:{credentials[2]}".encode()).decode()
        if auth != "anonymous" and parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise RvError("Authenticated requests require HTTPS")

        self.prefix = "/a" if auth != "anonymous" else ""
        self.opener = urllib.request.build_opener(
            NoRedirect(),
            urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=settings["ca_file"])))
        self.timeout = settings["timeout"]
        self.retries = settings["retries"]
        self.delay = settings["delay"]

    def get(self, path, params=()):
        url = self.base + self.prefix + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        for attempt in range(self.retries + 1):
            retry_after = 0
            try:
                if self.delay:
                    time.sleep(self.delay)
                request = urllib.request.Request(url, headers=self.headers)
                with self.opener.open(request, timeout=self.timeout) as response:
                    return parse_response(response.read())
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    raise HTTPFailure(exc.code) from None
                header = exc.headers.get("Retry-After", "0")
                try:
                    retry_after = float(header)
                except ValueError:
                    try:
                        retry_after = email.utils.parsedate_to_datetime(header).timestamp() - time.time()
                    except (ValueError, TypeError):
                        pass
            except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException):
                if attempt == self.retries:
                    raise RvError("Network request failed after retries; check connectivity and TLS") from None
            time.sleep(min(60, max(retry_after, 2 ** attempt)))


def build_query(filters):
    """The Gerrit search query for the configured filters.

    Every filter is optional. An empty list means "do not restrict", which is
    what the configuration documents, so an empty config exports everything the
    account can read rather than nothing at all.
    """
    terms = []
    if filters["projects"]:
        terms.append(or_terms("project", filters["projects"]))
    if filters["branches"]:
        terms.append(or_terms("branch", filters["branches"]))
    if filters["reviewers"]:
        terms.append(or_terms("reviewer", filters["reviewers"]))
    if filters["status"] != "any":
        terms.append(f"status:{filters['status']}")
    if filters["recent_days"]:
        # Gerrit's -age: means "updated more recently than", which is what
        # "recent" has to mean here: a change whose review finished last week
        # may well have been created a year ago.
        terms.append(f"-age:{int(filters['recent_days'])}d")
    return " ".join(terms) if terms else "is:open OR -is:open"


def or_terms(field, values):
    quoted = " OR ".join(f"{field}:{quote_term(value)}" for value in values)
    return f"({quoted})" if len(values) > 1 else quoted


def quote_term(value):
    """Gerrit query values need quoting when they contain anything but word characters."""
    if re.fullmatch(r"[A-Za-z0-9._/@-]+", value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


# --- discussion threads ----------------------------------------------------

def order_thread(comments):
    """Chronological where possible, with parents always before replies.

    Gerrit timestamps can tie, including across patch sets, so sorting by time
    alone would sometimes print a reply above the comment it answers.
    """
    by_id = {comment["id"]: comment for comment in comments}
    children = collections.defaultdict(list)
    ready = []
    for comment in comments:
        parent = comment.get("in_reply_to")
        if parent in by_id:
            children[parent].append(comment["id"])
        else:
            heapq.heappush(ready, (comment.get("updated", ""), comment["id"]))
    ordered, emitted = [], set()
    while ready:
        _, identifier = heapq.heappop(ready)
        if identifier in emitted:
            continue
        emitted.add(identifier)
        ordered.append(by_id[identifier])
        for child in children[identifier]:
            heapq.heappush(ready, (by_id[child].get("updated", ""), child))
    # A reply cycle has no topological order. Keep the data and let the caller
    # flag it rather than dropping comments or looping forever.
    ordered.extend(sorted((c for c in comments if c["id"] not in emitted),
                          key=lambda c: (c.get("updated", ""), c["id"])))
    return ordered


def group_threads(comments_by_file):
    """Groups a change's comments into threads, following in_reply_to to its root.

    Threads are NOT grouped by file and line. A reply can land on a later patch
    set, and Gerrit moves a comment's line as the file changes underneath it, so
    grouping on position would split one conversation into several.
    """
    comments = []
    for path, entries in comments_by_file.items():
        for comment in entries:
            comments.append({**comment, "path": path})
    by_id = {item["id"]: item for item in comments}

    groups = collections.defaultdict(list)
    for item in comments:
        cursor, seen, trail, cycle = item["id"], {}, [], False
        while True:
            if cursor in seen:
                # Pick a stable representative so every member of the cycle
                # lands in the same thread whichever one we started from.
                cursor, cycle = min(trail[seen[cursor]:]), True
                break
            seen[cursor] = len(trail)
            trail.append(cursor)
            parent = by_id[cursor].get("in_reply_to")
            if not parent:
                break
            if parent not in by_id:
                # The parent is not visible to this account. The reply is still
                # real discussion, so root the thread at the missing id.
                cursor = parent
                break
            cursor = parent
        groups[cursor].append((item, cycle))

    threads = []
    for root_id, entries in sorted(groups.items()):
        ordered = order_thread([entry[0] for entry in entries])
        threads.append({
            "thread_id": root_id,
            "comments": ordered,
            "reply_cycle": any(entry[1] for entry in entries),
            "orphan_root": root_id not in by_id,
        })
    return threads


def thread_is_resolved(thread):
    """Gerrit tracks `unresolved` per comment; the last one to speak decides.

    Older comments and robot comments may omit the flag entirely. Absent any
    signal the thread is treated as resolved, matching how Gerrit's own UI
    counts threads that predate the field.
    """
    for comment in reversed(thread["comments"]):
        if "unresolved" in comment:
            return not comment["unresolved"]
    return True


def thread_anchor(thread):
    """The comment that started the thread: what the reviewer actually pointed at.

    Not the newest comment. A thread opened on patch set 3 and answered on patch
    set 11 is about the code at 3; showing the reader patch set 11 as "before"
    would show them the fix as though it were the problem.
    """
    first = thread["comments"][0]
    return {
        "patchset": int(first.get("patch_set") or 0),
        "path": first.get("path"),
        "line": first.get("line"),
        "range": first.get("range"),
        "author": author_name(first),
        "date": first.get("updated"),
    }


def author_name(comment):
    author = comment.get("author") or {}
    return author.get("name") or author.get("username") or author.get("email") or "unknown"


# --- patch set timeline ----------------------------------------------------

def patchset_index(detail):
    """Patch set number -> commit sha, in numeric order."""
    revisions = detail.get("revisions") or {}
    pairs = sorted(((int(revision["_number"]), sha) for sha, revision in revisions.items()))
    return dict(pairs)


def revision_kind(detail, number):
    """Whether a patch set only rebased or reworked the change.

    Gerrit records this per revision. A thread "resolved" by a rebase was not
    actually answered, and a file that merely moved under a rebase produces a
    misleading after-section, so the renderer needs to know.
    """
    for revision in (detail.get("revisions") or {}).values():
        if int(revision["_number"]) == number:
            return revision.get("kind", "REWORK")
    return "REWORK"


# --- mapping lines across patch sets ---------------------------------------

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_hunks(diff_text):
    """(old_start, old_count, new_start, new_count) for each hunk of a unified diff."""
    hunks = []
    for line in diff_text.splitlines():
        match = HUNK.match(line)
        if match:
            old_start, old_count, new_start, new_count = match.groups()
            hunks.append((int(old_start), 1 if old_count is None else int(old_count),
                          int(new_start), 1 if new_count is None else int(new_count)))
    return hunks


def map_line(line, hunks):
    """Where a line of the old file ends up in the new one.

    Returns (new_line, touched). `touched` is True when the line falls inside a
    hunk, meaning the change being reviewed altered exactly that region -- which
    is the interesting case, because it says the comment was acted on.

    A line inside a hunk has no single counterpart: it may have been deleted, or
    split into five. The hunk's new start is the honest answer -- it points at
    the region that replaced it -- and `touched` tells the caller not to trust
    it as an exact line.
    """
    offset = 0
    for old_start, old_count, new_start, new_count in hunks:
        if old_count == 0:
            # A pure insertion removes nothing. `@@ -10,0 +11,3 @@` means "after
            # old line 10", so old line 10 itself is untouched and only the lines
            # after it shift. Treating old_start as inside the hunk would report
            # every line above an insertion as edited.
            if line <= old_start:
                break
            offset += new_count
            continue
        if line < old_start:
            break
        if line < old_start + old_count:
            return new_start, True
        offset += new_count - old_count
    return line + offset, False


def map_range(start, end, hunks):
    """Maps an inclusive line range, widening to cover everything the hunks touched."""
    new_start, start_touched = map_line(start, hunks)
    new_end, end_touched = map_line(end, hunks)
    touched = start_touched or end_touched
    for old_start, old_count, hunk_new_start, new_count in hunks:
        # A hunk entirely inside the commented range counts as touching it: the
        # reviewer pointed at a block and the author edited within it.
        if start <= old_start <= end or start <= old_start + max(old_count, 1) - 1 <= end:
            touched = True
            new_start = min(new_start, hunk_new_start)
            new_end = max(new_end, hunk_new_start + max(new_count, 1) - 1)
    if new_end < new_start:
        new_end = new_start
    return new_start, new_end, touched


def comment_span(anchor):
    """The inclusive line range a comment refers to, or None for a file-level comment.

    Gerrit's range is half-open at the end character but inclusive by line, and
    a range ending at character 0 of a line does not actually cover that line.
    """
    span = anchor.get("range")
    if span:
        start = int(span["start_line"])
        end = int(span["end_line"])
        if int(span.get("end_character", 0)) == 0 and end > start:
            end -= 1
        return start, end
    line = anchor.get("line")
    if line:
        return int(line), int(line)
    return None


# --- git archive -----------------------------------------------------------

def git(repo, *args, check=True, config=()):
    """Runs Git with a fixed, minimal environment.

    Prompts are disabled so an unattended run fails visibly instead of blocking
    on a credential question nobody is there to answer.
    """
    environment = {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    # GIT_TERMINAL_PROMPT already stops Git asking at a terminal nobody is
    # watching. GIT_ASKPASS is only forced when the caller has not set one:
    # overriding it unconditionally would disable the very credential helper an
    # operator configured to let this run authenticate at all.
    if not os.environ.get("GIT_ASKPASS"):
        environment["GIT_ASKPASS"] = "true"
    options = []
    for setting in config:
        options += ["-c", setting]
    result = subprocess.run(["git", *options, "--git-dir", str(repo), *args],
                            capture_output=True, env=environment)
    if check and result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RvError(f"git {' '.join(args[:2])} failed: {detail[-1] if detail else 'no output'}")
    return result


def http_credential_config(settings, remote):
    """Git config that answers an HTTP prompt from the same credential the API uses.

    Without this, the REST half authenticates and the Git half does not, so a run
    configured with nothing but a Gerrit HTTP password discovers changes and then
    fails to fetch a single patch set.

    The helper snippet names the environment variables; it never contains their
    values, so the password stays out of the process list and off disk. Git
    inherits the variables from this process and reads them when it asks.
    """
    if settings["auth"] != "basic" or not remote.lower().startswith("http"):
        return ()
    user, password = settings["user_env"], settings["password_env"]
    helper = f'!f() {{ echo "username=${user}"; echo "password=${password}"; }}; f'
    return (f"credential.helper={helper}",)


class Archive:
    """A bare repository holding every patch set this run needs.

    Patch set refs are SHA-addressed (`refs/rv/<change>/<patchset>/<sha>`) so a
    force-pushed change cannot overwrite a revision already captured, and a
    second run over the same change fetches nothing.
    """

    def __init__(self, path, remote, git_config=()):
        self.path = Path(path)
        self.remote = remote
        self.git_config = tuple(git_config)
        self.lock = threading.Lock()
        if not (self.path / "HEAD").exists():
            self.path.parent.mkdir(parents=True, exist_ok=True)
            git(self.path, "init", "--bare", "-q", str(self.path))

    def fetch(self, change_number, patchset, sha):
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise RvError(f"Invalid revision object id for change {change_number}")
        target = f"refs/rv/{change_number}/{patchset}/{sha}"
        # Git's own index is not concurrency-safe for writes into one bare repo,
        # so fetches are serialised even though the REST work around them is not.
        with self.lock:
            present = git(self.path, "for-each-ref", "--format=%(objectname)", target).stdout.decode().strip()
            if present == sha:
                return target
            ref = f"refs/changes/{int(change_number) % 100:02d}/{change_number}/{patchset}"
            git(self.path, "fetch", "--no-tags", "--no-write-fetch-head", "-q", self.remote,
                f"{ref}:{target}", config=self.git_config)
            actual = git(self.path, "rev-parse", target).stdout.decode().strip()
            if actual != sha:
                raise RvError(f"Patch set {change_number}/{patchset} changed during capture; rerun")
        return target

    def read_file(self, sha, path):
        """File contents at a revision, or None when the path is absent or binary."""
        result = git(self.path, "show", f"{sha}:{path}", check=False)
        if result.returncode != 0:
            return None
        if b"\0" in result.stdout[:8000]:
            return None
        return result.stdout.decode("utf-8", "replace")

    def diff(self, old_sha, new_sha, path):
        """Unified diff of one file between two revisions, context-free."""
        result = git(self.path, "diff", "--no-ext-diff", "--no-textconv", "-U0",
                     "--find-renames", old_sha, new_sha, "--", path, check=False)
        return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else ""

    def changed_files(self, old_sha, new_sha):
        result = git(self.path, "diff", "--name-only", "--no-ext-diff", old_sha, new_sha, check=False)
        if result.returncode != 0:
            return set()
        return {line for line in result.stdout.decode("utf-8", "replace").splitlines() if line}


# --- deciding which patch set answered a comment ---------------------------

def find_resolution(archive, change_number, patchsets, anchor):
    """The first patch set after the comment in which its file actually changed.

    This is the question the whole tool turns on, and the obvious answers are
    both wrong. The LAST patch set is wrong: on a change that ran to patch set
    12, a comment on 3 was usually answered by 4 or 5, and everything after is
    unrelated work that would bury the fix. The NEXT patch set is wrong too:
    authors push rebases and unrelated fixes between rounds, so the next one
    often does not touch the file at all.

    So: walk forward from the comment's patch set and take the first one where
    this file differs. Returns None when nothing ever touched it, which is a
    real and common outcome -- the thread was answered in words, or the reviewer
    withdrew the point.
    """
    later = [number for number in sorted(patchsets) if number > anchor["patchset"]]
    base_sha = patchsets.get(anchor["patchset"])
    if not base_sha or not anchor["path"] or anchor["path"] in SYNTHETIC:
        return None
    archive.fetch(change_number, anchor["patchset"], base_sha)
    for number in later:
        sha = patchsets[number]
        archive.fetch(change_number, number, sha)
        if anchor["path"] in archive.changed_files(base_sha, sha):
            return number
    return None


# --- rendering -------------------------------------------------------------

def csv_safe(value):
    """A value that can sit on one metadata line, and inside a CSV cell.

    Newlines, tabs and carriage returns become spaces; embedded quotes are
    doubled the way CSV escapes them. The result is always quoted, so a reader
    can split the line on quotes without knowing which fields contain spaces.
    """
    text = "" if value is None else str(value)
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    return '"' + text.replace('"', '""') + '"'


def meta_field(key, value):
    """Bare when the value is a simple token, quoted when it could contain spaces."""
    text = "" if value is None else str(value)
    if re.fullmatch(r"[A-Za-z0-9._:/@+-]*", text):
        return f"{key}={text}"
    return f"{key}={csv_safe(text)}"


def slice_lines(text, start, end, context, cap):
    """The requested lines with a margin, clamped to the file and to `cap` lines.

    Returns (lines, first_line_number). A comment on line 900 of a 20-line file
    happens -- Gerrit keeps the line from an older patch set -- so the range is
    clamped rather than trusted.
    """
    if text is None:
        return [], 0
    lines = text.splitlines()
    if not lines:
        return [], 0
    if start is None:
        first, last = 1, min(len(lines), cap)
    else:
        # Clamp the comment's own lines into the file BEFORE adding the margin.
        # Gerrit keeps a comment's line from the patch set it was written on, so
        # it can point past the end of a file that later shrank; adding context
        # to an out-of-range line first would leave an empty or one-line window.
        begin = min(max(1, start), len(lines))
        finish = min(max(begin, end), len(lines))
        first = max(1, begin - context)
        last = min(len(lines), finish + context)
    if last - first + 1 > cap:
        last = first + cap - 1
    return lines[first - 1:last], first


def render_code(lines, first_line):
    """Code with line numbers, so a reader can talk about "line 47" and be understood."""
    width = len(str(first_line + len(lines) - 1)) if lines else 1
    return "\n".join(f"{first_line + i:>{width}}  {line}" for i, line in enumerate(lines))


def render_discussion(thread, enrich):
    out = []
    for comment in thread["comments"]:
        who = author_name(comment) if enrich["author_names"] else "reviewer"
        patch_set = comment.get("patch_set")
        message = (comment.get("message") or "").strip()
        limit = int(enrich["max_comment_chars"])
        if len(message) > limit:
            message = message[:limit].rstrip() + " …[truncated]"
        head = f"[{who} · ps{patch_set}]" if patch_set else f"[{who}]"
        body = "\n".join("    " + line for line in message.splitlines()) if message else "    (no text)"
        out.append(f"{head}\n{body}")
    return "\n".join(out)


def render_rv(record, enrich):
    """The `.rv` document: two metadata lines, then before / discussion / after."""
    anchor = record["anchor"]
    line_one = " ".join([
        meta_field("id", record["thread_id"]),
        meta_field("change", record["change_number"]),
        meta_field("patchset", anchor["patchset"]),
        meta_field("resolved_in", record["resolved_in"] if record["resolved_in"] else "none"),
        meta_field("file", anchor["path"] or "-"),
        meta_field("project", record["project"]),
        meta_field("branch", record["branch"]),
        meta_field("status", record["status"]),
        meta_field("resolved", "yes" if record["resolved"] else "no"),
        meta_field("comments", len(record["thread"]["comments"])),
        meta_field("code_changed", "yes" if record["code_changed"] else "no"),
    ])
    line_two = " ".join([
        csv_safe(record["subject"]),
        meta_field("owner", record["owner"]),
        meta_field("reviewer", anchor["author"]),
        meta_field("date", anchor["date"]),
        meta_field("change_id", record["change_id"]),
        meta_field("url", record["url"]),
    ])

    sections = [line_one, line_two, ""]
    sections.append(record["before_header"])
    if record["before_code"]:
        sections.append(record["before_code"])
    sections.append("")
    sections.append("--- review")
    sections.append(render_discussion(record["thread"], enrich))
    sections.append("")
    sections.append(record["after_header"])
    if record["after_code"]:
        sections.append(record["after_code"])
    return "\n".join(sections).rstrip() + "\n"


def rv_filename(record, extension):
    """Stable, sortable, and unique per thread."""
    short = re.sub(r"[^A-Za-z0-9]", "", str(record["thread_id"]))[:12] or "thread"
    stem = f"{int(record['change_number']):07d}-ps{int(record['anchor']['patchset']):02d}-{short}"
    return stem + extension


# --- building one record ---------------------------------------------------

def build_record(archive, detail, thread, config):
    """Turns one discussion thread into everything `render_rv` needs, or None to skip.

    Every branch here is a real Gerrit situation rather than a defensive guess:
    file-level comments carry no line, the commit message is not a file, a
    thread can be answered in words with no code change at all, and a file can
    be deleted by the very patch set that answers the comment.
    """
    enrich = config["enrich"]
    anchor = thread_anchor(thread)
    resolved = thread_is_resolved(thread)
    if not resolved and not enrich["include_unresolved"]:
        return None

    path = anchor["path"]
    if path == PATCHSET_LEVEL or (path == COMMIT_MSG and not enrich["include_commit_message_comments"]):
        return None
    for pattern in config["filters"]["exclude_paths"]:
        if path and fnmatch.fnmatch(path, pattern):
            return None

    change_number = detail["_number"]
    patchsets = patchset_index(detail)
    resolved_in = find_resolution(archive, change_number, patchsets, anchor)
    if resolved_in is None and not enrich["include_reply_only"]:
        return None

    span = comment_span(anchor)
    context, cap = int(enrich["context_lines"]), int(enrich["max_code_lines"])
    before_code = after_code = ""
    before_header = after_header = ""
    code_changed = False

    if path in SYNTHETIC:
        # The commit message lives in the commit object, not the tree.
        before_header = f"--- before ({path}, patch set {anchor['patchset']})"
        base_sha = patchsets.get(anchor["patchset"])
        message = commit_message(archive, change_number, anchor["patchset"], base_sha)
        lines, first = slice_lines(message, None, None, context, cap)
        before_code = render_code(lines, first)
        after_header = "--- after (commit message is not tracked per patch set here)"
    else:
        base_sha = patchsets.get(anchor["patchset"])
        before_text = archive.read_file(base_sha, path) if base_sha else None
        if before_text is None:
            before_header = f"--- before (patch set {anchor['patchset']}: {path} is absent or binary)"
        else:
            start, end = span if span else (None, None)
            lines, first = slice_lines(before_text, start, end, context, cap)
            before_header = code_header("before", anchor["patchset"], first, len(lines), span)
            before_code = render_code(lines, first)

        if resolved_in is None:
            last = max(patchsets) if patchsets else anchor["patchset"]
            after_header = ("--- after (no patch set changed this file; the thread was answered in "
                            f"discussion, latest patch set is {last})")
        else:
            new_sha = patchsets[resolved_in]
            after_text = archive.read_file(new_sha, path)
            if after_text is None:
                after_header = f"--- after (patch set {resolved_in}: {path} was deleted or became binary)"
                code_changed = True
            else:
                hunks = parse_hunks(archive.diff(base_sha, new_sha, path))
                if span:
                    new_start, new_end, touched = map_range(span[0], span[1], hunks)
                else:
                    new_start, new_end, touched = None, None, bool(hunks)
                code_changed = touched
                lines, first = slice_lines(after_text, new_start, new_end, context, cap)
                after_header = code_header("after", resolved_in, first, len(lines), span,
                                           note="" if touched else "; the commented lines themselves were not edited")
                after_code = render_code(lines, first)

    owner = (detail.get("owner") or {}).get("name") or (detail.get("owner") or {}).get("username") or "unknown"
    return {
        "thread_id": thread["thread_id"],
        "thread": thread,
        "anchor": anchor,
        "change_number": change_number,
        "change_id": detail.get("change_id", ""),
        "project": detail.get("project", ""),
        "branch": detail.get("branch", ""),
        "status": detail.get("status", ""),
        "subject": detail.get("subject", ""),
        "owner": owner,
        "url": detail.get("_url", ""),
        "resolved": resolved,
        "resolved_in": resolved_in,
        "code_changed": code_changed,
        "before_header": before_header,
        "before_code": before_code,
        "after_header": after_header,
        "after_code": after_code,
    }


def code_header(kind, patchset, first, count, span, note=""):
    if not count:
        return f"--- {kind} (patch set {patchset}, no lines)"
    where = f"lines {first}-{first + count - 1}"
    target = f", comment on {span[0]}" + (f"-{span[1]}" if span[1] != span[0] else "") if span else ", whole-file comment"
    return f"--- {kind} (patch set {patchset}, {where}{target}{note})"


def commit_message(archive, change_number, patchset, sha):
    if not sha:
        return None
    archive.fetch(change_number, patchset, sha)
    result = git(archive.path, "show", "-s", "--format=%B", sha, check=False)
    return result.stdout.decode("utf-8", "replace") if result.returncode == 0 else None


# --- limits ----------------------------------------------------------------

class Budget:
    """Shared, thread-safe counters. Any limit reaching zero stops the export.

    `take` is all-or-nothing so two workers cannot both believe they claimed the
    last slot and write one file more than the configuration allows.
    """

    def __init__(self, limits):
        self.limits = {key: (None if value is None else int(value)) for key, value in limits.items()}
        self.used = {key: 0 for key in self.limits}
        self.lock = threading.Lock()
        self.stopped = None

    def take(self, key, amount=1):
        with self.lock:
            if self.stopped:
                return False
            limit = self.limits.get(key)
            if limit is not None and self.used[key] + amount > limit:
                self.stopped = key
                return False
            self.used[key] += amount
            return True

    @property
    def reason(self):
        return f"limits.{self.stopped} reached" if self.stopped else None


# --- export ----------------------------------------------------------------

DETAIL_OPTIONS = ["ALL_REVISIONS", "DETAILED_ACCOUNTS", "CURRENT_COMMIT", "MESSAGES"]


def discover(client, config, budget):
    """Change numbers matching the filters, newest first, within limits."""
    query = build_query(config["filters"])
    found, start = [], 0
    while True:
        batch = client.get("/changes/", [("q", query), ("n", 100), ("S", start),
                                         ("o", "SKIP_DIFFSTAT")]) or []
        if not batch:
            break
        for change in batch:
            if not budget.take("max_changes"):
                return found
            found.append(str(change["_number"]))
        if not batch[-1].get("_more_changes"):
            break
        start += len(batch)
    return found


def change_records(client, archive, number, config, budget):
    """Every renderable thread on one change."""
    detail = client.get(f"/changes/{number}/detail", [("o", option) for option in DETAIL_OPTIONS])
    if not detail:
        return []
    detail["_url"] = f"{client.base}/c/{urllib.parse.quote(detail['project'], safe='/')}/+/{number}"
    comments = client.get(f"/changes/{number}/comments") or {}
    if not comments:
        return []

    records = []
    for thread in group_threads(comments):
        if not budget.take("max_comments", len(thread["comments"])):
            break
        record = build_record(archive, detail, thread, config)
        if record is None:
            continue
        if not budget.take("max_files"):
            break
        records.append(record)
    return records


def export(config, out=sys.stdout):
    client = Client(config["gerrit"])
    remote = config["gerrit"]["git_url"] or derive_git_url(client.base, config)
    output_dir = Path(config["output"]["dir"])
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = Archive(output_dir / "code.git", remote,
                      http_credential_config(config["gerrit"], remote))
    budget = Budget(config["limits"])

    numbers = discover(client, config, budget)
    print(f"discovered {len(numbers)} change(s)", file=out)

    written, failed, lock = [], [], threading.Lock()

    def handle(number):
        try:
            return number, change_records(client, archive, number, config, budget), None
        except RvError as exc:
            return number, [], str(exc)

    workers = max(1, int(config["workers"]))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for number, records, error in pool.map(handle, numbers):
            if error:
                with lock:
                    failed.append((number, error))
                continue
            for record in records:
                path = output_dir / rv_filename(record, config["output"]["extension"])
                if path.exists() and not config["output"]["overwrite"]:
                    continue
                path.write_text(render_rv(record, config["enrich"]), encoding="utf-8")
                with lock:
                    written.append(path.name)

    summary = {
        "changes_seen": len(numbers),
        "files_written": len(written),
        "comments_read": budget.used.get("max_comments", 0),
        "stopped_by": budget.reason,
        "failed_changes": failed,
        "output_dir": str(output_dir),
    }
    print(f"wrote {len(written)} .rv file(s) to {output_dir}", file=out)
    if budget.reason:
        print(f"stopped early: {budget.reason}", file=out)
    for number, error in failed:
        print(f"change {number} failed: {error}", file=out)
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def derive_git_url(base, config):
    """The HTTP clone URL, used when gerrit.git_url is not configured."""
    projects = config["filters"]["projects"]
    if len(projects) != 1:
        raise RvError("Set gerrit.git_url, or filter to exactly one project so the clone URL can be derived")
    prefix = "/a" if config["gerrit"]["auth"] != "anonymous" else ""
    return base + prefix + "/" + urllib.parse.quote(projects[0], safe="/")


# --- command line ----------------------------------------------------------

def cmd_init_config(args):
    path = Path(args.path)
    if path.exists() and not args.force:
        raise RvError(f"{path} already exists; pass --force to overwrite")
    path.write_text(json.dumps(DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {path}")
    return 0


def cmd_export(args):
    config = load_config(args.config)
    if args.output:
        config["output"]["dir"] = args.output
    export(config)
    return 0


def cmd_check_config(args):
    load_config(args.config)
    print(f"{args.config} is valid")
    return 0


def parser():
    root = argparse.ArgumentParser(prog="gerrit_rv.py", description=__doc__.splitlines()[0])
    root.add_argument("--version", action="version", version=__version__)
    sub = root.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init-config", help="write a config file with every default")
    init.add_argument("path", nargs="?", default="rv.config.json")
    init.add_argument("--force", action="store_true")
    init.set_defaults(handler=cmd_init_config)

    check = sub.add_parser("check-config", help="validate a config file without contacting Gerrit")
    check.add_argument("--config", default="rv.config.json")
    check.set_defaults(handler=cmd_check_config)

    run = sub.add_parser("export", help="write .rv files")
    run.add_argument("--config", default="rv.config.json")
    run.add_argument("--output", help="override output.dir")
    run.set_defaults(handler=cmd_export)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return args.handler(args)
    except RvError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
