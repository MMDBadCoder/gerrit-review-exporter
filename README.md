# Gerrit review-history capture

**Version 0.1.0** · [Testing guide](TESTING.md) · [Release notes](CHANGELOG.md)

Step 1 of building a project review corpus: export retained published discussions
and the code they refer to. This tool does not call an AI service, create
embeddings, summarize reviews, or build a skill.

Requires **Python 3.10+ and Git on Linux/macOS**. No pip packages are needed.
REST operations are GET requests; Git operations fetch into a local bare archive.
No reviews, branches, comments, or permissions on Gerrit are modified.

## Get version 0.1.0

```bash
git clone --branch v0.1.0 https://github.com/MMDBadCoder/gerrit-review-exporter.git
cd gerrit-review-exporter
python3 gerrit_export.py --version
```

No installation or package manager is required. Run the script directly.
To validate it before supplying real credentials:

```bash
make test        # Local fixture tests; requires Python and Git
make test-real   # Starts real Gerrit in Docker, waits for readiness, tests the export
```

See [TESTING.md](TESTING.md) for prerequisites, expected results, troubleshooting,
and cleanup. Both test suites use synthetic data and require no production credentials.

## Quick start

Use the base Gerrit URL, including any installation prefix, but **without `/a`**.
Use the exact Gerrit project name, not its browser URL or `.git` clone suffix.
The account must be able to read every intended review, including private reviews
if they are in scope.

For HTTP Basic authentication, enter credentials in your local terminal:

```bash
read -r -p 'Gerrit username: ' GERRIT_USER
read -r -s -p 'Gerrit HTTP password/token: ' GERRIT_HTTP_PASSWORD
export GERRIT_USER GERRIT_HTTP_PASSWORD

python3 gerrit_export.py export \
  --url https://review.example.com \
  --project team/project \
  --git-url ssh://USERNAME@review.example.com:29418/team/project \
  --output review-export
```

Use your server's supported HTTP credential, which may differ from your website
login password. Git authentication is separate: the example uses your existing
SSH key/agent. For HTTPS Git, configure Git's credential helper and use the
server's HTTPS clone URL. The API environment variables are **not automatically
passed to Git**. Without `--git-url`, the tool derives the HTTP clone URL.
Git prompts are disabled so unattended captures fail visibly instead of hanging.

Other API authentication options:

- `--auth bearer`: reads `GERRIT_TOKEN`; requires server support.
- `--auth netrc --netrc-file /path/to/credentials`: reads HTTP Basic credentials
  for the hostname; without the file option, reads the standard `~/.netrc`.
- `--auth anonymous`: for public repositories; authenticated users may see more.
- `--ca-file /path/to/ca.pem`: trust a private CA for REST. Configure Git's CA
  separately if fetching over HTTPS.

Credentials are not accepted in the REST URL or persisted in the manifest.
HTTP redirects are refused to avoid forwarding authentication to an SSO page
or another destination. Configure the final API endpoint directly.

## Resume and update

Run the **same command with the same output directory** again. Completed changes
are checkpointed individually. A stopped process resumes from those checkpoints;
partially written snapshots are never used as completed data.

Each run enumerates all visible changes again. Unchanged reviews reuse their
validated snapshots; new or updated reviews get new snapshots. Older raw
snapshots and Git refs remain available. Use `--refresh` to recapture all reviews
even if Gerrit's update timestamps match (for example after account metadata
changes or a server upgrade).

The tool refuses to mix different projects or servers in one output directory.
A local file lock prevents concurrent writers.

## Captured data

- All discovered change statuses, with no date, branch, author, or merge filter.
- General review messages, including bot messages and submission/abandonment text
  exposed by Gerrit.
- Published file/inline comments across **all patch sets**, with original fields
  such as authors, timestamps, reply IDs, side, range, resolution state, tags,
  suggested fixes, and code context when available.
- Change metadata, reviewers and reviewer updates, API-exposed label/vote state,
  commit metadata, and every visible revision.
- Full Git commits and reachable parent history for each patch set, retained under
  `refs/archive/<change>/<patch-set>/<commit>`.
