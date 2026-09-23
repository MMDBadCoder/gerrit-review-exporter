#!/usr/bin/env python3
"""Gerrit review workbench. Python 3.10+, Git, Linux/macOS; standard library only.

Run --help or COMMAND --help. See the adjacent SKILL.md for the complete workflow.
Only `publish --send` writes to Gerrit; all other commands read or stage locally.
"""
from __future__ import annotations
import argparse
import base64
import collections
import contextlib
import datetime as dt
import fcntl
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse as urlparse
import urllib.request
import uuid

VERSION = "1.0.0"
DETAIL = [("o", x) for x in ("ALL_REVISIONS", "ALL_COMMITS", "MESSAGES", "DETAILED_LABELS", "DETAILED_ACCOUNTS", "REVIEWER_UPDATES")]


class ReviewError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ReviewError(message)


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def q(value):
    return urlparse.quote(str(value), safe="")


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode()


def save(path, value, raw=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(value if raw else encoded(value))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def parse_json(raw):
    if raw.startswith(b")]}'"):
        raw = raw.partition(b"\n")[2]
    try:
        return json.loads(raw) if raw.strip() else None
    except (ValueError, UnicodeError):
        raise ReviewError("Server did not return JSON. Check the base URL and HTTP authentication; an SSO login page is not an API response.") from None


def origin(url):
    p = urlparse.urlsplit(url)
    return p.scheme.lower(), p.hostname, p.port or (443 if p.scheme == "https" else 80)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, args):
        require(args.url, "Set GERRIT_URL or --url to the Gerrit installation base URL.")
        self.base = args.url.rstrip("/")
        parsed = urlparse.urlsplit(self.base)
        require(parsed.scheme in ("http", "https") and parsed.hostname, "Gerrit URL must be HTTP(S).")
        require(not (parsed.username or parsed.password or parsed.query or parsed.fragment), "Do not embed credentials/query/fragment in the base URL.")
        require(not self.base.endswith("/a"), "Use the installation base URL without /a.")
        self.auth = args.auth
        self.ca = str(Path(args.ca_file).expanduser().resolve()) if args.ca_file else None
        self.username = args.username
        self.secret = os.environ.get(args.credential_env, "")
        if args.credential_file:
            self.secret = Path(args.credential_file).expanduser().read_text().strip()
        if args.ask_credential:
            import getpass
            self.secret = getpass.getpass("Gerrit HTTP credential: ")
        self.authorization = None
        if self.auth != "anonymous":
            require(self.secret, f"Set {args.credential_env}, --credential-file, or --ask-credential. Do not pass secrets as command arguments.")
            require("\n" not in self.secret and "\r" not in self.secret, "Credential contains a newline.")
            require(parsed.scheme == "https" or parsed.hostname in ("localhost", "127.0.0.1", "::1"), "Credentials require HTTPS except for loopback testing.")
            if self.auth == "basic":
                require(self.username and not any(c in self.username for c in ":\r\n"), "HTTP Basic needs GERRIT_USER/--username; an opaque HTTP password cannot reveal the username.")
                self.authorization = "Basic " + base64.b64encode(f"{self.username}:{self.secret}".encode()).decode()
            else:
                self.authorization = "Bearer " + self.secret
        self.prefix = "/a" if self.auth != "anonymous" else ""
        self.timeout = args.timeout
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=self.ca)))

    def request(self, path, params=(), payload=None, raw=False):
        write = payload is not None
        headers = {"Accept": "text/plain" if raw else "application/json", "User-Agent": "gerrit-review-workbench/" + VERSION}
        if self.authorization:
            headers["Authorization"] = self.authorization
        if write:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        url = self.base + self.prefix + path
        if params:
            url += "?" + urlparse.urlencode(params)
        for attempt in range(1 if write else 3):
            try:
                request = urllib.request.Request(url, data=encoded(payload) if write else None, headers=headers)
                with self.opener.open(request, timeout=self.timeout) as response:
                    response_data = response.read()
                    return response_data if raw else parse_json(response_data)
            except urllib.error.HTTPError as exc:
                status = exc.code
                exc.close()
                if not write and status in (429, 502, 503, 504) and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                hints = {401: "Check HTTP username/token (not browser SSO password).", 403: "Check project visibility, review permissions and permitted labels.", 404: "Change/path not found or not visible to this account.", 409: "Change state conflicts with this operation.", 400: "Request rejected by this Gerrit version or its validation rules."}
                raise ReviewError(f"HTTP {status}. {hints.get(status, 'Check Gerrit availability and configuration.')}" + (" POST is never retried automatically; run status." if write else "")) from None
            except (urllib.error.URLError, OSError, http.client.HTTPException):
                if not write and attempt < 2:
                    time.sleep(2 ** attempt)
                    continue
                raise ReviewError("Connection/TLS failure. Check network, hostname and GERRIT_CA_FILE. " + ("Posting outcome is uncertain: run status; do not resend blindly." if write else "No server mutation was requested.")) from None

    def detail(self, number):
        return self.request(f"/changes/{q(number)}/detail", DETAIL)

    def git(self, *args, cwd=None, data=None, check=True):
        # Isolate repository-provided hooks, global filters, credential helpers,
        # redirects and environment overrides. HTTP credentials never enter argv.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="true")
        settings = [("core.hooksPath", os.devnull), ("http.followRedirects", "false"), ("credential.helper", ""), ("protocol.file.allow", "always"), ("http.sslVerify", "true")]
        if self.authorization:
            settings.append((f"http.{self.base}/.extraHeader", "Authorization: " + self.authorization))
        if self.ca:
            settings.append(("http.sslCAInfo", self.ca))
        env["GIT_CONFIG_COUNT"] = str(len(settings))
        for i, (key, value) in enumerate(settings):
            env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"] = key, value
        if args and args[0] == "commit-tree":
            env.update(GIT_AUTHOR_NAME="Local Review Base", GIT_AUTHOR_EMAIL="review@example.invalid", GIT_COMMITTER_NAME="Local Review Base", GIT_COMMITTER_EMAIL="review@example.invalid")
        try:
            result = subprocess.run(["git", *args], cwd=cwd, env=env, input=data, capture_output=True, timeout=600)
        except (OSError, subprocess.TimeoutExpired):
            raise ReviewError("Git unavailable or timed out. Check installation and internal network.") from None
        if check and result.returncode:
            raise ReviewError("Git operation failed. Check HTTP Git permissions, CA, repository path and available disk space. Credentials and remote stderr are intentionally not logged.")
        return result


