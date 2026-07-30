"""Code Healer -- automated bug fixing from GitHub issues.

Calls the Snowflake Managed Agents API (code_toolset_all) to investigate
and fix bugs. The agent runs server-side in Snowflake's managed sandbox --
no CLI install, no local containers. Just a REST call.

Usage:
    python heal.py --issue https://github.com/org/repo/issues/123
    python heal.py --issue https://github.com/org/repo/issues/42 --dry-run
    python heal.py --issue https://github.com/org/repo/issues/42 --json
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import requests
import snowflake.connector


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
    """Clone the repo and create a fix branch."""
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
    return repo_dir


def upload_to_workspace(repo_dir: Path, workspace: str, connection: str) -> None:
    """Upload repo files to a Snowflake workspace for the managed agent."""
    print(f"  Uploading repo to workspace {workspace}:/heal/ ...")
    for f in repo_dir.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            rel = f.relative_to(repo_dir)
            dest = f"{workspace}:/heal/{rel.parent}/" if str(rel.parent) != "." else f"{workspace}:/heal/"
            subprocess.run(
                ["cortex", "ws", "cp", str(f), dest, "-c", connection],
                capture_output=True, text=True,
            )


def download_from_workspace(repo_dir: Path, workspace: str, connection: str) -> None:
    """Download fixed files back from workspace."""
    # List all files in workspace heal dir and download them
    result = subprocess.run(
        ["cortex", "ws", "ls", f"{workspace}:/heal/", "-c", connection, "--json"],
        capture_output=True, text=True,
    )
    if result.returncode == 0:
        try:
            files = json.loads(result.stdout)
            for entry in files.get("items", []):
                name = entry.get("name", "")
                if name:
                    local_path = repo_dir / name.lstrip("/")
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    subprocess.run(
                        ["cortex", "ws", "cp", f"{workspace}:/heal/{name}", str(local_path.parent) + "/", "-c", connection],
                        capture_output=True, text=True,
                    )
        except json.JSONDecodeError:
            pass


def call_managed_agent(
    connection_name: str,
    workspace: str,
    prompt: str,
    json_only: bool = False,
) -> dict | None:
    """Call the Managed Agents API with code_toolset_all."""
    conn = snowflake.connector.connect(connection_name=connection_name)
    token = conn.rest.token
    account_url = f"https://{conn.host}"
    url = f"{account_url}/api/v2/cortex/agent:run"

    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
        "models": {"orchestration": "claude-sonnet-4-5"},
        "instructions": {
            "system": (
                "You are a bug-fixing agent. Investigate the codebase, identify the root cause, "
                "apply the minimal fix, and run tests to verify. Be thorough but concise. "
                "After fixing, provide a JSON summary with keys: pr_title, pr_body, root_cause, confidence."
            )
        },
        "tools": [
            {"tool_spec": {"type": "code_toolset_all", "name": "code_toolset_all"}}
        ],
        "tool_resources": {
            "code_toolset_all": {
                "permission_policy": {"type": "always_allow"},
                "workspace_mounts": [
                    {"name": workspace, "type": "workspace", "mount_path": "/workspace"}
                ]
            }
        }
    }

    headers = {
        "Authorization": f"Snowflake Token=\"{token}\"",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    response = requests.post(url, json=payload, headers=headers, stream=True)

    if response.status_code != 200:
        print(f"Error {response.status_code}: {response.text}", file=sys.stderr)
        return None

    full_text = []
    for line in response.iter_lines(decode_unicode=True):
        if not line or not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            event = json.loads(data)
            text = extract_text(event)
            if text:
                full_text.append(text)
                if not json_only:
                    print(text, end="", flush=True)
        except json.JSONDecodeError:
            continue

    conn.close()

    # Try to extract structured report from agent output
    combined = "".join(full_text)
    report = extract_json_report(combined)
    return report


def extract_text(event: dict) -> str:
    """Extract printable text from an SSE event."""
    if "delta" in event:
        delta = event["delta"]
        if isinstance(delta, dict):
            if "text" in delta:
                return delta["text"]
            inner = delta.get("delta", {})
            if isinstance(inner, dict) and "text" in inner:
                return inner["text"]
    content = event.get("content", [])
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block["text"]
    return ""


def extract_json_report(text: str) -> dict | None:
    """Try to find a JSON object with pr_title in the agent output."""
    # Look for JSON blocks
    for match in re.finditer(r'\{[^{}]*"pr_title"[^{}]*\}', text, re.DOTALL):
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue
    # Try code-fenced JSON
    for match in re.finditer(r'```json?\s*\n(.*?)\n```', text, re.DOTALL):
        try:
            obj = json.loads(match.group(1))
            if "pr_title" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    return None


def submit_pr(
    repo_dir: Path, owner: str, repo: str, issue_number: int,
    report: dict | None, dry_run: bool,
) -> str | None:
    """Commit changes and create a PR."""
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if not status.stdout.strip():
        print("  No file changes detected.", file=sys.stderr)
        return None

    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)

    title = (report or {}).get("pr_title", f"Fix issue #{issue_number}")
    subprocess.run(
        ["git", "commit", "-m", f"fix: {title}\n\nCloses #{issue_number}"],
        cwd=repo_dir, check=True, capture_output=True, text=True,
    )

    if dry_run:
        print("\n  [dry-run] Skipping push and PR creation.")
        return None

    branch = f"fix/issue-{issue_number}"
    github_token = os.environ.get("GITHUB_TOKEN")
    if github_token:
        subprocess.run(
            ["git", "remote", "set-url", "origin",
             f"https://x-access-token:{github_token}@github.com/{owner}/{repo}.git"],
            cwd=repo_dir, check=True, capture_output=True,
        )

    subprocess.run(
        ["git", "push", "-u", "--force", "origin", f"HEAD:{branch}"],
        cwd=repo_dir, check=True, capture_output=True, text=True,
    )

    pr_body = (report or {}).get("pr_body", f"Fixes #{issue_number}")
    result = subprocess.run(
        ["gh", "pr", "create", "--title", title, "--body", pr_body,
         "--head", branch, "--repo", f"{owner}/{repo}"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode != 0:
        if "already exists" in result.stderr:
            find = subprocess.run(
                ["gh", "pr", "view", branch, "--repo", f"{owner}/{repo}", "--json", "url", "--jq", ".url"],
                capture_output=True, text=True,
            )
            return find.stdout.strip() if find.stdout.strip() else None
        return None
    return result.stdout.strip()


def gha_output(name: str, value: str) -> None:
    """Write a GitHub Actions output."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{name}={value}\n")


