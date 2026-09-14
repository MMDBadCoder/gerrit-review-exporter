# Release notes

## 0.3.0

- **`enrich.min_comment_chars`** skips threads with nothing substantial in them.
  The length is measured over the whole thread rather than each comment, because
  a short "Done." is the evidence that a long review point was acted on. Default
  `0`, which keeps everything; `20` drops "LGTM" and bare nits.
- **`gerrit.insecure_tls`** skips certificate and hostname verification, for an
  internal server with a self-signed certificate. Off by default, and documented
  with what it costs: the credential this tool sends is no longer protected from
  a machine in the middle. `ca_file` remains the better answer.
- `rv-out/` and `rv.config.json` are git-ignored, so a working directory does not
  accumulate exports and a config with a real server in it is not committed by
  accident.

## 0.2.2

Documentation only; no behaviour changes.

- Documented `gerrit.netrc_file`, `gerrit.ca_file` and `output.extension`, which
  existed and worked but appeared in no table, and gave `output` its own table so
  `overwrite` is visible.
- Documented the contents of `summary.json`, including that `failed_changes`
  records changes that could not be exported without costing you the rest of the
  run.
- Corrected the unit test count in TESTING.md, which still said 57 after the
  credential tests took it to 63, and listed what those tests cover.

## 0.2.1

- **A Gerrit HTTP credential is now enough on its own.** With `auth: basic` and an
  HTTP(S) clone URL, Git authenticates with the same username and password the
  REST client uses. Previously the REST half authenticated and the Git half did
  not, so a run configured with nothing but an HTTP password discovered changes
  and then failed to fetch any patch set. The credential reaches Git through a
  helper that names the environment variables rather than carrying their values,
  so it stays out of the process list and off disk. SSH remotes and the `bearer`
  and `netrc` modes are untouched.

## 0.2.0

Replaces the raw-capture tool with a `.rv` generator: one file per resolved
review comment thread, holding the code the reviewer saw, the discussion, and
the code that answered it.

- **New `gerrit_rv.py`** with `init-config`, `check-config` and `export`.
  `gerrit_export.py` and its JSONL/normalise/verify pipeline are removed.
- **One JSON config file** for filters (projects, reviewers, branches, status,
  recency, path excludes), limits, parallel workers and enrichment. An unknown
  key is an error, not a silently ignored typo.
- **Limits stop the job**: `max_changes`, `max_comments`, `max_files`. Whatever
  was written stays complete, and the run says which limit stopped it.
- **Resolution matching** walks forward from the comment's patch set to the first
  later one that actually changed the file, rather than assuming the next patch
  set or the last. Threads answered only in words are reported as such.
- **Line tracing through diff hunks**, so the after section shows the region that
  replaced the commented lines even when the file moved.
- **Repeatable real-Gerrit suite.** The harness authenticates as `admin` with the
  development password rather than minting a token per run, which used to exhaust
  Gerrit's ten-token-per-account cap after ten runs.
- Handles file-level comments, commit-message comments, `/PATCHSET_LEVEL`,
  deleted and added files, binary files, stale line numbers, replies on much
  later patch sets, orphaned reply roots and reply cycles.

## 0.1.0

Raw capture of Gerrit review history with reproducible tests.
