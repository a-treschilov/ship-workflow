# Installation

The package installs as a pinned snapshot. There is no package manager, workspace link, or runtime dependency
on the source repository.

## Before you install

- Python 3.11 or newer on `PATH` as `python3`.
- `git`, `bash`, and standard Unix tools (`cp`, `mv`, `find`).
- An `.agents/` directory in the project root. Create it if needed:

  ```bash
  cd /path/to/project
  mkdir -p .agents
  ```

The installer refuses to run in a directory without `.agents`. That keeps a mistyped `cd` from writing a
workflow tree into an unrelated directory.

## Install a tag

Use the remote form when you want the installed bytes to come from a specific tag:

```bash
git clone https://github.com/<owner>/ship-workflow.git ~/ship-workflow
cd /path/to/project
SHIP_REMOTE=https://github.com/<owner>/ship-workflow.git \
  ~/ship-workflow/install/install.sh v1.1.0 --host both
```

The installer fetches `SHIP_REMOTE` at `<ref>` into a temporary directory and copies that snapshot. A private
remote uses the same environment variable with an SSH or HTTPS URL your `git` already authenticates.

Use the local form when you have a checkout containing the tag:

```bash
cd /path/to/project
SHIP_SRC=~/ship-workflow ~/ship-workflow/install/install.sh v1.1.0 --host both
```

`SHIP_SRC` takes precedence over `SHIP_REMOTE`. An unresolved ref is an error. A local install copies committed
bytes at that ref even if another revision is checked out. Pass `--dev` with the currently checked-out ref to
copy the working tree, including uncommitted changes; the version record marks it as a worktree snapshot.

With neither variable set, the source is the checkout containing `install/install.sh`.

Select a host with `--host claude`, `--host codex`, or `--host both`. When exactly one host executable is
found it is selected automatically; otherwise an interactive install asks, and a non-interactive install
requires `--host`. Upgrades retain the host in `.ship-version` unless `--host` explicitly changes it. Use `--models ocx`
for the OCX example assignments on a new install; the default is `inherit`. Existing model configuration is
preserved on upgrade.
The legacy `--sync` flag is accepted for compatibility; sync and check run on every install.

The installer creates `.agents/.ship-version`:

```json
{
  "schema_version": 1,
  "ref": "v1.1.0",
  "commit": "<resolved commit or unknown>",
  "host": "both",
  "installed_at": "<UTC timestamp>"
}
```

## What the installer writes

Shared files are replaced on every install:

```text
.agents/workflows/ship-fix/
.agents/skills/ship/
.agents/skills/fix/
.agents/skills/ship-analyze/
.agents/roles/
.agents/platforms/claude/roles.json
.agents/platforms/codex/roles.json
```

The model configuration is created only when absent. Host settings are merged with the required workflow
hooks and permissions; unrelated settings are retained. Conflicting worktree hooks stop installation:

```text
.agents/agent-models.yaml
.claude/settings.json
.codex/hooks.json
```

Host links for the selected host are created when absent. Unrelated existing paths cause installation to stop;
the known old broken protocol link is repaired. The installer also refuses symlinked configuration and agent
directories or target files, to avoid modifying files outside the project:

```text
.claude/scripts  -> ../.agents/workflows/ship-fix/scripts
.claude/hooks    -> ../.agents/workflows/ship-fix/hooks
.claude/skills/<skill> -> ../../.agents/skills/<skill>
.claude/references/ship-protocol.md -> ../../.agents/workflows/ship-fix/PROTOCOL.md
.codex/scripts   -> ../.agents/workflows/ship-fix/scripts
.codex/hooks     -> ../.agents/workflows/ship-fix/hooks
```

An existing installation without `.ship-version` is moved to `.agents/.ship-backup/<timestamp>/` before the
shared files are replaced. The installer then runs `agent_models.py sync` and `agent_models.py check`; a failure
returns a nonzero exit code and restores the managed files from a temporary backup.

## Configure models and permissions

`.agents/agent-models.yaml` maps the four shared roles to model IDs and contains the catalog used by the
checker. The template uses `inherit`, which uses the host's model configuration. To pin OCX models, replace
the role values with IDs your provider exposes.
Existing configurations with a single common prefix on all four role names are accepted without changing
their model assignments. Ambiguous or incomplete mappings are rejected.
Optional `claude_models` and `codex_models` mappings override individual roles for that host. A Claude override
accepts a host-native model ID or alias; Codex accepts the ID supported by its configured provider. Omitted
roles use the shared `models` assignment.
`claude_aliases` is the canonical-ID to Claude Code alias map. Every role model must appear in
`available_models`, and every model must have an alias when generating Claude role files.

After editing the file:

```bash
cd /path/to/project
python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py sync
python3 -I -B .agents/workflows/ship-fix/scripts/agent_models.py check
```

Restart or reload the host after `sync`. The workflow checks for drift before dispatch and stops rather than
changing a model in an already loaded session.
Both `sync` and `check` accept `--host claude`, `--host codex`, or `--host both`; by default they use the
installed host from `.ship-version`, or both hosts for a legacy install.
Codex-only checks do not require Claude aliases.

`ocx_models.py` refreshes the catalog from an OCX gateway. It is optional and does not assign models to roles.

`.claude/settings.json` registers the trusted-command and worktree hooks and allows the canonical helper path.
`.codex/hooks.json` registers the Codex pre-tool-use hook. Keep those files project-local; do not copy secrets or
machine-specific paths into the shared package.

## Upgrade

```bash
make -C ~/ship-workflow install REF=v1.2.0 HOST=both PROJECT=/path/to/project
```

The Make target invokes the same installer, so the tag verification and backup rules still apply. The shared
workflow, skills, roles and platform metadata are replaced; per-project configuration is preserved.

Use `make sync` after changing a role or model assignment, and `make check` to verify generated native files
without writing:

```bash
make -C ~/ship-workflow sync  PROJECT=/path/to/project
make -C ~/ship-workflow check PROJECT=/path/to/project
```

## Uninstall

```bash
make -C ~/ship-workflow uninstall PROJECT=/path/to/project
```

Uninstall removes the shared `ship-fix` tree, installed workflow skills, matching managed role files, generated agents and known
host links. It removes hook and permission entries recorded as added by the installer, preserving pre-existing
entries and `.agents/agent-models.yaml`. If an unowned hook still points to the workflow, including hooks from
legacy records without ownership metadata, uninstall stops before changes; remove that registration first.

## Troubleshooting

**`Not a project root (missing .agents)`** — run the installer from the directory that should own `.agents`, or
create `.agents` first. Do not run it from your home directory.

**`SHIP_SRC is at ..., but <ref> resolves to ...`** — check out the requested tag in the source checkout, or
unset `SHIP_SRC` and use `SHIP_REMOTE` so the installer clones the tag itself.

**`ship-workflow hook not found`** — the host started outside a project that has
`.agents/workflows/ship-fix/hooks/codex_pre_tool_use.py`. Open the project root, or reinstall so the hook exists.

**`Agent adapter drift` or `model_not_found`** — run `agent_models.py check`, fix the model catalog, run `sync`,
then restart the host. A stale model pin is usually a catalog or gateway issue, not a workflow failure.

**A command is not auto-approved** — extend `workflows/ship-fix/hooks/trusted_policy.py` for your repository
layout. Unknown commands fall through to the host's permission prompt by design; the hook is an allowlist, not
a sandbox.
