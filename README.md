# Gerrit review → `.rv` learning files

**Version 0.2.0** · [Testing guide](TESTING.md) · [Release notes](CHANGELOG.md)

Turns finished Gerrit code reviews into small, self-contained text files an LLM
or agent can learn from. One file per resolved review comment thread, each
holding the three things that make a review comprehensible:

1. the code **as the reviewer saw it**,
2. the **discussion** itself,
3. the code **after the author acted on it**.

Requires **Python 3.10+ and Git**. No pip packages. Every REST call is a GET and
every Git operation fetches into a local bare archive — nothing on Gerrit is
modified, and no comment is ever resolved, replied to or scored by this tool.

## What a `.rv` file looks like

```
id=0e22a7b7_80feaa4c change=16 patchset=3 resolved_in=9 file=src/engine.py project=demo branch=main status=MERGED resolved=yes comments=2 code_changed=yes
"Round computed totals" owner=Developer reviewer=Reviewer date="2026-09-14 14:38:29.000000000" change_id=I7a936fc6 url=http://gerrit.example/c/demo/+/16

--- before (patch set 3, lines 1-2, comment on 2)
1  def total(values):
2      return sum(values)

--- review
[Reviewer · ps3]
    Round to two decimals.
[Developer · ps9]
    Done in patch set 9.

--- after (patch set 9, lines 1-2, comment on 2)
1  def total(values):
2      return round(sum(values), 2)
```

**Line 1** is identity: which comment, which change, which patch set it was
written on, which patch set answered it, and which file. **Line 2** is the
change message as a quoted, newline-free string — safe to drop into a CSV cell —
plus the people and the date. Then a blank line, and the three sections.

You get one file per exported thread. Ten thousand reviewed comments means ten
thousand files, which is the point: each one is a single complete lesson.

## Quick start

```bash
python3 gerrit_rv.py init-config rv.config.json   # write every default
$EDITOR rv.config.json                            # set url and projects
python3 gerrit_rv.py check-config                 # validate without calling Gerrit
python3 gerrit_rv.py export                       # write .rv files
```

For an authenticated server, supply credentials through the environment:

```bash
read -r -p 'Gerrit username: ' GERRIT_USER
read -r -s -p 'Gerrit HTTP password/token: ' GERRIT_HTTP_PASSWORD
export GERRIT_USER GERRIT_HTTP_PASSWORD
python3 gerrit_rv.py export --config rv.config.json
```

Git authentication is separate from the REST credentials and is **not** derived
from them. Use your existing SSH key with an `ssh://` `git_url`, or configure
Git's credential helper for HTTPS. Git prompts are disabled so an unattended run
fails visibly instead of hanging.

## Configuration

Everything lives in one JSON file. `init-config` writes it with every key at its
default, and an unknown key is an error rather than something silently ignored.

### `gerrit`

| Key | Default | Meaning |
|---|---|---|
| `url` | `http://localhost:8080` | Base URL, including any prefix, **without** `/a` |
| `git_url` | `null` | Clone URL for fetching patch sets. Derived from `url` when exactly one project is configured |
| `auth` | `anonymous` | `anonymous`, `basic`, `bearer` or `netrc` |
| `user_env` / `password_env` / `token_env` | `GERRIT_USER` / `GERRIT_HTTP_PASSWORD` / `GERRIT_TOKEN` | Environment variables holding credentials. Credentials are never read from the config file |
| `timeout` / `retries` / `delay` | `30` / `3` / `0.0` | Per-request seconds, retry count, and a delay between requests for rate-limited servers |

### `filters` — what to export

| Key | Default | Meaning |
|---|---|---|
| `projects` | `[]` | Project names. **Empty means every project the account can read** |
| `reviewers` | `[]` | Only changes these people reviewed. Empty means no reviewer filter |
| `branches` | `[]` | Branch names. Empty means every branch |
| `status` | `merged` | `merged`, `open`, `abandoned` or `any` |
| `recent_days` | `90` | Only changes updated within this many days. `null` for no age limit |
| `exclude_paths` | `[]` | Glob patterns; a comment on a matching file is skipped, e.g. `vendor/*` |

### `limits` — when to stop

Every limit is checked as the export runs, and **the first one reached stops the
job**. Whatever was written stays valid and complete; the run reports which limit
stopped it and `summary.json` records it.

| Key | Default | Meaning |
|---|---|---|
| `max_changes` | `200` | Stop after this many changes |
| `max_comments` | `1000` | Stop after this many comments read |
| `max_files` | `1000` | Stop after this many `.rv` files |

