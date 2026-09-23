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

## Shared configuration and credentials

Keep configuration outside Git, for example `~/.config/gerrit-agent/env`:

```bash
export GERRIT_URL='https://gerrit.company.example'
export GERRIT_USER='your-username'
export GERRIT_AUTH='basic'
export GERRIT_CA_FILE='/absolute/path/company-ca.pem'
```

Remove the CA variable if using normal public/system trust. Gerrit must be
reachable from the agent host/container, even if disconnected from the public
internet. Paths must exist **inside the agent's execution environment**.

Both scripts accept the password through `GERRIT_HTTP_PASSWORD`, a secret file
(`--credential-file /absolute/path/http-password` before the subcommand), or
`--ask-credential`. HTTP passwords cannot reveal a username, so Basic auth
requires both. Obtain the HTTP credential from your Gerrit account settings
or administrator; browser SSO passwords are not necessarily HTTP credentials.

Prefer your runtime's secret manager to inject the password. For an interactive
shell, load nonsecret configuration and prompt without writing the password
into shell history:

```bash
source "$HOME/.config/gerrit-agent/env"
read -r -s -p 'Gerrit HTTP password: ' GERRIT_HTTP_PASSWORD
printf '\n'
export GERRIT_HTTP_PASSWORD
# Launch your agent from this shell so its command processes inherit variables.
```

For unattended operation, mount a password-only file with permissions 600 and
configure the agent to pass `--credential-file` on every helper invocation.
Use permissions 700 on its parent directory. Do not put secrets inside a skill,
AGENTS.md, repository, task directory, Git URL, prompt, or committed `.env` file.
Never use `env`, `printenv`, or shell tracing to debug secrets. A file is not
automatically loaded merely because it exists: source your trusted env file
before starting the agent, or configure the runtime environment explicitly.
The helpers do not automatically read `.env` files.

Check access after installation:

```bash
python3 "$HOME/.agents/skills/gerrit-review/gerrit_review.py" doctor
python3 "$HOME/.agents/skills/gerrit-implement/gerrit_implement.py" doctor
```

With password files, add `--credential-file ...` before `doctor`. All global
options go before the subcommand. Both skills share the same configuration;
implementation project/branch/workspace/task paths are explicit command inputs,
and review uses a change number/Change-Id/link. The skills contain full examples.

Example prompts:

- `Use $gerrit-review to review change 123 using the project context and post the review.`
- `Use $gerrit-implement for project team/service, branch master. Implement the following acceptance criteria, test them, and upload the Gerrit change: ...`
