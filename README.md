# Code Healer

Automated bug fixing from GitHub issues using the [Snowflake Managed Agents API](https://docs.snowflake.com/en/user-guide/snowflake-cortex/cortex-agents-coding-agent).

Point it at a GitHub issue. It calls the Managed Agents API (`code_toolset_all`), which runs a coding agent server-side in Snowflake's managed sandbox. The agent investigates, fixes the bug, runs the tests, and submits a PR.

No CLI install. No local containers. Just a REST call.

## How it works

```
python heal.py --issue https://github.com/org/repo/issues/123
```

1. Fetches the issue title and body via `gh api`
2. Clones the repo and creates a `fix/issue-{N}` branch
3. Uploads the repo to a Snowflake workspace
4. Calls `POST /api/v2/cortex/agent:run` with `code_toolset_all` — Snowflake runs the agent server-side
5. Agent investigates, fixes, and runs tests in the managed sandbox
6. Downloads the fixed files, commits, pushes, creates a PR

## Usage

```bash
# Fix a bug and create a PR
python heal.py --issue https://github.com/org/repo/issues/42

# Dry-run (fix but don't push)
python heal.py --issue https://github.com/org/repo/issues/42 --dry-run

# Specify connection and workspace
python heal.py --issue https://github.com/org/repo/issues/42 \
  --connection myconn \
  --workspace 'DB.SCHEMA.MY_WORKSPACE'
```

## GitHub Action

Turn any repo into a self-healing codebase. Label an issue with `heal` and the action fixes it.

### Setup

Add this workflow to your target repo at `.github/workflows/heal.yml`:

```yaml
name: Code Healer

on:
  issues:
    types: [labeled]

jobs:
  heal:
    if: github.event.label.name == 'heal'
    runs-on: ubuntu-latest
    permissions:
      contents: write
      issues: write
      pull-requests: write
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - uses: jamescha-earley/code-healer@main
        with:
          issue-number: ${{ github.event.issue.number }}
          snowflake-config: ${{ secrets.SNOWFLAKE_CONNECTIONS_TOML }}
```

Add `SNOWFLAKE_CONNECTIONS_TOML` as a repo secret containing your `connections.toml` content.

### Action inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `issue-number` | Yes | — | GitHub issue number to fix |
| `snowflake-connection` | No | `default` | Connection name from connections.toml |
| `snowflake-config` | No | `""` | Full connections.toml contents (as secret) |
| `workspace` | No | auto | Workspace FQN for the agent sandbox |
| `dry-run` | No | `false` | Fix code but skip PR creation |

### Action outputs

| Output | Description |
|--------|-------------|
| `pr-url` | URL of the created pull request |
| `status` | `success` or `no-changes` |
| `confidence` | Agent confidence: `high`, `medium`, or `low` |

## What's under the hood

The key API call:

```json
POST /api/v2/cortex/agent:run
{
  "models": {"orchestration": "claude-sonnet-4-5"},
  "tools": [{"tool_spec": {"type": "code_toolset_all", "name": "code_toolset_all"}}],
  "tool_resources": {
    "code_toolset_all": {
      "permission_policy": {"type": "always_allow"},
      "workspace_mounts": [{"name": "USER$YOU.PUBLIC.DEFAULT$", "type": "workspace", "mount_path": "/workspace"}]
    }
  }
}
```

`code_toolset_all` gives the agent the full CoCo sandbox: bash, file read/write/edit, grep, glob, web search, SQL execution, and skills. Snowflake manages the runtime — you don't host anything.

## Requirements

- Python 3.12+
- `snowflake-connector-python`, `requests`
- [GitHub CLI](https://cli.github.com/) (`gh`)
- Snowflake account with Managed Agents API access (Public Preview)

## Project structure

```
heal.py        Pipeline: fetch issue → upload → call API → download → PR
action.yml     GitHub Action definition
workflow.yml   Example workflow for target repos
```
