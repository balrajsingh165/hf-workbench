"""Enhanced search across agent session history with time filtering,
multi-keyword matching, topic awareness, and git correlation.

Usage:
    # Search for keywords in recent sessions
    python3 agent-journal/search.py --text "steering alpha" --recent 5

    # Search today's sessions
    python3 agent-journal/search.py --text "contrastive" --today

    # List session topics (LLM-powered)
    python3 agent-journal/search.py --topics --recent 5

    # Search with context (like grep -C)
    python3 agent-journal/search.py --text "paper" --context 3

    # Search with git correlation
    python3 agent-journal/search.py --text "steering" --with-git

    # Search only in digests (fast, high signal)
    python3 agent-journal/search.py --digests --text "eureka"

    # Filter by time
    python3 agent-journal/search.py --text "model" --since 2026-04-05 --before 2026-04-07

    # Filter by hours ago
    python3 agent-journal/search.py --text "experiment" --last-hours 24

    # JSON output for agent consumption
    python3 agent-journal/search.py --text "paper" --format json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # project root (for shared.gemini)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.session_io import (
    AGENT_TYPES,
    DEFAULT_ROOTS,
    SessionContent,
    SessionInfo,
    agent_tag,
    content_items_to_text,
    extract_conversation_text,
    is_noise_message,
    list_all_sessions,
    list_sessions,
    load_session_content,
    normalize_prompt_text,
)
from lib.git_context import git_log

DIGEST_DIR = Path(__file__).resolve().parent.parent / "docs" / "digests"


def _load_env():
    from lib.env import load_agent_env
    load_agent_env()


def _multi_keyword_match(text: str, keywords: list[str]) -> bool:
    """All keywords must appear in text (case-insensitive)."""
    text_lower = text.lower()
    return all(kw.lower() in text_lower for kw in keywords)


def _filter_sessions_by_time(
    sessions: list[SessionInfo],
    since: str | None = None,
    before: str | None = None,
    last_hours: int | None = None,
    today: bool = False,
) -> list[SessionInfo]:
    if today:
        since = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if last_hours:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=last_hours)).isoformat()
        sessions = [s for s in sessions if s.updated_at and s.updated_at >= cutoff]

    if since:
        sessions = [s for s in sessions if s.started_at and s.started_at[:10] >= since]

    if before:
        sessions = [s for s in sessions if s.started_at and s.started_at[:10] <= before]

    return sessions


@dataclass
class SearchMatch:
    session_id: str
    timestamp: str
    role: str  # USER / ASSISTANT
    text: str
    context_before: list[str]
    context_after: list[str]


@dataclass
class SearchResult:
    session: SessionInfo
    matches: list[SearchMatch]
    git_commits: list[dict] | None = None


def search_session(
    content: SessionContent,
    keywords: list[str],
    context_lines: int = 0,
    skip_noise: bool = True,
) -> list[SearchMatch]:
    events: list[tuple[str, str, str]] = []
    for msg in content.user_messages:
        if skip_noise and is_noise_message(msg["text"]):
            continue
        events.append((msg["timestamp"], "USER", msg["text"]))
    for msg in content.assistant_messages:
        events.append((msg["timestamp"], "ASSISTANT", msg["text"]))
    events.sort(key=lambda e: e[0])

    matches: list[SearchMatch] = []
    for i, (ts, role, text) in enumerate(events):
        if _multi_keyword_match(text, keywords):
            ctx_before = []
            ctx_after = []
            if context_lines > 0:
                for j in range(max(0, i - context_lines), i):
                    ctx_before.append(f"[{events[j][1]}] {events[j][2][:150]}")
                for j in range(i + 1, min(len(events), i + 1 + context_lines)):
                    ctx_after.append(f"[{events[j][1]}] {events[j][2][:150]}")
            matches.append(SearchMatch(
                session_id=content.info.session_id,
                timestamp=ts,
                role=role,
                text=text[:200],
                context_before=ctx_before,
                context_after=ctx_after,
            ))
    return matches


def search_digests(keywords: list[str]) -> list[dict]:
    """Search pre-generated digest files."""
    if not DIGEST_DIR.exists():
        return []

    results = []
    for f in sorted(DIGEST_DIR.iterdir(), reverse=True):
        if f.suffix != ".md":
            continue
        text = f.read_text()
        if _multi_keyword_match(text, keywords):
            # Extract matching lines with context
            lines = text.split("\n")
            matching_lines = []
            for i, line in enumerate(lines):
                if _multi_keyword_match(line, keywords):
                    start = max(0, i - 1)
                    end = min(len(lines), i + 2)
                    matching_lines.append("\n".join(lines[start:end]))
            results.append({
                "file": str(f),
                "filename": f.name,
                "matches": matching_lines[:10],
            })
    return results


def get_session_topics_llm(
    sessions: list[tuple[SessionInfo, SessionContent]],
) -> list[dict]:
    """Use Gemini to extract topics from sessions."""
    from shared.gemini import generate_text_with_retry, GEMINI_3_FLASH_PREVIEW

    results = []
    for info, content in sessions:
        # Build a compact representation for topic extraction
        user_texts = [m["text"][:300] for m in content.user_messages[:20]]
        assistant_texts = [m["text"][:300] for m in content.assistant_messages[:15]]
        compact = (
            f"Session started: {info.started_at}\n"
            f"Session ended: {info.updated_at}\n\n"
            f"User messages ({len(content.user_messages)} total, showing first 20):\n"
            + "\n---\n".join(user_texts)
            + f"\n\nAssistant messages ({len(content.assistant_messages)} total, showing first 15):\n"
            + "\n---\n".join(assistant_texts)
        )

        try:
            result = generate_text_with_retry(
                contents=compact[:30000],
                model=GEMINI_3_FLASH_PREVIEW,
                system_instruction=(
                    "Extract 3-8 topic labels from this conversation between a "
                    "researcher and an AI agent. Each topic should be 3-8 words. "
                    "Also write a one-sentence summary of the session. "
                    "Also list any important references (papers, URLs, model names, "
                    "dataset names, specific numbers/thresholds).\n\n"
                    "Output format (plain text, no markdown fences):\n"
                    "TOPICS: topic1 | topic2 | topic3\n"
                    "SUMMARY: one sentence summary\n"
                    "REFS: ref1 | ref2 | ref3"
                ),
                max_output_tokens=512,
            )
            results.append({
                "session_id": info.session_id,
                "started_at": info.started_at,
                "updated_at": info.updated_at,
                "record_count": info.record_count,
                "llm_output": result.text,
            })
        except Exception as e:
            results.append({
                "session_id": info.session_id,
                "started_at": info.started_at,
                "updated_at": info.updated_at,
                "record_count": info.record_count,
                "llm_output": f"[error: {e}]",
            })

    return results


def format_results_brief(results: list[SearchResult]) -> str:
    lines = []
    for r in results:
        tag = agent_tag(r.session.agent_type)
        lines.append(
            f"\n[{tag}] {r.session.session_id[:8]} "
            f"({r.session.started_at[:16] if r.session.started_at else '?'}) "
            f"{len(r.matches)} matches"
        )
        for m in r.matches[:5]:
            text_preview = m.text[:120].replace("\n", " ")
            lines.append(f"  {m.timestamp[11:19]} {m.role[0]}: {text_preview}")
        if len(r.matches) > 5:
            lines.append(f"  ... +{len(r.matches)-5} more")
        if r.git_commits:
            for c in r.git_commits[:3]:
                lines.append(f"  git: {c['hash']} {c['message']}")
    return "\n".join(lines)


def format_results_full(results: list[SearchResult]) -> str:
    lines = []
    for r in results:
        lines.append(
            f"\n{'='*60}\n"
            f"Session: {r.session.session_id}\n"
            f"Started: {r.session.started_at}\n"
            f"Updated: {r.session.updated_at}\n"
            f"Matches: {len(r.matches)}\n"
            f"{'='*60}"
        )
        for m in r.matches[:10]:
            if m.context_before:
                for ctx in m.context_before:
                    lines.append(f"  | {ctx}")
            lines.append(f"  > [{m.timestamp[:19]}] {m.role}: {m.text[:200]}")
            if m.context_after:
                for ctx in m.context_after:
                    lines.append(f"  | {ctx}")
            lines.append("")
        if r.git_commits:
            lines.append("  Related git commits:")
            for c in r.git_commits[:5]:
                lines.append(f"    {c['hash']} {c['timestamp'][:16]} {c['message']}")
    return "\n".join(lines)


def format_results_json(results: list[SearchResult]) -> str:
    data = []
    for r in results:
        data.append({
            "session_id": r.session.session_id,
            "started_at": r.session.started_at,
            "updated_at": r.session.updated_at,
            "match_count": len(r.matches),
            "matches": [
                {
                    "timestamp": m.timestamp,
                    "role": m.role,
                    "text": m.text,
                    "context_before": m.context_before,
                    "context_after": m.context_after,
                }
                for m in r.matches[:20]
            ],
            "git_commits": r.git_commits,
        })
    return json.dumps(data, indent=2, ensure_ascii=False)


def main():
    parser = argparse.ArgumentParser(description="Enhanced session history search")
    parser.add_argument("--project", default="-home-appuser-hf-workbench", help="Project directory name")
    parser.add_argument("--agent", choices=["claude", "codex", "factory"], default=None,
                        help="Agent type (default: search all types)")
    parser.add_argument("--root", type=Path, default=None, help="Override history root")

    # Search mode
    parser.add_argument("--text", help="Keywords to search (space-separated, all must match)")
    parser.add_argument("--topics", action="store_true", help="Extract topics from sessions using LLM")
    parser.add_argument("--digests", action="store_true", help="Search digest files instead of raw sessions")

    # Time filters
    parser.add_argument("--since", help="Sessions since date (YYYY-MM-DD)")
    parser.add_argument("--before", help="Sessions before date (YYYY-MM-DD)")
    parser.add_argument("--last-hours", type=int, help="Sessions from last N hours")
    parser.add_argument("--today", action="store_true", help="Today's sessions only")
    parser.add_argument("--recent", type=int, help="N most recent sessions")

    # Output control
    parser.add_argument("--context", type=int, default=0, help="Context lines around matches")
    parser.add_argument("--with-git", action="store_true", help="Correlate with git commits")
    parser.add_argument("--format", choices=["brief", "full", "json"], default="brief")
    parser.add_argument("--limit", type=int, default=10, help="Max matches per session")

    args = parser.parse_args()

    if not args.text and not args.topics and not args.digests:
        parser.error("Provide --text, --topics, or --digests")

    _load_env()

    # Digest search mode
    if args.digests and args.text:
        keywords = args.text.split()
        results = search_digests(keywords)
        if not results:
            print("No matches in digests.")
            return 0
        for r in results:
            print(f"\n--- {r['filename']} ---")
            for match in r["matches"]:
                print(match)
        return 0

    # Load sessions — search all agent types when --agent is not specified
    if args.agent:
        root = args.root or DEFAULT_ROOTS.get(args.agent)
        sessions = list_sessions(args.project, root, agent_type=args.agent)
    else:
        sessions = list_all_sessions(limit_per_type=200)
        if args.root:
            # root override only makes sense with a specific agent type
            pass

    sessions = _filter_sessions_by_time(
        sessions,
        since=args.since,
        before=args.before,
        last_hours=args.last_hours,
        today=args.today,
    )
    if args.recent:
        sessions = sessions[:args.recent]

    if not sessions:
        print("No sessions found matching time criteria.")
        return 0

    # Topic extraction mode
    if args.topics:
        print(f"Extracting topics from {len(sessions)} session(s) using Gemini...")
        session_contents = []
        for s in sessions:
            try:
                content = load_session_content(
                    project=s.project, session_id=s.session_id,
                    agent_type=s.agent_type, file_path=s.file_path,
                )
                session_contents.append((s, content))
            except FileNotFoundError:
                continue
        topic_results = get_session_topics_llm(session_contents)
        for tr in topic_results:
            print(f"\n--- Session {tr['session_id'][:8]}... ---")
            print(f"  Time: {tr['started_at']} -> {tr['updated_at']}")
            print(f"  Records: {tr['record_count']}")
            print(f"  {tr['llm_output']}")
        return 0

    # Keyword search mode
    if not args.text:
        parser.error("--text required for search mode")

    keywords = args.text.split()
    log = sys.stderr if args.format == "json" else sys.stdout
    agent_label = args.agent or "all"
    print(f"Searching {len(sessions)} {agent_label} session(s) for: {keywords}", file=log)

    results: list[SearchResult] = []
    for session in sessions:
        try:
            content = load_session_content(
                project=session.project, session_id=session.session_id,
                agent_type=session.agent_type, file_path=session.file_path,
            )
        except FileNotFoundError:
            continue

        matches = search_session(content, keywords, context_lines=args.context)
        if not matches:
            continue

        git_commits = None
        if args.with_git and session.started_at and session.updated_at:
            commits = git_log(since=session.started_at[:10])
            git_commits = [
                {"hash": c.hash, "timestamp": c.timestamp, "message": c.message}
                for c in commits
                if c.timestamp >= session.started_at and c.timestamp <= session.updated_at
            ]

        results.append(SearchResult(
            session=session,
            matches=matches[:args.limit],
            git_commits=git_commits,
        ))

    if not results:
        if args.format == "json":
            print("[]")
        else:
            print("No matches found.")
        return 0

    total_matches = sum(len(r.matches) for r in results)
    print(f"Found {total_matches} matches across {len(results)} session(s)\n", file=log)

    if args.format == "json":
        print(format_results_json(results))
    elif args.format == "full":
        print(format_results_full(results))
    else:
        print(format_results_brief(results))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
