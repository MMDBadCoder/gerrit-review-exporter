#!/usr/bin/env python3
"""Read-only Gerrit review capture. Python 3.10+, Git; no third-party modules."""
from __future__ import annotations

import argparse
import base64
import collections
import contextlib
import datetime as dt
import email.utils
import fcntl
import hashlib
import heapq
import http.client
import json
import netrc
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid


__version__ = "0.1.0"
SCHEMA = 1
OPTIONS = ["MESSAGES", "ALL_REVISIONS", "ALL_COMMITS", "DETAILED_ACCOUNTS",
           "DETAILED_LABELS", "REVIEWER_UPDATES"]


class ExportError(Exception):
    pass


class HTTPFailure(ExportError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}; check access, server version, and endpoint support")


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def file_digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".writing-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path, default=None):
    return json.loads(path.read_bytes()) if path.exists() else default


def parse_response(raw):
    # Gerrit's XSSI guard is not part of the JSON document.
    if raw.startswith(b")]}'"):
        raw = raw.split(b"\n", 1)[1] if b"\n" in raw else b""
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError) as exc:
        raise ExportError("Expected Gerrit JSON; check URL and authentication (possibly an SSO page)") from exc


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward authentication to an SSO or a different host.
        return None


class Client:
    def __init__(self, args):
        self.base = args.url.rstrip("/")
        parsed = urllib.parse.urlsplit(self.base)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise ExportError("--url must be an HTTP(S) Gerrit base URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ExportError("Use a base URL without credentials, query, or fragment")
        if self.base.endswith("/a"):
            raise ExportError("Use the Gerrit base URL without the /a authentication prefix")
        self.headers = {"Accept": "application/json", "User-Agent": f"gerrit-history-export/{__version__}"}
        if args.auth == "basic":
            user, password = os.getenv(args.user_env), os.getenv(args.password_env)
            if not user or not password:
                raise ExportError(f"Set {args.user_env} and {args.password_env}, or choose another --auth")
            self.headers["Authorization"] = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        elif args.auth == "bearer":
            token = os.getenv(args.token_env)
            if not token:
                raise ExportError(f"Set {args.token_env}")
            self.headers["Authorization"] = "Bearer " + token
        elif args.auth == "netrc":
            try:
                credentials = netrc.netrc(args.netrc_file).authenticators(parsed.hostname)
            except (OSError, netrc.NetrcParseError) as exc:
                raise ExportError("Cannot read netrc credentials") from exc
            if not credentials or not credentials[2]:
                raise ExportError("No netrc credentials for the Gerrit host")
            self.headers["Authorization"] = "Basic " + base64.b64encode(
                f"{credentials[0]}:{credentials[2]}".encode()).decode()
        if args.auth != "anonymous" and parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ExportError("Authenticated requests require HTTPS")
        self.prefix = "/a" if args.auth != "anonymous" else ""
        self.opener = urllib.request.build_opener(
            NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context(cafile=args.ca_file)))
        self.timeout, self.retries, self.delay = args.timeout, args.retries, args.delay

    def get(self, path, params=(), save=None):
        url = self.base + self.prefix + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        for attempt in range(self.retries + 1):
            retry_after = 0
            try:
                if self.delay:
                    time.sleep(self.delay)
                with self.opener.open(urllib.request.Request(url, headers=self.headers), timeout=self.timeout) as response:
                    raw = response.read()
                value = parse_response(raw)
                if save:
                    atomic(save, raw)
                return value
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
                    raise ExportError("Network request failed after retries; check connectivity and TLS") from None
            time.sleep(min(60, max(retry_after, 2 ** attempt)))


def fingerprint(change):
    return [change.get("updated"), change.get("meta_rev_id")]


def inventory_fingerprint(changes):
    return {key: value.get("updated") for key, value in changes.items()}