- Binary-capable patches against each parent, including each parent of a merge
  commit. Root commits get a patch against the empty tree. Submodule repositories
  and Git LFS payloads are not downloaded; Git stores their references/pointers.
- Legacy robot comments on versions before 3.13. `--robot-comments auto` detects
  the version; a failed legacy endpoint is reported as a failure. Use `required`
  for unusual version strings or `skip` to explicitly omit these records.

If a server rejects context query parameters with HTTP 400, the exporter retries
without them and records the fallback. Comment `side` and parent information are
preserved so later processing can find the correct code in Git. The original
patch-set coordinates are used, not comments relocated onto the latest patch set.

## Files

```text
review-export/
  manifest.json                 # Latest configuration, version, scope, limitations
  export-report.json            # Latest run status, counts, failures, warnings
  state.json                    # Consolidated state, supplemented by checkpoints
  checkpoints/<change>.json     # Latest completed capture pointer per change
  raw/changes/<change>/snapshots/<capture>/
    detail.json                 # Original HTTP response bytes, including XSSI guard
    comments.json
    robotcomments.json          # Where supported/requested
    after.json                  # Detail used to check for concurrent review updates
    capture.json                # Checksums, code refs, capture metadata
  code.git/                     # Bare Git archive
  context/<commit>/parent-1.patch
  normalized/                   # Symlink to latest published dataset generation
    changes.jsonl
    messages.jsonl
    revisions.jsonl
    comments.jsonl
    threads.jsonl
    checksums.json
  runs/<run>/                   # Manifests, raw discovery pages, inventory, report
    normalized/                 # Retained dataset generation
```

