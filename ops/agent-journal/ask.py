"""LLM-powered semantic question answering over session history.

Goes beyond keyword search: sends session context to Gemini with a
natural-language question and synthesizes an answer with citations.

Usage:
    # Ask across recent digests (fast, default)
    python3 agent-journal/ask.py --query "what experiment setups did we align with from papers?"

    # Ask across raw session transcripts (deeper, slower)
    python3 agent-journal/ask.py --query "how many times did the server fail?" --raw

    # Scope to Factory sessions, last 24 hours
    python3 agent-journal/ask.py --query "common training failures" --agent factory --last-hours 24

    # Ask across last 5 sessions
    python3 agent-journal/ask.py --query "what learning rates were tried?" --recent 5
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # project root (for shared.gemini)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.session_io import (
    DEFAULT_ROOTS,
    SessionInfo,
    extract_conversation_text,
    extract_tool_summary,
    list_all_sessions,
    list_sessions,
    load_session_content,
)

DIGEST_DIR = Path(__file__).resolve().parent.parent / "docs" / "digests"

EXTRACT_SYSTEM_PROMPT = """\
You are a research lab assistant. You are given a session transcript between
a researcher and an AI agent, plus a QUESTION from the researcher.

Your job: extract ALL information from this transcript that is relevant to
answering the question. Include specific details -- exact numbers, file paths,
model names, timestamps, error messages, decisions, and reasoning.

If the transcript contains nothing relevant, respond with exactly: NO_RELEVANT_INFO

Otherwise, respond with a concise but detailed extraction. Preserve verbatim
quotes when they matter. Cite approximate timestamps when available.\
"""

SYNTHESIZE_SYSTEM_PROMPT = """\
You are a research lab assistant. You have been given extractions from multiple
agent session transcripts, each tagged with a session ID and time range.

Synthesize these into a single coherent answer to the researcher's question.
Be specific and cite session IDs when attributing information.
If extractions contradict each other, note the contradiction.
If no extractions contain relevant info, say so directly.

Format: clear prose, use bullet points for lists. End with a "Sources" section
listing session IDs and dates that contributed to the answer.\
"""


def _load_env():
    from lib.env import load_agent_env
    load_agent_env()


def _find_digests(
    since: str | None = None,
    agent_type: str | None = None,
) -> list[tuple[str, str]]:
    """Return (filename, content) pairs from docs/digests/."""
    if not DIGEST_DIR.exists():
        return []
    digests = []
    for f in sorted(DIGEST_DIR.iterdir(), reverse=True):
        if f.suffix != ".md":
            continue
        if since and f.name[:8] < since.replace("-", ""):
            continue
        content = f.read_text()
        if agent_type:
            # Filter by agent type in digest header
            if f"**Agent:** {agent_type}" not in content:
                continue
        digests.append((f.name, content))
    return digests


def _filter_sessions_by_time(
    sessions: list[SessionInfo],
    since: str | None = None,
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
    return sessions


def ask_digests(query: str, digests: list[tuple[str, str]]) -> str:
    """Send all digests + query to Gemini in one call (digests are compact)."""
    from shared.gemini import generate_text_with_retry, GEMINI_3_FLASH_PREVIEW

    context_parts = []
    for filename, content in digests:
        context_parts.append(f"=== DIGEST: {filename} ===\n{content}\n")
    context = "\n".join(context_parts)

    # Digests are small enough to fit in one call
    user_prompt = f"""\
QUESTION: {query}

The following are structured digests from recent agent sessions.
Answer the question using information from these digests.

{context}

Answer the question now. Cite digest filenames as sources.\
"""
    result = generate_text_with_retry(
        contents=user_prompt,
        model=GEMINI_3_FLASH_PREVIEW,
        system_instruction=SYNTHESIZE_SYSTEM_PROMPT,
        max_output_tokens=4096,
    )
    return result.text


def ask_sessions_raw(
    query: str,
    sessions: list[SessionInfo],
    project: str,
    history_root: Path,
    agent_type: str,
) -> str:
    """Per-session extraction in parallel, then synthesis."""
    from shared.gemini import (
        batch_generate_texts,
        generate_text_with_retry,
        GeminiRequest,
        GEMINI_3_FLASH_PREVIEW,
    )

    # Phase 1: build extraction requests
    session_data: list[tuple[SessionInfo, str]] = []
    requests: list[GeminiRequest] = []

    for session in sessions:
        try:
            content = load_session_content(
                project=project, session_id=session.session_id,
                history_root=history_root, agent_type=agent_type,
            )
        except FileNotFoundError:
            continue

        conv_text = extract_conversation_text(content, max_chars=80000)
        if len(conv_text.strip()) < 50:
            continue

        session_label = (
            f"Session {session.session_id[:8]} "
            f"({session.started_at[:16] if session.started_at else '?'} to "
            f"{session.updated_at[:16] if session.updated_at else '?'})"
        )
        session_data.append((session, session_label))

        user_prompt = f"""\