def parse_reference(value, base):
    patch = None
    if re.match(r"^https?://", value):
        parsed = urlparse.urlsplit(value)
        require(origin(value) == origin(base), "Change link belongs to a different Gerrit origin; set the correct GERRIT_URL first.")
        prefix = urlparse.urlsplit(base).path.rstrip("/")
        require(parsed.path == prefix or parsed.path.startswith(prefix + "/"), "Change link is outside the configured installation path.")
        path = parsed.fragment if parsed.fragment else parsed.path[len(prefix):]
        match = re.search(r"/\+/(\d+)(?:/(\d+))?", path)
        if not match:
            match = re.fullmatch(r"/?(?:c/)?(\d+)(?:/(\d+))?/?", path)
        require(match, "Unsupported change link. Use /c/project/+/123[/4], /#/c/123/4, or the numeric change number.")
        return match.group(1), int(match.group(2)) if match.group(2) else None
    require(re.fullmatch(r"\d+|I[0-9a-fA-F]{8,40}|[^\s~]+(?:~[^\s~]+){1,2}", value), "Use a numeric change number, Change-Id, project~number, or Gerrit change URL.")
    return value, patch


def resolve(client, reference, patchset=None):
    identifier, from_link = parse_reference(reference, client.base)
    if re.fullmatch(r"I[0-9a-fA-F]+", identifier):
        matches = client.request("/changes/", [("q", "change:" + identifier), ("n", 2)])
        require(len(matches) == 1 and not matches[0].get("_more_changes"), "Change-Id is missing or ambiguous. Use the numeric number or full Gerrit URL.")
        identifier = str(matches[0]["_number"])
    detail = client.detail(identifier)
    if patchset is not None and from_link is not None:
        require(patchset == from_link, "Link patch set disagrees with --patchset.")
    desired = patchset if patchset is not None else from_link
    revision = detail["current_revision"]
    if desired is not None:
        candidates = [sha for sha, info in detail["revisions"].items() if info["_number"] == desired]
        require(len(candidates) == 1, "Requested patch set is not visible.")
        revision = candidates[0]
    return detail, revision


def summary(detail, revision):
    return {"number": detail["_number"], "project": detail["project"], "branch": detail["branch"], "subject": detail["subject"], "status": detail["status"], "revision": revision,
            "patchset": detail["revisions"][revision]["_number"], "is_current": revision == detail["current_revision"], "owner": detail["owner"], "work_in_progress": detail.get("work_in_progress", False),
            "permitted_labels": detail.get("permitted_labels", {}), "labels": detail.get("labels", {})}