def discover(client, args, run):
    previous = None
    for attempt in range(args.discovery_passes):
        found, start, seen_pages = {}, 0, set()
        while True:
            page = client.get("/changes/", [("q", "project:" + args.project),
                              ("n", args.page_size), ("S", start)],
                              run / "discovery" / str(attempt + 1) / f"{start}.json")
            if not isinstance(page, list):
                raise ExportError("Change query returned a non-list")
            if not page:
                break
            signature = tuple((row["_number"], row.get("updated")) for row in page)
            if signature in seen_pages:
                raise ExportError("Pagination repeated a page; refusing to report a complete export")
            seen_pages.add(signature)
            for change in page:
                if change.get("project") != args.project:
                    raise ExportError("Server returned a change outside the requested project")
                number = str(int(change["_number"]))
                found[number] = change
            start += len(page)
            if not page[-1].get("_more_changes", False):
                break
        print(f"Discovery pass {attempt + 1}: {len(found)} changes", flush=True)
        current = inventory_fingerprint(found)
        if previous is not None and current == previous:
            return found
        previous = current
    raise ExportError("Discovery did not stabilize; rerun during a quieter period or increase --discovery-passes")


def git(repo, *args):
    try:
        result = subprocess.run(["git", "--git-dir", str(repo), *args],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}, timeout=600)
    except (OSError, subprocess.TimeoutExpired):
        raise ExportError("Git could not run or timed out") from None
    if result.returncode:
        # Remote stderr can contain credentials/URLs. Do not put it in logs or reports.
        raise ExportError(f"Git {args[0]} failed (exit {result.returncode}); check Git access and local repository")
    return result.stdout


def check_git_url(url):
    parsed = urllib.parse.urlsplit(url)
    if url.startswith("-") or parsed.password or parsed.query or parsed.fragment or "\n" in url:
        raise ExportError("--git-url must not contain passwords, query strings, fragments, or options")
    if parsed.scheme in ("http", "https") and parsed.username:
        raise ExportError("Use Git's credential helper, not credentials embedded in --git-url")


def capture_code(root, detail, remote):
    repo = root / "code.git"
    if not repo.exists():
        git(repo, "init", "--bare", str(repo))
    revisions = []
    for sha, revision in sorted(detail.get("revisions", {}).items(), key=lambda pair: pair[1]["_number"]):
        if not re.fullmatch(r"[0-9a-f]{40,64}", sha):
            raise ExportError("Invalid revision object ID")
        number, patch_set = int(detail["_number"]), int(revision["_number"])
        ref = f"refs/changes/{number % 100:02d}/{number}/{patch_set}"
        # Archive refs are SHA-addressed to preserve previously observed revisions.
        target = f"refs/archive/{number}/{patch_set}/{sha}"
        present = git(repo, "for-each-ref", "--format=%(objectname)", target).decode().strip()
        if present != sha:
            git(repo, "fetch", "--no-tags", "--no-write-fetch-head", remote, f"{ref}:{target}")
        actual = git(repo, "rev-parse", target).decode().strip()
        if actual != sha:
            raise ExportError(f"Patch set {number}/{patch_set} changed during capture; rerun")
        parents = git(repo, "rev-list", "--parents", "-n", "1", sha).decode().split()[1:]
        patches = []
        # Preserve each parent diff for merge commits; root commits use diff-tree --root.
        for i, parent in enumerate(parents or [None], 1):
            relative = f"context/{sha}/parent-{i}.patch"
            path = root / relative
            if parent:
                raw = git(repo, "diff", "--no-ext-diff", "--no-textconv", "--binary", "--full-index", parent, sha, "--")
            else:
                raw = git(repo, "diff-tree", "--root", "--no-commit-id", "-p", "--binary",
                          "--no-ext-diff", "--no-textconv", "--full-index", sha, "--")
            if path.exists():
                if file_digest(path) != hashlib.sha256(raw).hexdigest():
                    raise ExportError("Stored patch differs from Git output; inspect archive corruption or Git configuration")
            else:
                atomic(path, raw)
            patches.append({"parent": parent, "path": relative, "sha256": file_digest(path)})
        revisions.append({"commit": sha, "patch_set": patch_set, "ref": target,
                          "parents": parents, "patches": patches})
    if not revisions:
        raise ExportError("No visible revisions returned for change")
    return revisions


