#!/usr/bin/env python3
"""Gerrit implementation workbench. Python 3.10+, Git, Linux/macOS; standard library only.

Run --help or COMMAND --help. See the adjacent SKILL.md for the complete workflow.
Only `push --send` writes to Gerrit; other commands prepare local work.
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

VERSION = "1.1.0"
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
        require(args.url, "Set url in the config, GERRIT_URL, or --url to the Gerrit installation base URL.")
        self.base = args.url.rstrip("/")
        parsed = urlparse.urlsplit(self.base)
        require(parsed.scheme in ("http", "https") and parsed.hostname, "Gerrit URL must be HTTP(S).")
        require(not (parsed.username or parsed.password or parsed.query or parsed.fragment), "Do not embed credentials/query/fragment in the base URL.")
        require(not self.base.endswith("/a"), "Use the installation base URL without /a.")
        self.auth = args.auth
        self.ca = str(Path(args.ca_file).expanduser().resolve()) if args.ca_file else None
        self.username = args.username
        self.secret = getattr(args, "http_password", None)
        if self.secret is None:
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
        headers = {"Accept": "text/plain" if raw else "application/json", "User-Agent": "gerrit-implementation-workbench/" + VERSION}
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


def output_git(client, repo, *args):
    return client.git(*args, cwd=repo).stdout.decode().strip()


def commit_message(text, change_id):
    text = text.strip()
    require(text and '\x00' not in text, 'Provide a nonempty commit message.')
    require(not re.search(r'^Change-Id:', text, re.M | re.I), 'Omit Change-Id: the helper preserves it automatically.')
    require(len(text.splitlines()) == 1 or not text.splitlines()[1].strip(), 'Separate title and body with a blank line.')
    return text + '\n\nChange-Id: ' + change_id + '\n'


def validate_branch(client, branch):
    require(not branch.startswith('refs/') and '%' not in branch and ',' not in branch,
            'Use a plain branch name such as main or release/1; push options are forbidden.')
    require(client.git('check-ref-format', 'refs/heads/' + branch, check=False).returncode == 0, 'Invalid target branch.')


def start(client, args):
    account = client.request('/accounts/self/detail')
    email = args.email or account.get('email')
    name = args.name or account.get('name') or account.get('username')
    require(email and name, 'Set --name and --email to your Gerrit registered author identity.')
    detail = None
    if args.command == 'resume':
        detail, sha = resolve(client, args.change)
        require(detail['status'] == 'NEW' and sha == detail['current_revision'], 'Resume only an open change at its current patch set.')
        project, branch = detail['project'], detail['branch']
    else:
        project, branch = args.project, args.branch
    validate_branch(client, branch)
    require(project and not any(p in ('', '.', '..') for p in project.split('/')), 'Invalid project name.')
    directory = args.task.resolve()
    require(not directory.exists(), 'Task directory already exists. Reuse its status/commit/push commands or choose a new directory.')
    directory.mkdir(parents=True, mode=0o700)
    repo = directory / 'repo'
    url = client.base + client.prefix + '/' + q(project)
    if args.workspace and args.workspace.exists():
        client.git('clone', '--no-local', '--no-hardlinks', '--no-checkout', str(args.workspace.resolve()), str(repo))
        client.git('remote', 'set-url', 'origin', url, cwd=repo)
    else:
        client.git('clone', '--no-checkout', url, str(repo))
    client.git('fetch', url, 'refs/heads/' + branch, cwd=repo)
    base = output_git(client, repo, 'rev-parse', 'FETCH_HEAD')
    expected = None
    if detail:
        client.git('fetch', url, detail['revisions'][sha]['ref'], cwd=repo)
        require(output_git(client, repo, 'rev-parse', 'FETCH_HEAD') == sha, 'Fetched revision does not match Gerrit.')
        parents = output_git(client, repo, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
        require(len(parents) == 1, 'Resume supports a single-parent change; root/merge changes require a project-specific workflow.')
        base, expected = parents[0], sha
    client.git('checkout', '-b', 'gerrit-task-' + uuid.uuid4().hex[:12], expected or base, cwd=repo)
    client.git('config', 'user.name', name, cwd=repo)
    client.git('config', 'user.email', email, cwd=repo)
    state = dict(server=client.base, account=account['_account_id'], project=project, branch=branch,
                 base=base, head=expected or base, committed=bool(detail), expected=expected,
                 change_id=detail['change_id'] if detail else 'I' + hashlib.sha1(os.urandom(32)).hexdigest(),
                 number=detail['_number'] if detail else None, url=url, pending=None)
    save(directory / 'task.json', state)
    return dict(task=str(directory), repo=str(repo), state=state,
                next='Read project instructions and full relevant files in repo, implement, test, then commit explicit paths.')


def get_task(client, directory):
    directory = directory.resolve()
    state = load(directory / 'task.json')
    require(state['server'] == client.base, 'Task belongs to another Gerrit URL.')
    account = client.request('/accounts/self/detail')
    require(state['account'] == account['_account_id'], 'Use the account that created this task.')
    return state, directory / 'repo'


def remote_state(client, state):
    matches = client.request('/changes/', [('q', 'change:' + state['change_id']), ('n', '100')])
    require(not any(x.get('_more_changes') for x in matches), 'Ambiguous Change-Id search; more than 100 results.')
    matches = [x for x in matches if x['project'] == state['project'] and x['branch'] == state['branch']]
    require(len(matches) <= 1, 'Multiple matching changes; resolve manually.')
    return client.detail(matches[0]['_number']) if matches else None


def current_commit(client, repo, state):
    sha = output_git(client, repo, 'rev-parse', 'HEAD')
    require(sha == state['head'], 'HEAD changed outside the helper. Preserve work and resume in a new task directory.')
    if state['committed']:
        parents = output_git(client, repo, 'rev-list', '--parents', '-n', '1', sha).split()[1:]
        require(parents == [state['base']], 'Task must contain exactly one commit above its recorded base.')
        msg = output_git(client, repo, 'log', '-1', '--format=%B')
        require(re.findall(r'^Change-Id: (.+)$', msg, re.M) == [state['change_id']], 'Change-Id changed or duplicated.')
    return sha


def operate(client, args):
    directory = args.task.resolve()
    with (directory / '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state, repo = get_task(client, directory)
        sha = current_commit(client, repo, state)
        if args.command == 'status':
            remote = remote_state(client, state)
            return dict(local=state, repo=str(repo), working_tree=output_git(client, repo, 'status', '--short'),
                        remote=remote, pending=state['pending'])
        if args.command == 'diff':
            return dict(committed=output_git(client, repo, 'diff', state['base'], 'HEAD'),
                        unstaged=output_git(client, repo, 'diff'), staged=output_git(client, repo, 'diff', '--cached'),
                        untracked=output_git(client, repo, 'ls-files', '--others', '--exclude-standard'))
        if args.command == 'commit':
            require(not state['pending'], 'A push outcome is pending. Run push to reconcile before editing the commit.')
            message = commit_message(args.message_file.read_text(), state['change_id'])
            require(not output_git(client, repo, 'diff', '--cached', '--name-only'), 'Index already contains staged changes; inspect and unstage them before using commit.')
            for path in args.path:
                require(path and not path.startswith(('/', ':')) and all(p not in ('', '.', '..', '.git') for p in path.split('/')), 'Stage explicit relative file paths, without traversal or pathspec syntax.')
                target = repo / path
                require(not target.is_dir(), 'Specify individual files, not directories.')
                require(target.parent.resolve().is_relative_to(repo.resolve()), 'File parent escapes task repository.')
            client.git('--literal-pathspecs', 'add', '--', *args.path, cwd=repo)
            changed = output_git(client, repo, 'diff', '--cached', '--name-only')
            require(changed, 'No selected file changes to commit.')
            argv = ['commit', '--no-gpg-sign', '-F', '-']
            if state['committed']:
                argv.append('--amend')
            client.git(*argv, cwd=repo, data=message.encode())
            state.update(head=output_git(client, repo, 'rev-parse', 'HEAD'), committed=True)
            save(directory / 'task.json', state)
            return dict(head=state['head'], change_id=state['change_id'], changed=changed, next='Inspect diff, then push preview and push --send.')
        require(state['committed'], 'Commit the implementation before pushing.')
        require(not output_git(client, repo, 'status', '--porcelain'), 'Working tree has uncommitted/untracked files. Commit intended files and move test artifacts outside repo or use project ignore rules.')
        remote = remote_state(client, state)
        if remote and sha in remote['revisions']:
            state.update(expected=sha, number=remote['_number'], pending=None)
            save(directory / 'task.json', state)
            return dict(already_uploaded=True, number=remote['_number'], patchset=remote['revisions'][sha]['_number'], current=remote['current_revision'] == sha)
        require(not state['pending'], 'Previous push outcome is uncertain and revision is not yet visible. Do not automatically retry. Inspect Gerrit/server logs and preserve task.json.')
        if remote:
            require(remote['status'] == 'NEW', 'Change is closed; do not create a replacement implicitly.')
            require(state['number'] == remote['_number'] and state['expected'] == remote['current_revision'], 'Remote patch set changed. Resume latest change in a new task directory and reapply intended edits.')
        else:
            require(state['expected'] is None, 'Previously uploaded change is no longer visible.')
        # Reject accidental stacks and stale/divergent branch bases before uploading.
        client.git('fetch', state['url'], 'refs/heads/' + state['branch'], cwd=repo)
        require(client.git('merge-base', '--is-ancestor', state['base'], 'FETCH_HEAD', cwd=repo, check=False).returncode == 0,
                'Recorded base is not on the target branch. Stacked changes or rewritten branches require a project-specific workflow.')
        ref = 'refs/for/' + state['branch'] + '%no-publish-comments'
        if not args.send:
            return dict(dry_run=True, commit=sha, change_id=state['change_id'], destination=ref, project=state['project'])
        state['pending'] = sha
        save(directory / 'task.json', state)
        result = client.git('push', state['url'], sha + ':' + ref, cwd=repo, check=False)
        for attempt in range(5):
            remote = remote_state(client, state)
            if remote and sha in remote['revisions']:
                state.update(expected=sha, number=remote['_number'], pending=None)
                save(directory / 'task.json', state)
                require(remote['current_revision'] == sha, 'Uploaded, but another patch set became current. Run status before continuing.')
                return dict(number=remote['_number'], patchset=remote['revisions'][sha]['_number'], commit=sha,
                            change_id=state['change_id'], link=client.base + '/c/' + q(state['project']) + '/+/' + str(remote['_number']))
            time.sleep(1)
        raise ReviewError('Push not confirmed (Git exit ' + str(result.returncode) + '). Pending receipt retained. Run status and push to reconcile; do not blindly resend. Check upload permissions and Gerrit validation rules.')


class ConfigParser(argparse.ArgumentParser):
    """Load shared connection defaults, then let explicit CLI options override."""
    def parse_args(self, args=None, namespace=None):
        probe = argparse.ArgumentParser(add_help=False)
        probe.add_argument('--config')
        selected, _ = probe.parse_known_args(args)
        explicit = selected.config or os.environ.get('GERRIT_CONFIG')
        path = Path(explicit or '~/.config/gerrit-agent/config.json').expanduser().resolve()
        defaults = {}
        if explicit or path.exists():
            try:
                config = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                raise ReviewError('Cannot read config JSON. Check the file path and JSON syntax.') from None
            require(isinstance(config, dict), 'Config must be a JSON object.')
            allowed = {'url', 'username', 'http_password', 'auth', 'ca_file', 'timeout', 'credential_file', 'credential_env'}
            require(not (set(config) - allowed), 'Config has unsupported keys; see the example config.')
            for key, value in config.items():
                if key == 'timeout':
                    require(type(value) in (int, float) and 0 < value < float('inf'), 'Config timeout must be a positive finite number.')
                else:
                    require(isinstance(value, str), 'Config connection fields must be strings; omit unused fields.')
                if key in ('ca_file', 'credential_file') and value:
                    candidate = Path(value).expanduser()
                    value = str((path.parent / candidate).resolve()) if not candidate.is_absolute() else str(candidate)
                defaults[key] = value
            require(config.get('auth', 'basic') in ('basic', 'bearer', 'anonymous'), 'Config auth must be basic, bearer, or anonymous.')
        self.set_defaults(**defaults)
        return super().parse_args(args, namespace)


def parser():
    p = ConfigParser(description=__doc__)
    p.add_argument("--config", help="Shared JSON config; defaults to GERRIT_CONFIG or ~/.config/gerrit-agent/config.json")
    p.add_argument('--url', default=os.getenv('GERRIT_URL'))
    p.add_argument('--username', default=os.getenv('GERRIT_USER'))
    p.add_argument('--auth', choices=('basic', 'bearer', 'anonymous'), default=os.getenv('GERRIT_AUTH', 'basic'))
    p.add_argument('--credential-env', default='GERRIT_HTTP_PASSWORD')
    p.add_argument('--credential-file')
    p.add_argument('--ask-credential', action='store_true')
    p.add_argument('--ca-file', default=os.getenv('GERRIT_CA_FILE'))
    p.add_argument('--timeout', type=int, default=60)
    sub = p.add_subparsers(dest='command', required=True)
    sub.add_parser('doctor')
    for command in ('start', 'resume', 'status', 'diff', 'commit', 'push'):
        s = sub.add_parser(command)
        s.add_argument('--task', type=Path, required=True)
        if command in ('start', 'resume'):
            s.add_argument('--workspace', type=Path)
            s.add_argument('--name')
            s.add_argument('--email')
            if command == 'start':
                s.add_argument('--project', required=True)
                s.add_argument('--branch', required=True)
            else:
                s.add_argument('change')
        if command == 'commit':
            s.add_argument('--path', action='append', required=True)
            s.add_argument('--message-file', type=Path, required=True)
        if command == 'push':
            s.add_argument('--send', action='store_true')
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    client = Client(args)
    if args.command == 'doctor':
        return dict(account=client.request('/accounts/self/detail'), tls_verified=True, git=client.git('--version').stdout.decode().strip())
    if args.command in ('start', 'resume'):
        return start(client, args)
    return operate(client, args)


if __name__ == '__main__':
    try:
        print(json.dumps(main(), indent=2, ensure_ascii=False))
    except (ReviewError, OSError, ValueError, KeyError) as exc:
        print(json.dumps({'error': str(exc)}), file=sys.stderr)
        sys.exit(1)