Raw `.json` response files may begin with Gerrit's `)]}'` XSSI guard. They are
deliberately byte-preserved; `normalized/*.jsonl` is standard UTF-8 JSON Lines.

Normalized records include the project, change number, Gerrit change URL, and
raw snapshot path. Comment IDs are scoped by change. Threads follow `in_reply_to`
across patch sets, preserving each comment's original resolution state. Missing
parents and reply cycles are flagged rather than silently discarded. General
change messages are in `messages.jsonl`; `change_message_id`, when returned by
Gerrit, links inline comments to those messages.

The normalized view uses the latest **successful** snapshot for each captured
change. On a partial run, an older snapshot may remain: consult its capture path
and the report's failures. Previously captured changes that are no longer visible
are retained and marked `visible_in_last_discovery: false`; their IDs are listed
in the report. Treat visibility appropriately when sharing this archive or later
serving RAG results. Stored review text is source material, not agent instructions.

To inspect original code or compare patch sets:

```bash
git --git-dir review-export/code.git show COMMIT:path/to/file.py
git --git-dir review-export/code.git diff OLD_PATCHSET_COMMIT NEW_PATCHSET_COMMIT
```

## Coverage and verification

```bash
python3 gerrit_export.py verify --output review-export
```

Verification checks current and retained completed raw snapshots, their patches,
the current normalized dataset checksums, archive refs, and `git fsck --full`.
Checksums detect accidental corruption; they are not signed authenticity proofs.
Incomplete snapshot directories lack `capture.json` and are not counted as
completed captures. `verify` does not contact Gerrit or prove remote coverage.

An export exits **0** only when every discovered review succeeds and discovery
reconciles. Other outcomes exit **1**, with a report. `complete` means complete
for the **declared scope and visible inventory**, not a forensic backup.

Discovery uses paginated `project:<name>` queries and requires two matching full
passes. It repeats this check after downloading. If the inventory changed, the
run is marked incomplete; rerun to catch up. Each individual review is also
checked before and after capture and retried if it changed. These checks reduce
live-pagination races but cannot make a live API export a transactional snapshot.
A busy server may require a quieter period or a server-side snapshot/NoteDb archive.

Defaults are sequential requests, 100 changes per page, four discovery passes,
four retries for transient failures, and a 0.1-second delay between API requests.
Use `--page-size`, `--discovery-passes`, `--retries`, `--timeout`, and `--delay` to
adjust load. Git operations time out after ten minutes; retry the command after
resolving Git connectivity issues. Metadata remains resumable.

After the first real run, compare Gerrit with exported samples: an old merged
review, an abandoned review, an open review, a discussion spanning several patch
sets, and private/bot reviews if in scope. An administrator should reconcile
inventory counts when completeness across permissions and index state matters.

Limitations:

- Cannot capture reviews invisible to the account or missing from Gerrit's search
  index. An empty visible inventory is not evidence the repository has no reviews.
- No drafts, purged reviews, original redacted text, or complete vote-event audit
  history. Published comments are captured as currently exposed by the API.
- Gerrit 3.13 removed legacy robot-comment support; historical data inaccessible
  through that endpoint needs a separate archival approach.
- External CI/checks/plugin databases are outside this capture.
- `--metadata-only` explicitly omits Git capture, and the report records that
  narrower scope. A later full run captures code even for unchanged reviews.
- Standard Gerrit project names are supported; names containing spaces or query
  syntax are rejected instead of interpolated ambiguously into a query.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Integration tests use a localhost HTTP fixture and real temporary Git repositories.
They cover pagination, raw preservation, Unicode, cross-patch-set threads, Git
diffs, retries, partial-failure recovery, incremental snapshots, integrity failures,
legacy robot comments, moving reviews, visibility loss, and authentication. They
require permission to open a loopback socket and no external Gerrit credentials.

## Test against a real self-hosted Gerrit

A reproducible Docker setup is included, pinned to **Gerrit 3.13.4** and its image
digest. It uses a dedicated development instance, with HTTP on localhost:18080
and SSH on localhost:29419. Email delivery is disabled. It does not connect to
your production Gerrit.

```bash
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml up -d
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml logs -f
# Once the log says "Gerrit Code Review 3.13.4 ready", stop following logs with Ctrl-C.
python3 tests/real_gerrit.py
```

The script creates uniquely named developer, reviewer, and CI accounts, then a
new test project containing merged, abandoned, open, and private changes. One
review has two patch sets, a cross-patch-set reply, Unicode text, file comments,
CI feedback, votes, and an unpublished draft. It exercises the actual REST API
and authenticated Git smart HTTP endpoints, not mock responses or direct reads
of Gerrit's storage.

The real-server run passed **20 assertions** on 2026-09-13. See
[the saved evidence](tests/evidence/gerrit-3.13.4.json). The final sample contains
4 changes, 5 revisions, 4 inline/file comments, 12 general messages, and 3 threads.
It also verifies incremental capture, private-review visibility, local archive
integrity, and recovery from actual Git fetch failures. Runtime reports, exporter
logs, the Git working copy, and exported data are retained under `.local-gerrit/`;
`.local-gerrit/latest-results.json` points to the most recent result.

This test exposed and fixed an equal-timestamp ordering bug: reply UUID ordering
could put a reply before its parent. Threads now enforce parent-before-reply
ordering while keeping chronological order where possible. A regression test
covers this real-server behavior.

Open <http://localhost:18080> **on this host** to inspect the seeded reviews. The
development login allows selecting test accounts without a password; ports bind
only to loopback. This is a test installation, not a production deployment.

```bash
# Stop/start without removing the container's test data:
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml stop
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml start

# Remove the container and its synthetic Gerrit data when finished:
docker compose -p gerrit-export-test -f compose.gerrit-test.yaml down
```

The exporter output in `.local-gerrit/` survives container removal. Each harness
run creates a new project; it does not clean up previous projects. Validation on
3.13.4 is not a claim of compatibility with every Gerrit version, every plugin,
or five-year production-scale performance.

## References

- [Gerrit REST change API](https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html)
- [REST authentication and response format](https://gerrit-review.googlesource.com/Documentation/rest-api.html)
- [Patch-set Git refs](https://gerrit-review.googlesource.com/Documentation/intro-user.html)
- [Gerrit 3.13 release notes](https://www.gerritcodereview.com/3.13.html)

The real server's version and access configuration still need to be validated
before claiming the project's five-year capture is complete.