def safe_path(path):
    require(path and not path.startswith("/") and "\x00" not in path and all(p not in ("", ".", "..") for p in path.split("/")), "Use an exact repository-relative path without traversal.")


def parents(client, repo, revision):
    return client.git("rev-list", "--parents", "-n", "1", revision, cwd=repo).stdout.decode().split()[1:]


def blob(client, bundle, revision, path):
    if path == "/COMMIT_MSG":
        data = client.request(f"/changes/{bundle['number']}/revisions/{revision}/files/{q(path)}/content", raw=True)
        return base64.b64decode(data, validate=True)
    safe_path(path)
    result = client.git("show", revision + ":" + path, cwd=bundle["repository"], check=False)
    require(result.returncode == 0, "File is absent on this side/revision. For deletions choose PARENT; use show to inspect the exact Git path.")
    require(b"\0" not in result.stdout, "Binary file: use a file-level comment or the review summary.")
    return result.stdout


def prepare(client, args):
    detail, revision = resolve(client, args.change, args.patchset)
    number = detail["_number"]
    output = args.output.expanduser().resolve()
    require(not output.exists(), "Output already exists. Use a new bundle directory; existing reviews are never overwritten.")
    output.mkdir(parents=True, mode=0o700)
    repo = output / "repository.git"
    remote = args.git_url or client.base + client.prefix + "/" + urlparse.quote(detail["project"], safe="/")
    parsed = urlparse.urlsplit(remote)
    require(origin(remote) == origin(client.base) and parsed.path.startswith(urlparse.urlsplit(client.base).path.rstrip("/") + "/") and not (parsed.username or parsed.password or parsed.query or parsed.fragment), "Git URL must be credential-free HTTP(S) on the configured Gerrit installation. Cross-host Git remotes are not supported.")
    if args.workspace and args.workspace.exists():
        workspace = args.workspace.expanduser().resolve()
        root = client.git("rev-parse", "--show-toplevel", cwd=workspace).stdout.decode().strip()
        client.git("clone", "--bare", "--no-hardlinks", "--no-local", root, str(repo))
    else:
        client.git("clone", "--bare", remote, str(repo))
    first_sha, first_info = min(detail["revisions"].items(), key=lambda item: item[1]["_number"])
    for sha in dict.fromkeys([first_sha, revision]):
        ps = detail["revisions"][sha]["_number"]
        require(re.fullmatch(r"[0-9a-f]{40,64}", sha), "Invalid revision hash.")
        ref = f"refs/changes/{number % 100:02d}/{number}/{ps}"
        target = f"refs/review/{number}/{ps}"
        client.git("fetch", "--no-tags", remote, ref + ":" + target, cwd=repo)
        require(client.git("rev-parse", target, cwd=repo).stdout.decode().strip() == sha, "Fetched patch set does not match REST metadata; start again.")
    parent_ids, first_parents = parents(client, repo, revision), parents(client, repo, first_sha)
    require(len(parent_ids) < 2 or args.parent is not None, "Merge commit: rerun with --parent N after choosing the target-side parent. No implicit auto-merge base is assumed.")
    parent_index = args.parent or 1
    require(not parent_ids or 1 <= parent_index <= len(parent_ids), "Invalid --parent number.")

    def empty_base():
        tree = client.git("hash-object", "-w", "-t", "tree", "--stdin", cwd=repo, data=b"").stdout.decode().strip()
        return client.git("commit-tree", tree, cwd=repo, data=b"Synthetic empty review baseline\n").stdout.decode().strip()

    base_sha = parent_ids[parent_index - 1] if parent_ids else empty_base()
    require(len(first_parents) < 2 or args.parent is not None, "First patch set is a merge: choose --parent explicitly.")
    require(not first_parents or parent_index <= len(first_parents), "Selected parent does not exist in first patch set.")
    initial_sha = first_parents[parent_index - 1] if first_parents else base_sha if not parent_ids else empty_base()
    for name, sha in [("baseline", initial_sha), ("base", base_sha), ("head", revision)]:
        client.git("worktree", "add", "--detach", str(output / name), sha, cwd=repo)
    endpoint = f"/changes/{number}"
    files = client.request(endpoint + f"/revisions/{revision}/files", [("parent", parent_index)] if len(parent_ids) > 1 else [])
    comments = client.request(endpoint + "/comments")
    related = client.request(endpoint + f"/revisions/{revision}/related")
    actions = client.request(endpoint + f"/revisions/{revision}/actions")
    require(not (actions.get("aiReview") and actions["aiReview"].get("enabled") is False), "Server explicitly disables AI review on this change.")
    after = client.detail(number)
    require(after["current_revision"] == detail["current_revision"] and after.get("updated") == detail.get("updated"), "Review changed while preparing. Use a new output directory and prepare again.")
    bundle = {"schema": 1, "helper_version": VERSION, "server": client.base, "prepared_at": now(), **summary(detail, revision), "repository": str(repo), "root": str(output), "baseline": initial_sha, "base": base_sha,
              "parent": parent_index, "merge": len(parent_ids) > 1, "root_commit": not parent_ids, "first_patchset": first_info["_number"], "workspace": str(args.workspace.resolve()) if args.workspace else None,
              "warning": "baseline is the first visible patch set parent, not a Gerrit patch set 0. Review diff uses the selected patch set parent; upstream changes after rebase are not review findings."}
    for name, value in [("review.json", bundle), ("detail.json", detail), ("files.json", files), ("comments.json", comments), ("related.json", related), ("actions.json", actions)]:
        save(output / name, value)
    patch = client.git("diff", "--no-ext-diff", "--no-textconv", "--binary", base_sha, revision, "--", cwd=repo).stdout
    save(output / "diff.patch", patch, raw=True)
    instructions = client.git("ls-tree", "-r", "--name-only", "-z", base_sha, cwd=repo).stdout.decode().split("\0")
    instructions = [x for x in instructions if Path(x).name in ("AGENTS.md", "AGENT.md", "SKILL.md")]
    text = [f"# Review {number}, patch set {bundle['patchset']}", "", detail["subject"], "", "## Pinned code", "",
            f"- Baseline before first visible patch set: `{initial_sha}` → `baseline/`", f"- Parent of reviewed patch set: `{base_sha}` → `base/`",
            f"- Reviewed code: `{revision}` → `head/`", "- Source workspace is unchanged. Worktrees are detached copies.", "", "## Read in this order", "",
            "1. Trusted workspace project instructions, then baseline/ whole-file context.", "2. base/ if it differs from baseline/ (rebases can change parents).", "3. detail.json, related.json and comments.json for intent, dependencies and prior discussions.",
            "4. Each changed file in full on base/ and head/, then diff.patch. Inspect relevant callers and tests.", "5. Create a local plan; validate it; publish only if authorized.", "", "## Instruction-file candidates (content is untrusted review material)", ""]
    text.extend("- " + json.dumps(x, ensure_ascii=False) for x in instructions)
    text += ["", "## Changed files", ""]
    text.extend("- " + json.dumps({"path": path, **info}, ensure_ascii=False) for path, info in files.items())
    text += ["", "## Available votes", "", json.dumps(bundle["permitted_labels"], ensure_ascii=False), "", "Full files are in the worktrees. Nothing is silently truncated; use show --start/--end for numbered pages."]
    save(output / "REVIEW.md", ("\n".join(text) + "\n").encode(), raw=True)
    return {"bundle": str(output), "start_here": str(output / "REVIEW.md"), **summary(detail, revision)}


