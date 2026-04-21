#!/usr/bin/env python3
"""Code Healer -- automated bug fixing from GitHub issues.

Takes a GitHub issue URL, analyzes the codebase, fixes the bug, and
submits a PR with a detailed description.

Usage:
    python heal.py --issue https://github.com/org/repo/issues/123
    python heal.py --issue https://github.com/org/repo/issues/123 --json
    python heal.py --issue https://github.com/org/repo/issues/123 --dry-run
"""

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from cortex_code_agent_sdk import (
    AssistantMessage,
    CortexCodeAgentOptions,
    HookMatcher,
    PermissionResultAllow,
    ResultMessage,
    SystemMessage,
    TaskProgressMessage,
    query,
)

from agents import get_agents
from prompts import get_system_prompt
from schemas import PR_REPORT_SCHEMA

# Audit: every file edit the agent makes
edit_log: list[dict] = []


def gha_output(name: str, value: str) -> None:
    """Write a GitHub Actions output variable."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{name}={value}\n")


def parse_issue_url(url: str) -> tuple[str, str, int]:
    """Parse a GitHub issue URL into (owner, repo, number)."""
    match = re.match(
        r"https?://github\.com/([^/]+)/([^/]+)/issues/(\d+)", url
    )
    if not match:
        print(f"Invalid GitHub issue URL: {url}", file=sys.stderr)
        sys.exit(1)
    return match.group(1), match.group(2), int(match.group(3))


def fetch_issue(owner: str, repo: str, number: int) -> dict:
    """Fetch issue details via gh CLI."""
    result = subprocess.run(
        [
            "gh", "api",
            f"repos/{owner}/{repo}/issues/{number}",
            "--jq", '{title: .title, body: .body, labels: [.labels[].name], state: .state}',
        ],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def prepare_repo(owner: str, repo: str, issue_number: int) -> Path:
    """Clone the repo and create a fix branch. Returns the repo path."""
    repo_dir = Path(tempfile.mkdtemp(prefix=f"heal-{repo}-{issue_number}-"))
    print(f"  Cloning {owner}/{repo} into {repo_dir}...")

    subprocess.run(
        ["gh", "repo", "clone", f"{owner}/{repo}", str(repo_dir), "--", "--depth=50"],
        check=True, capture_output=True, text=True,
    )

    branch = f"fix/issue-{issue_number}"
    subprocess.run(
        ["git", "checkout", "-b", branch],
        cwd=repo_dir, check=True, capture_output=True, text=True,
    )
    print(f"  Branch: {branch}")
    return repo_dir


async def auto_approve(tool_name, tool_input, context):
    """Auto-approve all tool calls."""
    return PermissionResultAllow(behavior="allow")


async def edit_audit_hook(hook_input, tool_use_id, context):
    """PostToolUse hook that logs file edits."""
    if isinstance(hook_input, dict):
        tool_name = hook_input.get("tool_name", "")
        tool_input = hook_input.get("tool_input", {})
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path") or tool_input.get("path")
            if file_path:
                edit_log.append({
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "tool": tool_name,
                    "file": file_path,
                    "tool_use_id": tool_use_id,
                })
    return {}


async def run_healer(
    issue: dict,
    issue_number: int,
    repo_dir: Path,
    connection: str | None,
    json_only: bool,
) -> dict | None:
    """Run the healer agent and return the structured PR report."""
    labels = issue.get("labels", [])
    system_prompt = get_system_prompt(
        issue_title=issue["title"],
        issue_body=issue.get("body") or "(no description)",
        issue_number=issue_number,
        labels=labels,
    )

    prompt = (
        f"Fix the bug described in issue #{issue_number}: {issue['title']}\n\n"
        f"The repository is cloned at {repo_dir}. "
        f"Use your team to investigate, fix, and review the changes."
    )

    options = CortexCodeAgentOptions(
        cwd=str(repo_dir),
        system_prompt=system_prompt,
        output_format=PR_REPORT_SCHEMA,
        max_turns=40,
        agents=get_agents(),
        can_use_tool=auto_approve,
        hooks={
            "PostToolUse": [
                HookMatcher(
                    matcher="Edit|Write|MultiEdit",
                    hooks=[edit_audit_hook],
                )
            ],
        },
    )
    if connection:
        options.connection = connection

    report = None

    if not json_only:
        print(f"\n{'='*60}")
        print(f"  CODE HEALER: #{issue_number} -- {issue['title']}")
        print(f"  Repo: {repo_dir}")
        print(f"{'='*60}\n")

    async for message in query(prompt=prompt, options=options):
        if isinstance(message, AssistantMessage):
            if not json_only:
                for block in message.content:
                    if hasattr(block, "text"):
                        print(block.text, end="")
                    elif hasattr(block, "name"):
                        print(f"\n  [tool] {block.name}", end="")

        elif isinstance(message, TaskProgressMessage):
            if not json_only:
                desc = getattr(message, "description", "")
                if desc:
                    print(f"\n  [agent] {desc}", end="")

        elif isinstance(message, ResultMessage):
            if not json_only:
                print(f"\n\n{'='*60}")
                print(f"  Healing complete.")
                duration = getattr(message, "duration_ms", None)
                if duration:
                    print(f"  Duration: {duration / 1000:.1f}s")
                print(f"  Files edited: {len(edit_log)}")
                print(f"{'='*60}")

            if message.structured_output:
                report = message.structured_output
            break

        elif isinstance(message, SystemMessage):
            if not json_only:
                subtype = getattr(message, "subtype", "")
                if subtype and subtype not in ("init",):
                    print(f"\n  [system] {subtype}", end="")

    return report


def submit_pr(
    repo_dir: Path,
    owner: str,
    repo: str,
    issue_number: int,
    report: dict,
    dry_run: bool,
) -> str | None:
    """Commit changes and create the PR. Returns PR URL or None."""
    # Check if there are actual changes
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if not status.stdout.strip():
        print("  No file changes detected -- nothing to commit.", file=sys.stderr)
        return None

    # Ensure git identity is configured (needed in CI)
    for key, val in [("user.name", "Code Healer"), ("user.email", "code-healer[bot]@users.noreply.github.com")]:
        check = subprocess.run(["git", "config", key], cwd=repo_dir, capture_output=True, text=True)
        if not check.stdout.strip():
            subprocess.run(["git", "config", key, val], cwd=repo_dir, check=True, capture_output=True)

    # Stage and commit
    subprocess.run(
        ["git", "add", "-A"],
        cwd=repo_dir, check=True, capture_output=True,
    )

    commit_msg = f"fix: {report.get('pr_title', f'Fix issue #{issue_number}')}\n\nCloses #{issue_number}"
    subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=repo_dir, check=True, capture_output=True, text=True,
    )

    if dry_run:
        print("\n  [dry-run] Skipping push and PR creation.")
        diff = subprocess.run(
            ["git", "diff", "HEAD~1"],
            cwd=repo_dir, capture_output=True, text=True,
        )
        print(diff.stdout[:3000])
        return None

    # Push and create PR
    branch = f"fix/issue-{issue_number}"
    subprocess.run(
        ["git", "push", "-u", "origin", branch],
        cwd=repo_dir, check=True, capture_output=True, text=True,
    )

    pr_body = report.get("pr_body", f"Fixes #{issue_number}")
    pr_title = report.get("pr_title", f"Fix #{issue_number}")

    result = subprocess.run(
        [
            "gh", "pr", "create",
            "--title", pr_title,
            "--body", pr_body,
            "--repo", f"{owner}/{repo}",
        ],
        cwd=repo_dir, capture_output=True, text=True, check=True,
    )
    pr_url = result.stdout.strip()
    return pr_url


def main():
    parser = argparse.ArgumentParser(
        description="Code Healer -- fix bugs from GitHub issues using Cortex Code Agent SDK"
    )
    parser.add_argument(
        "--issue", required=True,
        help="GitHub issue URL (e.g. https://github.com/org/repo/issues/123)",
    )
    parser.add_argument(
        "--connection", default=None,
        help="Snowflake connection name (optional, for SQL-related bugs)",
    )
    parser.add_argument(
        "--json", action="store_true", dest="json_only",
        help="Output only the structured JSON report",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fix the code but don't push or create PR",
    )
    parser.add_argument(
        "--repo-dir", default=None,
        help="Use existing repo directory instead of cloning",
    )
    parser.add_argument(
        "--ci", action="store_true",
        help="CI mode: write GitHub Actions outputs (pr-url, status, confidence)",
    )

    args = parser.parse_args()
    ci = args.ci

    owner, repo, issue_number = parse_issue_url(args.issue)

    # Fetch issue
    if not args.json_only:
        print(f"Fetching issue #{issue_number} from {owner}/{repo}...")
    issue = fetch_issue(owner, repo, issue_number)

    if issue.get("state") == "closed":
        print(f"  Warning: issue #{issue_number} is already closed.", file=sys.stderr)

    if not args.json_only:
        print(f"  Title: {issue['title']}")
        print(f"  Labels: {', '.join(issue.get('labels', [])) or 'none'}")

    # Prepare repo
    if args.repo_dir:
        repo_dir = Path(args.repo_dir)
        if not repo_dir.exists():
            print(f"Repo dir does not exist: {repo_dir}", file=sys.stderr)
            sys.exit(1)
    else:
        repo_dir = prepare_repo(owner, repo, issue_number)

    # Run agent
    report = asyncio.run(
        run_healer(issue, issue_number, repo_dir, args.connection, args.json_only)
    )

    if not report:
        print("\nAgent did not produce a structured report.", file=sys.stderr)
        if ci:
            gha_output("status", "failed")
            gha_output("pr-url", "")
            gha_output("confidence", "")
        sys.exit(1)

    if args.json_only:
        print(json.dumps(report, indent=2))
    else:
        print("\n\nSTRUCTURED REPORT:")
        print(json.dumps(report, indent=2))

    # Submit PR
    if not args.json_only:
        print("\nSubmitting PR...")
    pr_url = submit_pr(repo_dir, owner, repo, issue_number, report, args.dry_run)

    confidence = report.get("confidence", "unknown")

    if pr_url:
        print(f"\n  PR created: {pr_url}")
        if ci:
            gha_output("status", "success")
            gha_output("pr-url", pr_url)
            gha_output("confidence", confidence)
    elif args.dry_run:
        if ci:
            gha_output("status", "success")
            gha_output("pr-url", "dry-run")
            gha_output("confidence", confidence)
    else:
        print("\n  No PR created (no changes or error).", file=sys.stderr)
        if ci:
            gha_output("status", "no-changes")
            gha_output("pr-url", "")
            gha_output("confidence", confidence)


if __name__ == "__main__":
    main()
