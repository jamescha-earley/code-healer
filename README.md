# Code Healer

Automated bug fixing from GitHub issues using the [Cortex Code Agent SDK](https://docs.snowflake.com/en/developer-guide/cortex-code/cortex-code-agent-sdk).

Point it at a GitHub issue. It investigates the codebase, fixes the bug, runs the tests, and submits a PR -- in seconds.

## How it works

```
python heal.py --issue https://github.com/org/repo/issues/123
```

1. Fetches the issue title, body, and labels via `gh api`
2. Clones the repo and creates a `fix/issue-{N}` branch
3. Launches a Cortex Code agent with a team of three specialists:
   - **Investigator** (Read, Glob, Grep) -- finds the root cause
   - **Fixer** (Edit, Write) -- applies the minimal code change
   - **Reviewer** (Read, Bash) -- validates the fix via `git diff` and tests
4. Produces a structured JSON report (PR title, body, root cause, confidence)
5. Commits, pushes, and creates a PR via `gh pr create`

## Usage

```bash
# Fix a bug and create a PR
python heal.py --issue https://github.com/org/repo/issues/42

# Preview the fix without pushing
python heal.py --issue https://github.com/org/repo/issues/42 --dry-run

# JSON-only output (for piping)
python heal.py --issue https://github.com/org/repo/issues/42 --json

# Use an existing local checkout
python heal.py --issue https://github.com/org/repo/issues/42 --repo-dir ./my-repo

# CI mode (writes GitHub Actions outputs)
python heal.py --issue https://github.com/org/repo/issues/42 --ci
```

## GitHub Action

Turn any repo into a self-healing codebase. When someone adds the `heal` label to an issue, the action automatically investigates, fixes, and opens a PR.

### Setup

**1. Create the workflow file** in your target repo:

```bash
mkdir -p .github/workflows
cp workflow.yml .github/workflows/heal.yml
```

Or copy the contents of `workflow.yml` manually. The key parts:

```yaml
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
        id: healer
        with:
          issue-number: ${{ github.event.issue.number }}
```

**2. Create the `heal` label** in your repo (Settings > Labels > New label).

**3. That's it.** Label any issue with `heal` and the action will:
- Clone the repo
- Install the Cortex Code CLI and Agent SDK
- Run the three-agent team (investigate -> fix -> review)
- Push a fix branch and create a PR
- Comment on the issue with the result and confidence level
- Remove the `heal` label to prevent re-triggers

### Action inputs

| Input | Required | Default | Description |
|-------|----------|---------|-------------|
| `issue-number` | Yes | -- | GitHub issue number to fix |
| `snowflake-connection` | No | `""` | Snowflake connection (for SQL-related bugs) |
| `cortex-version` | No | `beta` | Cortex Code CLI version channel |
| `dry-run` | No | `false` | Fix code but skip PR creation |

### Action outputs

| Output | Description |
|--------|-------------|
| `pr-url` | URL of the created pull request |
| `status` | `success`, `no-changes`, or `failed` |
| `confidence` | Agent's confidence: `high`, `medium`, or `low` |

### What happens on failure

If the agent can't fix the bug, it comments on the issue explaining that a human needs to investigate, with a link to the workflow run logs.

## Project structure

```
heal.py        Main pipeline: fetch issue, clone, fix, submit PR
agents.py      Three subagent definitions (investigator, fixer, reviewer)
prompts.py     Orchestrator system prompt built from issue details
schemas.py     JSON Schema for structured PR output
action.yml     GitHub Action definition
workflow.yml   Example workflow for target repos
```

## Requirements

- Python 3.12+
- [Cortex Code CLI](https://docs.snowflake.com/en/developer-guide/cortex-code/cortex-code-overview)
- [Cortex Code Agent SDK](https://docs.snowflake.com/en/developer-guide/cortex-code/cortex-code-agent-sdk) (`pip install cortex-code-agent-sdk`)
- [GitHub CLI](https://cli.github.com/) (`gh`)

## Example

Given [this issue](https://github.com/jamescha-earley/heal-test-repo/issues/1) reporting an off-by-one bug in `get_low_stock()`, the agent:

- Found the root cause: `<` instead of `<=` on line 30 of `inventory.py`
- Applied the one-character fix
- Ran all 5 tests -- all passing
- Created [this PR](https://github.com/jamescha-earley/heal-test-repo/pull/3) with full root cause analysis

Total time: **7.8 seconds**. Confidence: **high**.
