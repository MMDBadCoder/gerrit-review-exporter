# Install the Gerrit agent skills

Each folder is a standalone skill containing exactly two files: `SKILL.md` and
one Python helper. Install the **whole folder**, keeping the helper next to the
skill. Python 3.10+, Git, and a Linux/macOS execution environment are required.

## Codex

From a clone of this repository, install both skills for your user:

```bash
mkdir -p "$HOME/.agents/skills"
cp -R skills/gerrit-review skills/gerrit-implement "$HOME/.agents/skills/"
```

Alternatively, install in your target project's `.agents/skills/` for project
scope. Choose one scope to avoid duplicate copies. Start a new agent session
and explicitly invoke `$gerrit-review` or `$gerrit-implement` in your prompt.
Follow your Codex version's [official skill discovery documentation](https://learn.chatgpt.com/docs/build-skills).

## Other agents

Putting files near an agent does not necessarily load them. Register the two
folders in its supported skill directory/skill configuration. If it has no
native skill loader, add this instruction to its trusted project instructions
(e.g. AGENTS.md) using real absolute paths:

```text
For Gerrit code review, read /path/gerrit-review/SKILL.md and follow its workflow.
For implementing and uploading a task to Gerrit, read /path/gerrit-implement/SKILL.md
and follow its workflow. Locate each Python helper next to its SKILL.md.
```

The agent must have shell execution, filesystem access to project and task
folders, and network access to Gerrit. Loading text alone cannot grant tools,
network permissions, or credentials. Project context and existing project
skills remain separate and should also be available to the agent.

## One config file for both skills

```bash
mkdir -p "$HOME/.config/gerrit-agent"
cp skills/gerrit.config.example.json "$HOME/.config/gerrit-agent/config.json"
```

Edit that file and replace the URL, username, HTTP password, and CA path with
your values. It is normal to store the credential beside these other settings:

```json
{
  "url": "https://gerrit.company.example",
  "username": "your-username",
  "http_password": "your-gerrit-http-password",
  "auth": "basic",
  "ca_file": "company-ca.pem",
  "timeout": 60
}
```

Both skills automatically use this file. No environment variables are necessary.
Put the PEM certificate beside the config, or use an absolute path. Omit
`ca_file` for normal system trust. These paths must exist inside the agent's
execution environment. The URL is Gerrit's installation base, without `/a`.

To keep the config somewhere else (including beside the skill files), pass
`--config /path/gerrit.json` **before the subcommand** on every command, or set
`GERRIT_CONFIG=/path/gerrit.json` in the agent's environment. A config just placed
beside the script is not automatically discovered: select it explicitly.

Explicit CLI connection options override the config; config values override
legacy environment variables. The inline `http_password` overrides the password
environment variable; `--credential-file` overrides it, and `--ask-credential`
overrides both. The optional JSON keys `credential_file` and `credential_env`
also support these alternative sources. Relative file paths in JSON are relative
to the config directory. Unknown keys and malformed JSON fail with an error.

Project, branch, task/workspace paths, and the change to review remain task
inputs. Store your actual config locally; the public example contains only
placeholders. The helpers do not print the password or store it in Git URLs,
review bundles, or task state. Agents need the config's path, not its contents.

Check access after installation:

```bash
python3 "$HOME/.agents/skills/gerrit-review/gerrit_review.py" doctor
python3 "$HOME/.agents/skills/gerrit-implement/gerrit_implement.py" doctor
```

With a custom config path, add `--config /path/gerrit.json` before `doctor`. All global
options go before the subcommand. Both skills share the same configuration;
implementation project/branch/workspace/task paths are explicit command inputs,
and review uses a change number/Change-Id/link. The skills contain full examples.

Example prompts:

- `Use $gerrit-review to review change 123 using the project context and post the review.`
- `Use $gerrit-implement for project team/service, branch master. Implement the following acceptance criteria, test them, and upload the Gerrit change: ...`

## Verify configuration support

From the source repository:

```bash
python3 -m unittest discover -s tests -p test_skill_config.py -v
python3 skills/gerrit-review/gerrit_review.py self-test
# With the local test Gerrit running (see gerrit-implement/SKILL.md):
python3 tests/real_gerrit_implement.py
```

The live test uses one generated config containing synthetic credentials for
both helpers, and verifies HTTPS, relative CA paths, and patch-set uploads.