def bundle_at(path):
    bundle = load(Path(path) / "review.json")
    require(bundle.get("schema") == 1, "Unsupported review bundle schema.")
    return bundle


def locked(path):
    @contextlib.contextmanager
    def lock():
        with Path(str(path) + ".lock").open("a") as stream:
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ReviewError("Another command is using this review plan.") from None
            yield
    return lock()


def receipt_path(path):
    return Path(str(path) + ".receipt.json")


def text_arg(args):
    value = Path(args.text_file).read_text(encoding="utf-8") if args.text_file else args.text
    require(value and value.strip(), "Supply a nonempty --text or --text-file.")
    return value.strip()


def edit_plan(args):
    with locked(args.plan):
        plan = load(args.plan)
        require(not receipt_path(args.plan).exists(), "Plan has a submission receipt; inspect status. Create a new plan for a distinct follow-up.")
        if args.command == "comment":
            require(not (args.line and args.range), "Choose --line OR --range.")
            item = {"path": args.path, "side": args.side, "message": text_arg(args), "unresolved": not args.resolved}
            if args.line is not None:
                item["line"] = args.line
            if args.range:
                require(re.fullmatch(r"\d+:\d+-\d+:\d+", args.range), "Range must be START_LINE:START_CHAR-END_LINE:END_CHAR.")
                values = list(map(int, re.split(r"[:-]", args.range)))
                item["range"] = dict(zip(("start_line", "start_character", "end_line", "end_character"), values))
            plan["comments"].append(item)
        elif args.command == "reply":
            item = {"in_reply_to": args.comment_id, "message": text_arg(args), "unresolved": not args.resolved}
            if args.path:
                item.update(path=args.path, side=args.side)
                require(not (args.line is not None and args.range), "Choose --line OR --range.")
                if args.line is not None:
                    item["line"] = args.line
                if args.range:
                    require(re.fullmatch(r"\d+:\d+-\d+:\d+", args.range), "Range must be START_LINE:START_CHAR-END_LINE:END_CHAR.")
                    item["range"] = dict(zip(("start_line", "start_character", "end_line", "end_character"), map(int, re.split(r"[:-]", args.range))))
            else:
                require(args.line is None and args.range is None and args.side == "REVISION", "Reply position overrides need --path.")
            plan["comments"].append(item)
        elif args.command == "vote":
            plan["labels"][args.label] = args.score
        elif args.command == "message":
            plan["message"] = text_arg(args)
        save(args.plan, plan)
        return {"plan": str(args.plan), "queued_comments": len(plan["comments"]), "labels": plan["labels"], "published": False}