QUESTION: {query}

--- SESSION TRANSCRIPT ---
{conv_text}
--- END TRANSCRIPT ---

Extract all information relevant to the question.\
"""
        requests.append(GeminiRequest(
            contents=user_prompt,
            model=GEMINI_3_FLASH_PREVIEW,
            system_instruction=EXTRACT_SYSTEM_PROMPT,
            max_output_tokens=2048,
        ))

    if not requests:
        return "No sessions with sufficient content found."

    print(f"  Extracting from {len(requests)} session(s) in parallel...")
    batch_results = batch_generate_texts(requests, max_workers=min(len(requests), 8))

    # Collect non-empty extractions
    extractions: list[str] = []
    for br in batch_results:
        session, label = session_data[br.index]
        if br.error:
            print(f"  FAILED: {session.session_id[:8]}: {br.error}")
            continue
        text = br.result.text.strip()
        if text == "NO_RELEVANT_INFO" or len(text) < 20:
            print(f"  Skip: {session.session_id[:8]} (no relevant info)")
            continue
        print(f"  Hit:  {session.session_id[:8]} ({len(text)} chars extracted)")
        extractions.append(f"=== {label} ===\n{text}\n")

    if not extractions:
        return "No relevant information found across the searched sessions."

    # Phase 2: synthesize
    print(f"  Synthesizing from {len(extractions)} extraction(s)...")
    synthesis_prompt = f"""\
QUESTION: {query}

The following are extractions from individual agent sessions, each containing
information relevant to the question:

{"".join(extractions)}

Synthesize a comprehensive answer now.\
"""
    result = generate_text_with_retry(
        contents=synthesis_prompt,
        model=GEMINI_3_FLASH_PREVIEW,
        system_instruction=SYNTHESIZE_SYSTEM_PROMPT,
        max_output_tokens=4096,
    )
    return result.text


def main():
    parser = argparse.ArgumentParser(
        description="Ask natural-language questions over session history"
    )
    parser.add_argument("--query", "-q", required=True, help="Question to answer")
    parser.add_argument("--agent", choices=["claude", "codex", "factory"],
                        help="Filter by agent type")
    parser.add_argument("--project", default="-home-appuser-hf-workbench")
    parser.add_argument("--raw", action="store_true",
                        help="Search raw session transcripts instead of digests")
    parser.add_argument("--recent", type=int, help="N most recent sessions (raw mode)")
    parser.add_argument("--since", help="Sessions/digests since date (YYYY-MM-DD)")
    parser.add_argument("--last-hours", type=int, help="Sessions from last N hours")
    parser.add_argument("--today", action="store_true", help="Today only")
    parser.add_argument("--root", type=Path, default=None)
    args = parser.parse_args()

    _load_env()

    if args.raw:
        # Raw session mode: per-session extraction + synthesis
        agent_type = args.agent or "claude"
        root = args.root or DEFAULT_ROOTS.get(agent_type)
        sessions = list_sessions(args.project, root, agent_type=agent_type)
        sessions = _filter_sessions_by_time(
            sessions, since=args.since,
            last_hours=args.last_hours, today=args.today,
        )
        if args.recent:
            sessions = sessions[:args.recent]
        elif not args.since and not args.last_hours and not args.today:
            sessions = sessions[:5]  # default: last 5

        if not sessions:
            print("No sessions found.")
            return 1

        print(f"Asking across {len(sessions)} raw session(s)...")
        answer = ask_sessions_raw(
            args.query, sessions, args.project, root, agent_type,
        )
    else:
        # Digest mode (default): fast, compact
        since = args.since
        if args.today:
            since = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        elif args.last_hours:
            since = (datetime.now(timezone.utc) - timedelta(hours=args.last_hours)).strftime("%Y-%m-%d")

        digests = _find_digests(since=since, agent_type=args.agent)

        if args.recent:
            digests = digests[:args.recent]
        elif not since:
            digests = digests[:10]  # default: last 10 digests

        if not digests:
            print("No digests found. Run `aj digest` first, or use --raw for raw sessions.")
            return 1

        print(f"Asking across {len(digests)} digest(s)...")
        answer = ask_digests(args.query, digests)

    print(f"\n{'='*60}")
    print(answer)
    print(f"{'='*60}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