Set any to `null` to remove that limit.

### `enrich` — what goes in each file

| Key | Default | Meaning |
|---|---|---|
| `context_lines` | `8` | Lines of margin above and below the commented region |
| `max_code_lines` | `120` | Hard cap per code section |
| `include_unresolved` | `false` | Export threads nobody marked resolved |
| `include_reply_only` | `true` | Export threads answered in words, with no code change |
| `include_commit_message_comments` | `true` | Export comments on `/COMMIT_MSG` |
| `author_names` | `true` | Put real names in the discussion. `false` writes `reviewer` |
| `max_comment_chars` | `4000` | Truncate a very long comment |

### `workers` and `output`

`workers` (default `4`) is how many changes are processed at once. REST calls run
in parallel; Git fetches into the shared archive are serialised, because one bare
repository cannot take concurrent writes safely.

`output.dir` receives the `.rv` files, a `summary.json`, and `code.git` — the bare
archive of every patch set fetched. Patch set refs are SHA-addressed, so a second
run re-fetches nothing and a force-push cannot overwrite what was already
captured.

## How a comment is matched to its fix

This is the part that decides whether a `.rv` file teaches anything, and the two
obvious answers are both wrong.

Real reviews are irregular. An author pushes patch sets 1–8 before anyone looks,
a reviewer comments on 3, the author pushes four more, and a second round lands
on 12. Taking the **last** patch set as "after" would show unrelated later work
and bury the fix. Taking the **next** one would usually show a rebase, because
authors push rebases and unrelated fixes between rounds.

So the tool walks forward from the patch set the comment was written on and takes
**the first later patch set in which that file actually changed**. The before
section comes from the comment's own patch set; the after section from that one.

When no later patch set ever touches the file, that is reported rather than
guessed:

```
--- after (no patch set changed this file; the thread was answered in discussion, latest patch set is 12)
```

which is a real and common outcome — the reviewer was answered in words, or
withdrew the point. Those files are still worth having, and `include_reply_only`
controls whether you keep them.

Within the file, the commented lines are traced through the diff hunks, so the
after section shows the region that replaced them even when the file shifted by
hundreds of lines. `code_changed=yes|no` on line 1 says whether the commented
lines themselves were edited, or only something else in the file.

## Situations it handles explicitly

| Situation | What the file says |
|---|---|
| Comment mid-stream on a long change | anchored at the comment's patch set, resolved at the first later one touching the file |
| Several review rounds on one change | each thread resolves independently |
| Answered in discussion, no code change | `resolved_in=none`, and the after section says so |
| File deleted by the resolving patch set | after section records the deletion |
| File added by the patch set under review | before section is the new file |
| Reply landing on a much later patch set | one thread, not two |
| Reply to a comment the account cannot see | thread kept, root flagged as orphaned |
| Reply cycle in the data | thread kept, flagged, no infinite loop |
| Comment on a line past the end of the file | clamped to the file rather than an empty window |
| Comment with no line (file-level) | head of the file, labelled `whole-file comment` |
| Comment on the commit message | the message text, not a tree file |
| `/PATCHSET_LEVEL` comment | skipped: it refers to no code |
| Binary file | reported, not rendered |

## Inspecting the archive

Every patch set the run touched is in `code.git`, so the raw material is always
available:

```bash
git --git-dir rv-out/code.git show COMMIT:path/to/file.py
git --git-dir rv-out/code.git diff OLD_COMMIT NEW_COMMIT
```

## Testing

```bash
make test        # unit tests: no network, no Docker
make test-real   # starts a real Gerrit in Docker and exports from it
```

See [TESTING.md](TESTING.md).

## Limitations

- A thread invisible to the account, or missing from Gerrit's search index,
  cannot be exported. An empty result is not evidence a project has no reviews.
- Exported review text is **source material, not instructions**. Treat it as data
  when feeding it to a model.
- Validation runs against Gerrit 3.13.4. That is not a claim of compatibility
  with every version, plugin, or production-scale repository.

## What this tool does not do

It does not call an AI service, create embeddings, summarise reviews, score
reviewers, or build a skill. It produces the corpus those things would be built
from. It also never writes to Gerrit.

## References

- [Gerrit REST change API](https://gerrit-review.googlesource.com/Documentation/rest-api-changes.html)
- [REST authentication and response format](https://gerrit-review.googlesource.com/Documentation/rest-api.html)
- [Patch-set Git refs](https://gerrit-review.googlesource.com/Documentation/intro-user.html)