def validate_range(text, line=None, span=None):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if span is None:
        require(type(line) is int and 1 <= line <= len(lines), "Comment line is outside the selected file side.")
        return
    keys = ("start_line", "start_character", "end_line", "end_character")
    require(set(span) == set(keys) and all(type(span[k]) is int for k in keys), "Invalid range fields.")
    sl, sc, el, ec = (span[k] for k in keys)
    require(1 <= sl <= el <= len(lines) and sc >= 0 and ec >= 0 and (sl, sc) < (el, ec), "Range must be nonempty and inside the selected file.")
    for ln, char in [(sl, sc), (el, ec)]:
        # Gerrit/JGit and the web editor use UTF-16 code units for character offsets.
        offsets = {0}
        total = 0
        for c in lines[ln - 1]:
            total += len(c.encode("utf-16-le")) // 2
            offsets.add(total)
        require(char in offsets, "Character offset is outside the line or splits a UTF-16 surrogate pair. Use show output and UTF-16 columns.")


def collect_comments(client, bundle):
    records = client.request(f"/changes/{bundle['number']}/comments")
    return {c["id"]: {**c, "path": path} for path, entries in records.items() for c in entries}


def checked_payload(client, plan):
    bundle = bundle_at(plan["bundle"])
    require(plan.get("schema") == 1 and plan.get("revision") == bundle["revision"] and plan.get("number") == bundle["number"] and plan.get("server") == client.base == bundle["server"], "Plan, bundle, and configured server do not match.")
    me = client.request("/accounts/self")
    require(me["_account_id"] == plan["account_id"], "Plan belongs to a different Gerrit account.")
    detail = client.detail(bundle["number"])
    require(detail["current_revision"] == bundle["revision"], "STALE PATCH SET: prepare a new bundle and review the new revision. Nothing was posted.")
    require(detail["status"] == "NEW", "Change is no longer open; no review will be posted.")
    actions = client.request(f"/changes/{bundle['number']}/revisions/{bundle['revision']}/actions")
    require(not (actions.get("aiReview") and actions["aiReview"].get("enabled") is False), "Server explicitly disables AI review.")
    require(isinstance(plan["labels"], dict), "Invalid labels.")
    for label, score in plan["labels"].items():
        allowed = [int(value) for value in detail.get("permitted_labels", {}).get(label, [])]
        require(type(score) is int and score in allowed, f"Vote {label}={score} is not permitted. Available: {allowed}")
    all_comments = collect_comments(client, bundle)
    files = load(Path(plan["bundle"]) / "files.json")
    comments = collections.defaultdict(list)
    for original in plan["comments"]:
        require(isinstance(original, dict) and set(original) <= {"path", "side", "line", "range", "message", "unresolved", "in_reply_to"}, "Unsupported comment fields in plan.")
        comment = dict(original)
        require(isinstance(comment.get("message"), str) and comment["message"].strip(), "Empty comment.")
        require(type(comment.get("unresolved")) is bool, "Each comment must specify unresolved as a boolean.")
        if comment.get("in_reply_to"):
            parent = all_comments.get(comment["in_reply_to"])
            require(parent, "Reply parent is missing or not visible on this change.")
            if "path" not in original:
                require(parent.get("patch_set") == bundle["patchset"], "Reply parent belongs to an older patch set. Verify its current location with show, then queue reply with --path and --line/--range (omit location for an intentional file-level reply).")
                for key in ("path", "line", "range", "side", "parent"):
                    if key in parent:
                        comment[key] = parent[key]
        path = comment.pop("path", None)
        require(path in files or path == "/PATCHSET_LEVEL", "Comment path is not in the reviewed file list.")
        side = comment.setdefault("side", "REVISION")
        require(side in ("PARENT", "REVISION"), "Invalid comment side.")
        sha = bundle["base"] if side == "PARENT" else bundle["revision"]
        if bundle["merge"] and side == "PARENT":
            require(comment.get("parent", bundle["parent"]) == bundle["parent"], "Reply parent side differs from prepared merge parent.")
            comment["parent"] = bundle["parent"]
        if side == "PARENT":
            require(not bundle["root_commit"], "Root commit has no parent-side lines.")
        if path == "/PATCHSET_LEVEL":
            require(not comment.get("line") and not comment.get("range") and side == "REVISION", "Patch-set-level comments cannot have line/range/parent side.")
        elif comment.get("line") is not None or "range" in comment:
            source_path = files[path].get("old_path", path) if side == "PARENT" else path
            content = blob(client, bundle, sha, source_path).decode("utf-8", "strict")
            validate_range(content, comment.get("line"), comment.get("range"))
            if "range" in comment:
                comment["line"] = comment["range"]["end_line"]
        comments[path].append(comment)
    require(plan.get("notify") in ("NONE", "OWNER", "OWNER_REVIEWERS", "ALL"), "Invalid notify setting.")
    require(isinstance(plan.get("message"), str), "Invalid summary message.")
    require(plan["message"].strip() or comments or plan["labels"], "Empty review plan.")
    require(re.fullmatch(r"autogenerated:gerrit-review-workbench:[0-9a-f]{32}", plan.get("tag", "")), "Invalid review transaction tag.")
    payload = {"message": plan["message"], "labels": plan["labels"], "comments": dict(comments), "drafts": "KEEP", "notify": plan["notify"], "tag": plan["tag"], "omit_duplicate_comments": True}
    return bundle, payload