def capture_change(client, args, root, number, run_id):
    endpoint = "/changes/" + number
    for attempt in range(3):
        relative = f"raw/changes/{number}/snapshots/{run_id}-{attempt}"
        folder = root / relative
        folder.mkdir(parents=True, exist_ok=True)
        detail = client.get(endpoint + "/detail", [("o", option) for option in OPTIONS], folder / "detail.json")
        if detail.get("project") != args.project or str(detail.get("_number")) != number:
            raise ExportError("Change detail identity did not match request")
        context = True
        try:
            comments = client.get(endpoint + "/comments", [("enable-context", "true"),
                                  ("context-padding", args.context_lines)], folder / "comments.json")
        except HTTPFailure as exc:
            if exc.status != 400:
                raise
            context = False
            comments = client.get(endpoint + "/comments", save=folder / "comments.json")
        if not isinstance(comments, dict):
            raise ExportError("Comments endpoint returned a non-map")
        seen_ids = set()
        for entries in comments.values():
            if not isinstance(entries, list):
                raise ExportError("Comment file entry is not a list")
            for comment in entries:
                if not isinstance(comment, dict) or not comment.get("id") or comment["id"] in seen_ids:
                    raise ExportError("Missing or duplicate comment ID in response")
                seen_ids.add(comment["id"])
        if args.capture_robots:
            robots = client.get(endpoint + "/robotcomments", save=folder / "robotcomments.json")
            if not isinstance(robots, dict):
                raise ExportError("Robot comments endpoint returned a non-map")
        code = [] if args.metadata_only else capture_code(root, detail, args.git_url)
        after = client.get(endpoint + "/detail", save=folder / "after.json")
        if fingerprint(detail) != fingerprint(after):
            continue
        files = {file.name: file_digest(file) for file in folder.glob("*.json")}
        record = {"snapshot": relative, "updated": detail.get("updated"),
                  "meta_rev_id": detail.get("meta_rev_id"), "captured_at": now(),
                  "context_requested": context, "code": code, "files": files,
                  "robots_captured": args.capture_robots}
        atomic(folder / "capture.json", encode(record))
        return record
    raise ExportError("Review changed during all three capture attempts; rerun to retry")


def load_response(path):
    return parse_response(path.read_bytes())


