"""Git history helpers for agent-journal tools."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


@dataclass
class GitCommit:
    hash: str
    timestamp: str
    message: str


def git_log(
    since: str | None = None,
    limit: int = 20,
    repo_path: Path = PROJECT_ROOT,
) -> list[GitCommit]:
    cmd = [
        "git", "-C", str(repo_path), "log",
        f"--max-count={limit}",
        "--format=%H|%aI|%s",
    ]
    if since:
        cmd.append(f"--since={since}")
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    commits = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("|", 2)
        if len(parts) == 3:
            commits.append(GitCommit(hash=parts[0][:8], timestamp=parts[1], message=parts[2]))
    return commits


def git_diff_stat(repo_path: Path = PROJECT_ROOT) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "diff", "--stat"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def git_status_short(repo_path: Path = PROJECT_ROOT) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ""


def new_result_files(since: str | None = None, repo_path: Path = PROJECT_ROOT) -> list[str]:
    """Find new files in results/ directories."""
    cmd = ["git", "-C", str(repo_path), "status", "--porcelain"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []

    new_files = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        status = line[:2].strip()
        filepath = line[3:].strip()
        if "results/" in filepath and status in ("??", "A"):
            new_files.append(filepath)
    return new_files