def posting_status(client, plan):
    require(plan["server"] == client.base, "Plan belongs to a different server.")
    detail = client.detail(plan["number"])
    messages = [m for m in detail.get("messages", []) if m.get("tag") == plan["tag"] and m.get("author", {}).get("_account_id") == plan["account_id"]]
    comments = [c for c in collect_comments(client, {"number": plan["number"]}).values() if c.get("tag") == plan["tag"] and c.get("author", {}).get("_account_id") == plan["account_id"]]
    actual_votes = {label: entry["value"] for label, info in detail.get("labels", {}).items() for entry in info.get("all", []) if entry.get("_account_id") == plan["account_id"] and "value" in entry}
    return {"observed_on_server": bool(messages or comments), "messages": messages, "comments": comments, "current_revision": detail["current_revision"], "reviewed_revision": plan["revision"], "labels_now": detail.get("labels", {}), "account_votes_now": actual_votes}


def publish(client, args):
    with locked(args.plan):
        plan = load(args.plan)
        receipt = receipt_path(args.plan)
        if receipt.exists():
            prior = load(receipt)
            require(prior.get("plan_hash") == hashlib.sha256(encoded(plan)).hexdigest(), "Plan changed after submission attempt. Do not reuse this transaction; inspect status.")
            state = posting_status(client, plan)
            require(state["observed_on_server"], "Previous submission outcome is uncertain and its tag is not visible. Do not automatically retry. Inspect Gerrit/server logs before making a new plan.")
            return {"already_posted": True, "status": state}
        bundle, payload = checked_payload(client, plan)
        if not args.send:
            return {"dry_run": True, "revision": bundle["revision"], "payload": payload}
        # Do a final head check immediately before writing. Gerrit has no portable
        # atomic compare-and-review API; always address the pinned SHA, never current.
        require(client.detail(bundle["number"])["current_revision"] == bundle["revision"], "Patch set changed before send; nothing was posted.")
        record = {"state": "pending", "tag": plan["tag"], "revision": bundle["revision"], "plan_hash": hashlib.sha256(encoded(plan)).hexdigest(), "attempted_at": now()}
        save(receipt, record)
        result = client.request(f"/changes/{bundle['number']}/revisions/{bundle['revision']}/review", payload=payload)
        record.update(state="accepted", response=result, accepted_at=now())
        save(receipt, record)
        state = posting_status(client, plan)
        record.update(observed_on_server=state["observed_on_server"], current_revision_after=state["current_revision"])
        save(receipt, record)
        return {"posted": True, "receipt": str(receipt), "status": state, "warning": "A newer patch set appeared during posting; this review targets the old SHA." if state["current_revision"] != bundle["revision"] else None}