def main():
    parser = argparse.ArgumentParser(description="Code Healer — fix bugs via Managed Agents API")
    parser.add_argument("--issue", required=True, help="GitHub issue URL")
    parser.add_argument("--connection", default="devrel", help="Snowflake connection name")
    parser.add_argument("--workspace", default=None, help="Workspace FQN (default: USER$<user>.PUBLIC.DEFAULT$)")
    parser.add_argument("--json", action="store_true", dest="json_only")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo-dir", default=None)
    parser.add_argument("--ci", action="store_true")
    args = parser.parse_args()

    owner, repo, issue_number = parse_issue_url(args.issue)

    # Determine workspace
    if args.workspace:
        workspace = args.workspace
    else:
        conn = snowflake.connector.connect(connection_name=args.connection)
        user = conn.cursor().execute("SELECT CURRENT_USER()").fetchone()[0]
        conn.close()
        workspace = f"USER${user}.PUBLIC.DEFAULT$"

    if not args.json_only:
        print(f"Fetching issue #{issue_number} from {owner}/{repo}...")
    issue = fetch_issue(owner, repo, issue_number)

    if not args.json_only:
        print(f"  Title: {issue['title']}")

    # Prepare repo
    if args.repo_dir:
        repo_dir = Path(args.repo_dir)
    else:
        repo_dir = prepare_repo(owner, repo, issue_number)

    # Upload to workspace
    upload_to_workspace(repo_dir, workspace, args.connection)

    # Call Managed Agents API
    if not args.json_only:
        print(f"\n  Calling Managed Agents API (code_toolset_all)...")
        print(f"  Workspace: {workspace}")
        print(f"  Agent runs server-side in Snowflake's managed sandbox.\n")

    prompt = (
        f"Fix the bug described in this GitHub issue:\n\n"
        f"**{issue['title']}**\n\n"
        f"{issue.get('body') or '(no description)'}\n\n"
        f"The repository is at /workspace/heal/. "
        f"Investigate the codebase, find the root cause, fix it, and run tests. "
        f"After fixing, output a JSON block with: pr_title, pr_body, root_cause, confidence (high/medium/low)."
    )

    report = call_managed_agent(args.connection, workspace, prompt, args.json_only)

    # Download fixed files back
    download_from_workspace(repo_dir, workspace, args.connection)

    # Submit PR
    if not args.json_only:
        print("\n\nSubmitting PR...")
    pr_url = submit_pr(repo_dir, owner, repo, issue_number, report, args.dry_run)

    confidence = (report or {}).get("confidence", "unknown")

    if pr_url:
        print(f"\n  PR created: {pr_url}")
    if args.ci:
        gha_output("status", "success" if pr_url else "no-changes")
        gha_output("pr-url", pr_url or "")
        gha_output("confidence", confidence)


if __name__ == "__main__":
    main()
