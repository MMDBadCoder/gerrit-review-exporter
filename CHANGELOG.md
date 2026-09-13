# Changelog

## 0.1.0 — 2026-09-13

Initial release for capturing a Gerrit project's review history and reviewed code.

- Export published inline/file comments, general messages, reviewers, revisions,
  and API-exposed metadata across merged, abandoned, and open changes.
- Retain raw responses and historical snapshots; produce linked JSONL datasets.
- Fetch patch-set commits into a bare Git archive and generate parent diffs.
- Resume interrupted exports, update changed reviews, and report incomplete runs.
- Preserve cross-patch-set threads with parent-before-reply ordering, even when
  Gerrit gives a parent and reply identical timestamps.
- Verify raw/normalized checksums, patches, refs, and Git object integrity.
- Support HTTP Basic, bearer, netrc, and anonymous API authentication.
- Include a pinned Docker-based Gerrit 3.13.4 setup and reproducible test harness.

Validation: 22 local tests and 20 checks against self-hosted Gerrit 3.13.4.
See [TESTING.md](TESTING.md) and [saved real-server evidence](tests/evidence/gerrit-3.13.4.json).

Scope: data capture only. No embeddings, RAG service, or generated agent skill.
API visibility, deleted/redacted data, external plugin/CI storage, and production
scale remain subject to the limitations described in the README.