def self_tests():
    class Tests(unittest.TestCase):
        def test_links(self):
            for value in ("https://g/r/c/a/b/+/123/4/file.py", "https://g/r/#/c/123/4"):
                self.assertEqual(parse_reference(value, "https://g/r"), ("123", 4))
            self.assertEqual(parse_reference("123", "https://g"), ("123", None))
        def test_foreign_link(self):
            with self.assertRaises(ReviewError): parse_reference("https://evil/c/x/+/1", "https://g")
        def test_prefix(self):
            with self.assertRaises(ReviewError): parse_reference("https://g/other/c/x/+/1", "https://g/r")
        def test_ranges(self):
            validate_range("abc\nxyz\n", span=dict(start_line=1, start_character=1, end_line=2, end_character=0))
            for line in (0, -1, 3):
                with self.assertRaises(ReviewError): validate_range("a\nb\n", line=line)
        def test_utf16(self):
            validate_range("a😀z", span=dict(start_line=1, start_character=1, end_line=1, end_character=3))
            with self.assertRaises(ReviewError): validate_range("a😀z", span=dict(start_line=1, start_character=1, end_line=1, end_character=2))
        def test_empty_range(self):
            with self.assertRaises(ReviewError): validate_range("abc", span=dict(start_line=1, start_character=1, end_line=1, end_character=1))
        def test_json(self):
            self.assertEqual(parse_json(b")]}'\n{\"x\":1}"), {"x": 1})
            with self.assertRaises(ReviewError): parse_json(b"<html>login</html>")
        def test_paths(self):
            for value in ("../key", "/etc/passwd", "a/../key", "a//b"):
                with self.assertRaises(ReviewError): safe_path(value)
            safe_path("src/file name.py")
        def test_atomic(self):
            with tempfile.TemporaryDirectory() as directory:
                p = Path(directory) / "p.json"
                save(p, {"text": "日本語"})
                self.assertEqual(load(p)["text"], "日本語")
        def test_plan_is_local(self):
            with tempfile.TemporaryDirectory() as directory:
                p = Path(directory) / "p.json"
                save(p, {"comments": [], "labels": {}, "message": ""})
                args = argparse.Namespace(command="comment", plan=p, path="x.py", side="REVISION", line=None, range="1:0-1:2", text="Bug", text_file=None, resolved=False)
                self.assertFalse(edit_plan(args)["published"])
                self.assertEqual(load(p)["comments"][0]["range"]["end_character"], 2)
                save(receipt_path(p), {"state": "pending"})
                with self.assertRaises(ReviewError): edit_plan(args)
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(Tests))
    require(result.wasSuccessful(), "Self-tests failed.")
    return {"passed": result.testsRun}


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version", version=VERSION)
    p.add_argument("--url", default=os.environ.get("GERRIT_URL"))
    p.add_argument("--username", default=os.environ.get("GERRIT_USER"))
    p.add_argument("--auth", choices=("basic", "bearer", "anonymous"), default=os.environ.get("GERRIT_AUTH", "basic"))
    p.add_argument("--credential-env", default="GERRIT_HTTP_PASSWORD")
    p.add_argument("--credential-file")
    p.add_argument("--ask-credential", action="store_true")
    p.add_argument("--ca-file", default=os.environ.get("GERRIT_CA_FILE"))
    p.add_argument("--timeout", type=float, default=60)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("self-test", help="Run embedded offline tests")
    sub.add_parser("doctor", help="Check TLS, identity, and server version")
    ls = sub.add_parser("list", help="Query changes; paginated, explicitly capped")
    ls.add_argument("--query", required=True)
    ls.add_argument("--limit", type=int, default=100)
    for name in ("get", "prepare"):
        s = sub.add_parser(name, help="Read change metadata" if name == "get" else "Prepare full local code and review bundle")
        s.add_argument("change")
        s.add_argument("--patchset", type=int)
        if name == "prepare":
            s.add_argument("--output", type=Path, required=True)
            s.add_argument("--workspace", type=Path)
            s.add_argument("--git-url")
            s.add_argument("--parent", type=int)
    for name in ("show", "comments", "plan"):
        s = sub.add_parser(name)
        s.add_argument("--bundle", type=Path, required=True)
        if name == "show":
            s.add_argument("--path", required=True)
            s.add_argument("--side", choices=("baseline", "base", "head"), default="head")
            s.add_argument("--start", type=int, default=1)
            s.add_argument("--end", type=int)
        elif name == "plan":
            s.add_argument("--out", type=Path, required=True)
            s.add_argument("--notify", choices=("NONE", "OWNER", "OWNER_REVIEWERS", "ALL"), default="OWNER")
    for name in ("comment", "reply", "vote", "message", "validate", "publish", "status"):
        s = sub.add_parser(name)
        s.add_argument("--plan", type=Path, required=True)
        if name in ("comment", "reply", "message"):
            group = s.add_mutually_exclusive_group(required=True)
            group.add_argument("--text")
            group.add_argument("--text-file", type=Path)
        if name == "comment":
            s.add_argument("--path", required=True)
            s.add_argument("--side", choices=("REVISION", "PARENT"), default="REVISION")
            s.add_argument("--line", type=int)
            s.add_argument("--range")
        if name in ("comment", "reply"):
            s.add_argument("--resolved", action="store_true")
        if name == "reply":
            s.add_argument("--comment-id", required=True)
            s.add_argument("--path", help="Verified current path for an older-patch-set reply")
            s.add_argument("--side", choices=("REVISION", "PARENT"), default="REVISION")
            s.add_argument("--line", type=int)
            s.add_argument("--range")
        if name == "vote":
            s.add_argument("--label", default="Code-Review")
            s.add_argument("--score", required=True, type=int)
        if name == "publish":
            s.add_argument("--send", action="store_true", help="Actually POST this authorized review; omitted means dry run")
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "self-test":
        return self_tests()
    if args.command in ("comment", "reply", "vote", "message"):
        return edit_plan(args)
    client = Client(args)
    if args.command == "doctor":
        return {"server": client.base, "version": client.request("/config/server/version"), "identity": client.request("/accounts/self") if client.auth != "anonymous" else "anonymous", "tls_verified": client.base.startswith("https"), "custom_ca": client.ca, "auth_mode": client.auth}
    if args.command == "list":
        require(args.limit > 0, "Limit must be positive.")
        results, seen = [], set()
        more = True
        while more and len(results) < args.limit:
            page = client.request("/changes/", [("q", args.query), ("n", min(100, args.limit - len(results))), ("S", len(results))])
            signature = tuple(c["_number"] for c in page)
            require(signature not in seen, "Server repeated a query page.")
            seen.add(signature)
            results.extend(page)
            more = bool(page and page[-1].get("_more_changes"))
        return {"changes": results, "truncated": more, "next_offset": len(results)}
    if args.command == "get":
        detail, revision = resolve(client, args.change, args.patchset)
        return {"summary": summary(detail, revision), "detail": detail, "comments": client.request(f"/changes/{detail['_number']}/comments")}
    if args.command == "prepare":
        return prepare(client, args)
    if args.command in ("show", "comments", "plan"):
        bundle = bundle_at(args.bundle)
        require(bundle["server"] == client.base, "Bundle belongs to a different configured server.")
        if args.command == "comments":
            return list(collect_comments(client, bundle).values())
        if args.command == "show":
            sha = bundle["revision"] if args.side == "head" else bundle[args.side]
            raw = blob(client, bundle, sha, args.path)
            lines = raw.decode("utf-8", "strict").split("\n")
            if lines and lines[-1] == "":
                lines.pop()
            end = args.end if args.end is not None else len(lines)
            require(args.start >= 1 and end >= args.start - 1, "Invalid line window.")
            return {"path": args.path, "side": args.side, "commit": sha, "total_lines": len(lines), "lines": [{"line": n, "text": lines[n-1], "utf16_length": len(lines[n-1].encode('utf-16-le')) // 2} for n in range(args.start, min(end, len(lines)) + 1)]}
        require(not args.out.exists(), "Plan already exists; choose a new filename.")
        plan = {"schema": 1, "bundle": str(args.bundle.resolve()), "server": client.base, "number": bundle["number"], "revision": bundle["revision"], "account_id": client.request("/accounts/self")["_account_id"], "tag": "autogenerated:gerrit-review-workbench:" + uuid.uuid4().hex,
                "message": "", "labels": {}, "comments": [], "notify": args.notify}
        save(args.out, plan)
        return {"plan": str(args.out), "published": False, "permitted_labels": bundle["permitted_labels"]}
    if args.command == "validate":
        bundle, payload = checked_payload(client, load(args.plan))
        return {"valid": True, "revision": bundle["revision"], "payload": payload}
    if args.command == "status":
        return posting_status(client, load(args.plan))
    return publish(client, args)


if __name__ == "__main__":
    try:
        print(json.dumps(main(), ensure_ascii=False, indent=2))
    except (ReviewError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({"error": str(exc) if isinstance(exc, ReviewError) else f"{type(exc).__name__}: invalid input, local file, encoding, or CA configuration", "posted": "unknown if a receipt is pending; run status"}), file=sys.stderr)
        sys.exit(1)
