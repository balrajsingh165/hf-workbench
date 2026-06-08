"""Build a context document for resuming work after interruption.

Assembles recent session digests, git activity, uncommitted changes,
new result files, and notes.md tail into a single context block.

Usage:
    # Resume context for a specific project
    python3 agent-journal/resume.py --project proj-1-onboarding

    # Morning catchup -- last 12 hours across all projects
    python3 agent-journal/resume.py --overnight

    # Resume from last session
    python3 agent-journal/resume.py --project proj-1-onboarding --last-session

    # Custom time window
    python3 agent-journal/resume.py --project proj-1-onboarding --since 2026-04-06

    # Include LLM-powered session topics
    python3 agent-journal/resume.py --project proj-1-onboarding --with-topics
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # project root (for shared.gemini)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.session_io import (
    DEFAULT_ROOTS,
    agent_tag,
    extract_conversation_text,
    extract_tool_summary,
    list_all_sessions,
    list_sessions,
    load_session_content,
)
from lib.git_context import git_log, git_diff_stat, git_status_short, new_result_files

OPS_ROOT = Path(__file__).resolve().parent.parent
DIGEST_DIR = OPS_ROOT / "docs" / "digests"


def _load_env():
    from lib.env import load_agent_env
    load_agent_env()


def _read_tail(filepath: Path, lines: int = 50) -> str:
    if not filepath.exists():
        return ""
    all_lines = filepath.read_text().splitlines()
    tail = all_lines[-lines:]
    return "\n".join(tail)


def _find_recent_digests(since: str | None = None, limit: int = 5) -> list[tuple[str, str]]:
    if not DIGEST_DIR.exists():
        return []
    digests = []
    for f in sorted(DIGEST_DIR.iterdir(), reverse=True):
        if f.suffix != ".md":
            continue
        if since and f.name[:8] < since.replace("-", ""):
            continue
        content = f.read_text()
        digests.append((f.name, content))
        if len(digests) >= limit:
            break
    return digests


def _find_project_dirs() -> list[str]:
    return sorted(
        d.name for d in OPS_ROOT.iterdir()
        if d.is_dir() and d.name.startswith("proj-")
    )


def build_resume_context(
    project: str | None = None,
    since: str | None = None,
    overnight: bool = False,
    last_session: bool = False,
    with_topics: bool = False,
    claude_project: str = "-home-appuser-hf-workbench",
    factory_project: str = "-home-appuser-hf-workbench",
) -> str:
    sections: list[str] = []

    if overnight:
        since = (datetime.now(timezone.utc) - timedelta(hours=12)).strftime("%Y-%m-%d")
    if not since:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d")

    # 1. Recent digests
    digests = _find_recent_digests(since=since)
    if digests:
        sections.append("# Recent Session Digests\n")
        for name, content in digests:
            sections.append(f"## {name}\n{content}\n")
    else:
        # Fall back to session summaries across all agent types
        all_sessions = list_all_sessions(limit_per_type=20)
        all_sessions = [s for s in all_sessions if s.started_at and s.started_at[:10] >= since]
        if last_session:
            all_sessions = all_sessions[:1]
        else:
            all_sessions = all_sessions[:8]

        if all_sessions:
            sections.append("# Recent Sessions\n")
            for s in all_sessions:
                tag = agent_tag(s.agent_type)
                sections.append(
                    f"- [{tag}] `{s.session_id[:8]}` "
                    f"{s.started_at[:16] if s.started_at else '?'} "
                    f"({s.record_count}r) {s.title[:70]}"
                )
                if with_topics:
                    try:
                        content = load_session_content(
                            project=s.project, session_id=s.session_id,
                            agent_type=s.agent_type,
                        )
                        from search import get_session_topics_llm
                        topics = get_session_topics_llm([(s, content)])
                        if topics:
                            sections.append(f"  Topics: {topics[0]['llm_output']}\n")
                    except Exception as e:
                        sections.append(f"  [topic extraction failed: {e}]\n")

    # 2. Git activity
    commits = git_log(since=since)
    if commits:
        sections.append("# Git Activity\n")
        for c in commits:
            sections.append(f"- `{c.hash}` {c.timestamp[:16]} {c.message}")
        sections.append("")

    # 3. Uncommitted changes (compact)
    diff_stat = git_diff_stat()
    status = git_status_short()
    if diff_stat or status:
        sections.append("# Working Tree")
        if diff_stat:
            sections.append(f"```\n{diff_stat}\n```")
        if status:
            new_results = [l[3:].strip() for l in status.split("\n") if l.startswith("??") and "results/" in l]
            if new_results:
                sections.append("New result files: " + ", ".join(new_results[:10]))
        sections.append("")

    # 4. Project notes tail (compact)
    if project:
        notes_path = OPS_ROOT / project / "notes.md"
        tail = _read_tail(notes_path, lines=30)
        if tail:
            sections.append(f"# {project}/notes.md (last 30 lines)\n```\n{tail}\n```\n")
    else:
        for proj_dir in _find_project_dirs():
            notes_path = OPS_ROOT / proj_dir / "notes.md"
            tail = _read_tail(notes_path, lines=15)
            if tail:
                sections.append(f"# {proj_dir}/notes.md (tail)\n```\n{tail}\n```\n")

    return "\n".join(sections)


def main():
    parser = argparse.ArgumentParser(description="Build resume context for continuing work")
    parser.add_argument("--project", help="Specific project dir (e.g. proj-1-onboarding)")
    parser.add_argument("--overnight", action="store_true", help="Morning catchup: last 12 hours")
    parser.add_argument("--last-session", action="store_true", help="Focus on the most recent session")
    parser.add_argument("--since", help="Time window start (YYYY-MM-DD)")
    parser.add_argument("--with-topics", action="store_true", help="Use LLM to extract session topics")
    parser.add_argument("--claude-project", default="-home-appuser-hf-workbench", help="Claude project dir name")
    parser.add_argument("--factory-project", default="-home-appuser-hf-workbench", help="Factory project dir name")
    parser.add_argument("--output", help="Save to file instead of stdout")
    args = parser.parse_args()

    _load_env()

    context = build_resume_context(
        project=args.project,
        since=args.since,
        overnight=args.overnight,
        last_session=args.last_session,
        with_topics=args.with_topics,
        claude_project=args.claude_project,
        factory_project=args.factory_project,
    )

    if args.output:
        Path(args.output).write_text(context)
        print(f"Resume context saved to {args.output}")
    else:
        print(context)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
