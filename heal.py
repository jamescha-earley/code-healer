"""Code Healer -- automated bug fixing from GitHub issues.

Calls the Snowflake Managed Agents API (code_toolset_all) to investigate
and fix bugs. The agent runs server-side in Snowflake's managed sandbox.

No CLI install. No local containers. Pure REST.

Usage:
    python heal.py --issue https://github.com/org/repo/issues/123
    python heal.py --issue https://github.com/org/repo/issues/42 --dry-run
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
        ["gh", "api", f"repos/{owner}/{repo}/issues/{number}",
         "--jq", '{title: .title, body: .body, labels: [.labels[].name], state: .state}'],
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout)


def prepare_repo(owner: str, repo: str, issue_number: int) -> Path:
    """Clone the repo and create a fix branch."""
    repo_dir = Path(tempfile.mkdtemp(prefix=f"heal-{repo}-{issue_number}-"))
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


def read_repo_files(repo_dir: Path) -> dict[str, str]:
    """Read all source files from the repo (skip .git, binary, large files)."""
    files = {}
    for f in sorted(repo_dir.rglob("*")):
        if f.is_file() and ".git" not in f.parts:
            rel = str(f.relative_to(repo_dir))
            try:
                content = f.read_text()
                if len(content) < 50_000:  # skip large files
                    files[rel] = content
            except (UnicodeDecodeError, PermissionError):
                continue
    return files


def build_prompt(issue: dict, issue_number: int, files: dict[str, str]) -> str:
    """Build the agent prompt with issue details and file contents."""
    file_section = "\n\n".join(
        f"--- {path} ---\n```\n{content}\n```"
        for path, content in files.items()
    )

    return (
        f"Fix the bug described in issue #{issue_number}:\n\n"
        f"**{issue['title']}**\n\n"
        f"{issue.get('body') or '(no description)'}\n\n"
        f"---\n\n"
        f"Here are the repository files:\n\n{file_section}\n\n"
        f"---\n\n"
        f"Instructions:\n"
        f"1. Identify the root cause of each failing test\n"
        f"2. Fix the source files (not the tests)\n"
        f"3. For each file you fix, output the COMPLETE fixed file content in a fenced code block "
        f"labeled with the filename, like:\n\n"
        f"```python:query_builder.py\n# full fixed content\n```\n\n"
        f"4. After showing fixes, output a JSON block with: "
        f"pr_title, pr_body, root_cause, confidence (high/medium/low)"
    )


def call_managed_agent(connection_name: str, prompt: str, json_only: bool = False) -> str:
    """Call the Cortex Agents API. Returns full response text."""
    conn = snowflake.connector.connect(connection_name=connection_name)
    token = conn.rest.token
    account_url = f"https://{conn.host}"
    url = f"{account_url}/api/v2/cortex/agent:run"

    payload = {
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": prompt}]}
        ],
        "models": {"orchestration": "claude-sonnet-4-5"},
    }

    headers = {
        "Authorization": f"Snowflake Token=\"{token}\"",
        "Content-Type": "application/json",
        "Accept": "text/event-stream",
    }

    response = requests.post(url, json=payload, headers=headers, stream=True)

    if response.status_code != 200:
        print(f"API Error {response.status_code}: {response.text[:500]}", file=sys.stderr)
        conn.close()
        return ""

    print(f"  API responded (status {response.status_code})", file=sys.stderr)

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
    return "".join(full_text)


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


def extract_fixed_files(response_text: str) -> dict[str, str]:
    """Extract fixed file contents from agent response."""
    files = {}
    # Match ```python:filename.py or ```filename.py patterns
    pattern = r'```(?:python)?:?([\w./]+\.py)\n(.*?)```'
    for match in re.finditer(pattern, response_text, re.DOTALL):
        filename = match.group(1)
        content = match.group(2)
        files[filename] = content
    return files


def extract_json_report(text: str) -> dict | None:
    """Try to find a JSON object with pr_title in the agent output."""
    for match in re.finditer(r'```json?\s*\n(.*?)\n```', text, re.DOTALL):
        try:
            obj = json.loads(match.group(1))
            if "pr_title" in obj:
                return obj
        except json.JSONDecodeError:
            continue
    for match in re.finditer(r'\{[^{}]*"pr_title"[^{}]*\}', text, re.DOTALL):
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            continue
    return None


def apply_fixes(repo_dir: Path, fixed_files: dict[str, str]) -> int:
    """Write fixed files back to the repo. Returns number of files changed."""
    changed = 0
    for filename, content in fixed_files.items():
        target = repo_dir / filename
        if target.exists():
            original = target.read_text()
            if original != content:
                target.write_text(content)
                changed += 1
    return changed


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
        return None

    subprocess.run(["git", "add", "-A"], cwd=repo_dir, check=True, capture_output=True)

    title = (report or {}).get("pr_title", f"Fix issue #{issue_number}")
    subprocess.run(
        ["git", "commit", "-m", f"fix: {title}\n\nCloses #{issue_number}"],
        cwd=repo_dir, check=True, capture_output=True, text=True,
    )

    if dry_run:
        diff = subprocess.run(["git", "diff", "HEAD~1", "--stat"], cwd=repo_dir, capture_output=True, text=True)
        print(f"\n  [dry-run] Changes:\n{diff.stdout}")
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
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{name}={value}\n")


def main():
    parser = argparse.ArgumentParser(description="Code Healer — fix bugs via Managed Agents API")
    parser.add_argument("--issue", required=True, help="GitHub issue URL")
    parser.add_argument("--connection", default="devrel", help="Snowflake connection name")
    parser.add_argument("--json", action="store_true", dest="json_only")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--repo-dir", default=None)
    parser.add_argument("--ci", action="store_true")
    args = parser.parse_args()

    owner, repo, issue_number = parse_issue_url(args.issue)

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

    # Read all files
    files = read_repo_files(repo_dir)
    if not args.json_only:
        print(f"  Files: {len(files)}")

    # Build prompt and call the API
    prompt = build_prompt(issue, issue_number, files)

    if not args.json_only:
        print(f"\n  Calling Managed Agents API (code_toolset_all)...\n")

    response_text = call_managed_agent(args.connection, prompt, args.json_only)

    # Extract fixes and report
    fixed_files = extract_fixed_files(response_text)
    report = extract_json_report(response_text)

    if not args.json_only:
        print(f"\n\n  Fixed files: {list(fixed_files.keys())}")
        if report:
            print(f"  Confidence: {report.get('confidence', 'unknown')}")

    # Apply fixes to local repo
    changed = apply_fixes(repo_dir, fixed_files)
    if not args.json_only:
        print(f"  Applied {changed} file changes.")

    # Submit PR
    if changed > 0:
        pr_url = submit_pr(repo_dir, owner, repo, issue_number, report, args.dry_run)
        if pr_url:
            print(f"\n  PR: {pr_url}")
        if args.ci:
            gha_output("status", "success" if pr_url else "no-changes")
            gha_output("pr-url", pr_url or "")
            gha_output("confidence", (report or {}).get("confidence", "unknown"))
    else:
        print("  No changes produced.")
        if args.ci:
            gha_output("status", "no-changes")
            gha_output("pr-url", "")
            gha_output("confidence", "")


if __name__ == "__main__":
    main()
