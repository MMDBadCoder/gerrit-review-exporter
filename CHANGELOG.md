# Release notes

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