def order_thread(comments):
    """Chronological where possible, with parents always before replies.

    Gerrit timestamps can tie, including across patch sets. UUID ordering alone
    would then put some replies before the comment they answer.
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
    # Cycles have no topological ordering. Keep their data; the thread flags them.
    ordered.extend(sorted((c for c in comments if c["id"] not in emitted),
                          key=lambda c: (c.get("updated", ""), c["id"])))
    return ordered


def normalized_records(root, state, base, visible):
    for number, record in sorted(state["changes"].items(), key=lambda pair: int(pair[0])):
        folder = root / record["snapshot"]
        detail = load_response(folder / "detail.json")
        common = {"project": detail["project"], "change_number": int(number),
                  "change_url": base + "/c/" + urllib.parse.quote(detail["project"], safe="/") + "/+/" + number,
                  "snapshot": record["snapshot"], "visible_in_last_discovery": number in visible}
        yield "changes", {**detail, **common}
        for message in detail.get("messages", []):
            yield "messages", {**message, **common}
        for sha, revision in detail.get("revisions", {}).items():
            captured = next((item for item in record["code"] if item["commit"] == sha), {})
            yield "revisions", {**revision, **captured, "commit": sha, **common}
        comments = []
        for kind, filename in [("published", "comments.json"), ("robot", "robotcomments.json")]:
            path = folder / filename
            if not path.exists():
                continue
            for filename, entries in load_response(path).items():
                for comment in entries:
                    item = {**comment, "path": filename, "kind": kind, **common}
                    comments.append(item)
                    yield "comments", item
        # IDs are scoped by change. Do not group by file/line: replies may span patch sets.
        by_id = {item["id"]: item for item in comments}
        groups = collections.defaultdict(list)
        for item in comments:
            cursor, seen, trail, missing, cycle = item["id"], {}, [], None, False
            while True:
                if cursor in seen:
                    cursor, cycle = min(trail[seen[cursor]:]), True
                    break
                seen[cursor] = len(trail)
                trail.append(cursor)
                parent = by_id[cursor].get("in_reply_to")
                if not parent:
                    break
                if parent not in by_id:
                    missing, cursor = parent, parent
                    break
                cursor = parent
            groups[cursor].append((item, missing, cycle))
        for thread_id, entries in sorted(groups.items()):
            ordered = order_thread([entry[0] for entry in entries])
            yield "threads", {**common, "thread_id": thread_id, "comments": ordered,
                              "missing_parent_ids": sorted({entry[1] for entry in entries if entry[1]}),
                              "reply_cycle": any(entry[2] for entry in entries)}


def normalize(root, state, base, visible, run):
    counts = collections.Counter()
    names = ["changes", "messages", "revisions", "comments", "threads"]
    out = run / "normalized"
    out.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as stack:
        streams = {name: stack.enter_context((out / f"{name}.jsonl").open("w", encoding="utf-8")) for name in names}
        for kind, item in normalized_records(root, state, base, visible):
            streams[kind].write(json.dumps(item, ensure_ascii=False) + "\n")
            counts[kind] += 1
            if kind == "threads" and (item["missing_parent_ids"] or item["reply_cycle"]):
                counts["threads_with_gaps"] += 1
    atomic(out / "checksums.json", encode({f"{name}.jsonl": file_digest(out / f"{name}.jsonl") for name in names}))
    # A single atomic pointer publishes the complete set, without mixing generations.
    link = root / (".normalized-" + uuid.uuid4().hex)
    link.symlink_to(out.relative_to(root), target_is_directory=True)
    os.replace(link, root / "normalized")
    return dict(counts)


def check_record(root, record, check_code=True):
    for name, expected in record["files"].items():
        path = root / record["snapshot"] / name
        if not path.exists() or file_digest(path) != expected:
            raise ExportError("Raw snapshot missing or checksum mismatch")
    if check_code:
        for revision in record["code"]:
            actual = git(root / "code.git", "rev-parse", revision["ref"]).decode().strip()
            if actual != revision["commit"]:
                raise ExportError("Archived Git ref does not match captured revision")
            for patch in revision["patches"]:
                path = root / patch["path"]
                if not path.exists() or file_digest(path) != patch["sha256"]:
                    raise ExportError("Patch missing or checksum mismatch")


def load_state(root, default=None):
    state = read_json(root / "state.json", default)
    if state is not None:
        for checkpoint in (root / "checkpoints").glob("*.json"):
            state["changes"][checkpoint.stem] = read_json(checkpoint)
    return state


def export(args):
    client = Client(args)
    root = args.output.resolve()
    # Restrict project syntax so it is a literal Gerrit query operand.
    if not re.fullmatch(r"[A-Za-z0-9_.+/@-]+", args.project) or args.project.startswith("^"):
        raise ExportError("Project must be its exact Gerrit name, using letters, digits, _, ., +, /, @, or -")
    state = load_state(root, {"schema": SCHEMA, "url": client.base,
                      "project": args.project, "changes": {}})
    if (state.get("schema"), state.get("url"), state.get("project")) != (SCHEMA, client.base, args.project):
        raise ExportError("Output directory belongs to a different server/project or schema")
    atomic(root / "state.json", encode(state))
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    run = root / "runs" / run_id
    run.mkdir(parents=True)
    report = {"run": run_id, "started_at": now(), "status": "running", "failures": [],
              "scope": "all visible retained published reviews; no drafts",
              "code_requested": not args.metadata_only, "warnings": []}
    atomic(root / "export-report.json", encode(report))
    try:
        version = client.get("/config/server/version", save=run / "server-version.json")
        if not isinstance(version, str):
            raise ExportError("Expected a Gerrit version string")
        match = re.match(r"(?:v)?(\d+)\.(\d+)", version)
        if args.robot_comments == "auto" and not match:
            raise ExportError("Cannot determine robot-comment support; select --robot-comments required or skip")
        args.capture_robots = args.robot_comments == "required" or (
            args.robot_comments == "auto" and tuple(map(int, match.groups())) < (3, 13))
        if not args.capture_robots:
            report["warnings"].append("Legacy robot-comment storage is not captured; support removed in Gerrit 3.13, or explicitly skipped")
        if args.metadata_only:
            report["warnings"].append("Metadata-only export: reviewed Git code and patches are not captured")
        if not args.git_url:
            args.git_url = client.base + client.prefix + "/" + urllib.parse.quote(args.project, safe="/")
        check_git_url(args.git_url)
        manifest = {"schema": SCHEMA, "exporter_version": __version__, "url": client.base, "project": args.project, "server_version": version,
                    "run": run_id, "auth_mode": args.auth, "metadata_only": args.metadata_only,
                    "robot_comments": args.robot_comments, "created_at": now(),
                    "limitations": ["Visibility and search-index completeness depend on the server and account",
                                    "A live API export is not a transactional server backup",
                                    "No other users' drafts, purged data, or external CI/plugin databases",
                                    "Votes are API-exposed state, not a full vote-event audit log",
                                    "Git LFS payloads and submodule repositories are not downloaded"]}
        atomic(run / "manifest.json", encode(manifest))
        atomic(root / "manifest.json", encode(manifest))
        found = discover(client, args, run)
        atomic(run / "inventory.json", encode(found))
        report["discovered_changes"] = len(found)
        report["previously_captured_not_visible"] = sorted(set(state["changes"]) - set(found), key=int)
        report["captured"], report["reused"] = 0, 0
        for position, (number, summary) in enumerate(sorted(found.items(), key=lambda pair: int(pair[0])), 1):
            try:
                old = state["changes"].get(number)
                reuse = old and old["updated"] == summary.get("updated") and not args.refresh
                reuse = reuse and (args.metadata_only or old["code"]) and (not args.capture_robots or old["robots_captured"])
                if reuse:
                    check_record(root, old, check_code=not args.metadata_only)
                    report["reused"] += 1
                else:
                    state["changes"][number] = capture_change(client, args, root, number, run_id)
                    # O(1) checkpoint writes per review, rather than rewriting the entire
                    # five-year inventory for every successful download.
                    atomic(root / "checkpoints" / f"{number}.json", encode(state["changes"][number]))
                    report["captured"] += 1
            except (ExportError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
                report["failures"].append({"change": number, "error": safe_error(exc)})
            if position % 25 == 0 or position == len(found):
                print(f"Reviews {position}/{len(found)}; failures: {len(report['failures'])}", flush=True)
                atomic(root / "export-report.json", encode(report))
        atomic(root / "state.json", encode(state))
        report["counts"] = normalize(root, state, client.base, found, run)
        report["normalized_dataset"] = str((run / "normalized").relative_to(root))
        if any(not record["context_requested"] for record in state["changes"].values()):
            report["warnings"].append("Some snapshots lack requested API context; exact code remains available when Git was captured")
        if report["counts"].get("threads_with_gaps"):
            report["warnings"].append("Some threads have missing parents or cyclic replies; see threads.jsonl")
        # Reconcile after downloading: fail visibly if an update/new change requires another run.
        final_inventory = discover(client, args, run / "reconcile")
        report["inventory_changed_during_capture"] = inventory_fingerprint(found) != inventory_fingerprint(final_inventory)
        if not args.metadata_only and (root / "code.git").exists():
            git(root / "code.git", "fsck", "--full", "--no-reflogs")
        report["status"] = "complete" if not report["failures"] and not report["inventory_changed_during_capture"] else "incomplete"
    except (ExportError, OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        report["status"] = "incomplete"
        report["failures"].append({"error": safe_error(exc)})
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    finally:
        report["finished_at"] = now()
        atomic(run / "report.json", encode(report))
        atomic(root / "export-report.json", encode(report))
    print(f"Export {report['status']}: {root / 'export-report.json'}", flush=True)
    if report["failures"]:
        print(report["failures"][0]["error"], file=sys.stderr)
    return 0 if report["status"] == "complete" else 1


def safe_error(exc):
    return str(exc) if isinstance(exc, ExportError) else f"{type(exc).__name__}: invalid data or local I/O failure"


def verify(args):
    root = args.output.resolve()
    state = load_state(root)
    if not state:
        raise ExportError("No captured state found")
    for record in state["changes"].values():
        check_record(root, record)
    historical = 0
    for marker in (root / "raw" / "changes").glob("*/snapshots/*/capture.json"):
        check_record(root, read_json(marker))
        historical += 1
    normalized = root / "normalized"
    if normalized.exists():
        checksums = read_json(normalized / "checksums.json")
        if not checksums:
            raise ExportError("Normalized dataset checksums missing")
        for name, expected in checksums.items():
            path = normalized / name
            if not path.exists() or file_digest(path) != expected:
                raise ExportError("Normalized dataset checksum mismatch")
    if (root / "code.git").exists():
        git(root / "code.git", "fsck", "--full", "--no-reflogs")
    print(f"Verified {len(state['changes'])} current and {historical} retained snapshots, normalized data, patches, and Git objects")
    print("This checks local integrity; consult export-report.json for capture coverage.")
    return 0


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--version", action="version", version=f"gerrit-review-exporter {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    e = sub.add_parser("export", help="Capture or resume a project")
    e.add_argument("--url", required=True)
    e.add_argument("--project", required=True)
    e.add_argument("--output", type=Path, default=Path("review-export"))
    e.add_argument("--auth", choices=["basic", "bearer", "netrc", "anonymous"], default="basic")
    e.add_argument("--user-env", default="GERRIT_USER")
    e.add_argument("--password-env", default="GERRIT_HTTP_PASSWORD")
    e.add_argument("--token-env", default="GERRIT_TOKEN")
    e.add_argument("--netrc-file")
    e.add_argument("--ca-file", help="PEM CA bundle for private TLS certificates")
    e.add_argument("--git-url", help="Git remote; defaults to Gerrit's HTTP project URL. Uses Git credentials separately")
    e.add_argument("--metadata-only", action="store_true", help="Explicitly omit Git code capture")
    e.add_argument("--robot-comments", choices=["auto", "required", "skip"], default="auto")
    e.add_argument("--page-size", type=int, default=100)
    e.add_argument("--discovery-passes", type=int, default=4)
    e.add_argument("--context-lines", type=int, default=10)
    e.add_argument("--timeout", type=float, default=60)
    e.add_argument("--retries", type=int, default=4)
    e.add_argument("--delay", type=float, default=0.1, help="Seconds between API requests")
    e.add_argument("--refresh", action="store_true", help="Capture fresh snapshots even when update timestamps match")
    v = sub.add_parser("verify", help="Check local raw data, patches, and Git integrity")
    v.add_argument("--output", type=Path, default=Path("review-export"))
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "export" and (args.page_size < 1 or args.discovery_passes < 2 or
            args.context_lines < 0 or args.retries < 0 or args.delay < 0 or args.timeout <= 0):
        raise ExportError("Invalid pagination, context, retry, delay, or timeout argument")
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".export.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ExportError("Another export or verification is using this output directory") from None
        return export(args) if args.command == "export" else verify(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ExportError as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
