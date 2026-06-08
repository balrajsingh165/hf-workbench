"""Digest agent sessions into structured summaries using Gemini 3 Flash.

A *different* agent (or human) runs this after a session ends to produce
a structured digest -- the original agent doesn't need to be alive.

Usage:
    # Digest the most recent session
    python3 agent-journal/digest.py --project=-home-appuser-hf-workbench --recent 1

    # Digest last 3 sessions
    python3 agent-journal/digest.py --project=-home-appuser-hf-workbench --recent 3

    # Digest a specific session
    python3 agent-journal/digest.py --project=-home-appuser-hf-workbench --session <session-id>

    # Digest sessions since a date
    python3 agent-journal/digest.py --project=-home-appuser-hf-workbench --since 2026-04-06

    # Dry run -- show what would be digested without calling LLM
    python3 agent-journal/digest.py --project=-home-appuser-hf-workbench --recent 1 --dry-run
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))  # project root (for shared.gemini)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.session_io import (
    DEFAULT_ROOTS,
    SessionContent,
    SessionInfo,
    extract_conversation_text,
    extract_tool_summary,
    list_sessions,
    load_session_content,
)


DIGEST_DIR = Path(__file__).resolve().parent.parent / "docs" / "digests"

DIGEST_SYSTEM_PROMPT = """\
You are a research lab assistant. Your job is to read a session transcript
between a human researcher and an AI agent, and produce a structured digest.

The digest must capture:
1. TOPICS: List the main topics discussed (3-8 topics, each 3-10 words)
2. SUMMARY: 2-4 paragraph narrative of what happened, decisions made, and outcomes
3. KEY DECISIONS: Bullet list of important decisions with reasoning
4. EUREKA MOMENTS: Any insights, discoveries, or non-obvious findings worth preserving verbatim
5. REFERENCES: Papers, URLs, code paths, model names, dataset names mentioned
6. OPEN THREADS: What was left unfinished, pending, or planned for next session
7. RUNNING EXPERIMENTS: Any experiments launched that may still be running (VM names, expected completion)
8. GOTCHAS: Unexpected issues, bugs, workarounds discovered

Format your response as markdown with these exact section headers:
## Topics
## Summary
## Key Decisions
## Eureka Moments
## References
## Open Threads
## Running Experiments
## Gotchas

Be concise but preserve specific details -- exact model names, exact numbers,
exact file paths. These details are what gets lost in compression and are the
whole point of this digest.

If a section has nothing to report, write "None." under it.\
"""


def _load_env():
    from lib.env import load_agent_env
    load_agent_env()


def _build_user_prompt(conversation_text: str, tool_summary: str, session_info: SessionInfo) -> str:
    return f"""\
Session ID: {session_info.session_id}
Started: {session_info.started_at or 'unknown'}
Ended: {session_info.updated_at or 'unknown'}
Records: {session_info.record_count}

{tool_summary}

--- CONVERSATION TRANSCRIPT ---
{conversation_text}
--- END TRANSCRIPT ---

Produce the structured digest now.\
"""


def _call_gemini(conversation_text: str, tool_summary: str, session_info: SessionInfo) -> str:
    from shared.gemini import generate_text_with_retry, GEMINI_3_FLASH_PREVIEW

    result = generate_text_with_retry(
        contents=_build_user_prompt(conversation_text, tool_summary, session_info),
        model=GEMINI_3_FLASH_PREVIEW,
        system_instruction=DIGEST_SYSTEM_PROMPT,
        max_output_tokens=4096,
    )
    return result.text


def digest_session(
    project: str,
    session_id: str,
    history_root: Path,
    dry_run: bool = False,
    agent_type: str = "claude",
) -> str | None:
    content = load_session_content(
        project=project, session_id=session_id,
        history_root=history_root, agent_type=agent_type,
    )
    conversation_text = extract_conversation_text(content, max_chars=80000)
    tool_summary = extract_tool_summary(content)

    print(f"\n--- Session: {session_id} ---")
    print(f"  Started: {content.info.started_at}")
    print(f"  Updated: {content.info.updated_at}")
    print(f"  Records: {content.info.record_count}")
    print(f"  User messages: {len(content.user_messages)}")
    print(f"  Assistant messages: {len(content.assistant_messages)}")
    print(f"  Tool uses: {len(content.tool_uses)}")
    print(f"  Compact summaries: {len(content.compact_summaries)}")
    print(f"  Conversation text: {len(conversation_text)} chars")

    if dry_run:
        print("  [DRY RUN] Skipping LLM call")
        return None

    print("  Calling Gemini 3 Flash for digest...")
    digest_text = _call_gemini(conversation_text, tool_summary, content.info)

    # Build output
    date_str = (content.info.started_at or "unknown")[:10]
    header = f"# Session Digest: {date_str}\n\n"
    header += f"- **Session ID:** `{session_id}`\n"
    header += f"- **Started:** {content.info.started_at}\n"
    header += f"- **Ended:** {content.info.updated_at}\n"
    header += f"- **Records:** {content.info.record_count}\n"
    header += f"- **Agent:** {agent_type}\n"
    header += f"- **Project:** {project}\n\n"

    full_digest = header + digest_text

    # Save to docs/digests/
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    safe_date = date_str.replace("-", "")
    filename = f"{safe_date}_{session_id[:8]}.md"
    output_path = DIGEST_DIR / filename
    output_path.write_text(full_digest)
    print(f"  Saved: {output_path}")

    return str(output_path)


def select_sessions(
    project: str,
    history_root: Path,
    recent: int | None = None,
    session_id: str | None = None,
    since: str | None = None,
    agent_type: str = "claude",
) -> list[SessionInfo]:
    if session_id:
        sessions = list_sessions(project, history_root, agent_type=agent_type)
        return [s for s in sessions if s.session_id == session_id]

    sessions = list_sessions(project, history_root, agent_type=agent_type)

    if since:
        sessions = [
            s for s in sessions
            if s.started_at and s.started_at[:10] >= since
        ]

    if recent:
        sessions = sessions[:recent]

    return sessions


def _prepare_session(
    project: str,
    session_id: str,
    history_root: Path,
    agent_type: str = "claude",
) -> tuple[SessionContent, str, str]:
    """Load session and extract text. Returns (content, conversation_text, tool_summary)."""
    content = load_session_content(
        project=project, session_id=session_id,
        history_root=history_root, agent_type=agent_type,
    )
    conversation_text = extract_conversation_text(content, max_chars=80000)
    tool_summary = extract_tool_summary(content)
    return content, conversation_text, tool_summary


def _save_digest(
    session_id: str,
    digest_text: str,
    content: SessionContent,
    agent_type: str,
    project: str,
) -> str:
    """Write digest markdown to docs/digests/ and return the path."""
    date_str = (content.info.started_at or "unknown")[:10]
    header = f"# Session Digest: {date_str}\n\n"
    header += f"- **Session ID:** `{session_id}`\n"
    header += f"- **Started:** {content.info.started_at}\n"
    header += f"- **Ended:** {content.info.updated_at}\n"
    header += f"- **Records:** {content.info.record_count}\n"
    header += f"- **Agent:** {agent_type}\n"
    header += f"- **Project:** {project}\n\n"

    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    safe_date = date_str.replace("-", "")
    filename = f"{safe_date}_{session_id[:8]}.md"
    output_path = DIGEST_DIR / filename
    output_path.write_text(header + digest_text)
    return str(output_path)


def digest_sessions_parallel(
    project: str,
    sessions: list[SessionInfo],
    history_root: Path,
    agent_type: str = "claude",
) -> list[str]:
    """Digest multiple sessions with parallel Gemini calls via batch_generate_texts."""
    from shared.gemini import batch_generate_texts, GeminiRequest, GEMINI_3_FLASH_PREVIEW

    # Phase 1: prepare all sessions sequentially (local I/O, print stats)
    prepared: list[tuple[SessionInfo, SessionContent, str, str]] = []
    for session in sessions:
        content, conv_text, tool_sum = _prepare_session(
            project, session.session_id, history_root, agent_type,
        )
        print(f"\n--- Session: {session.session_id} ---")
        print(f"  Started: {content.info.started_at}")
        print(f"  Updated: {content.info.updated_at}")
        print(f"  Records: {content.info.record_count}")
        print(f"  User messages: {len(content.user_messages)}")
        print(f"  Assistant messages: {len(content.assistant_messages)}")
        print(f"  Tool uses: {len(content.tool_uses)}")
        print(f"  Conversation text: {len(conv_text)} chars")
        prepared.append((session, content, conv_text, tool_sum))

    # Phase 2: parallel Gemini calls
    print(f"\nCalling Gemini 3 Flash for {len(prepared)} sessions in parallel...")

    requests = [
        GeminiRequest(
            contents=_build_user_prompt(conv_text, tool_sum, content.info),
            model=GEMINI_3_FLASH_PREVIEW,
            system_instruction=DIGEST_SYSTEM_PROMPT,
            max_output_tokens=4096,
        )
        for _, content, conv_text, tool_sum in prepared
    ]

    batch_results = batch_generate_texts(requests, max_workers=len(prepared))

    outputs: list[str] = []
    for batch_result in batch_results:
        session, content, _, _ = prepared[batch_result.index]
        if batch_result.error:
            print(f"  FAILED: {session.session_id[:8]}: {batch_result.error}")
        else:
            path = _save_digest(
                session.session_id, batch_result.result.text,
                content, agent_type, project,
            )
            print(f"  Done: {session.session_id[:8]} -> {path}")
            outputs.append(path)

    return outputs


def main():
    parser = argparse.ArgumentParser(description="Digest agent sessions using Gemini")
    parser.add_argument("--project", required=True, help="Claude project directory name")
    parser.add_argument("--session", help="Specific session ID to digest")
    parser.add_argument("--recent", type=int, help="Digest N most recent sessions")
    parser.add_argument("--since", help="Digest sessions since date (YYYY-MM-DD)")
    parser.add_argument("--agent", choices=["claude", "codex", "factory"], default="claude",
                        help="Agent type (claude, codex, factory)")
    parser.add_argument("--root", type=Path, default=None, help="Override history root path")
    parser.add_argument("--dry-run", action="store_true", help="Show info without calling LLM")
    args = parser.parse_args()

    if not args.session and not args.recent and not args.since:
        args.recent = 1

    root = args.root or DEFAULT_ROOTS.get(args.agent)

    _load_env()

    sessions = select_sessions(
        args.project, root,
        recent=args.recent,
        session_id=args.session,
        since=args.since,
        agent_type=args.agent,
    )

    if not sessions:
        print("No sessions found matching criteria.")
        return 1

    print(f"Found {len(sessions)} session(s) to digest")

    if len(sessions) <= 1 or args.dry_run:
        outputs = []
        for session in sessions:
            path = digest_session(
                args.project, session.session_id, root,
                dry_run=args.dry_run,
                agent_type=args.agent,
            )
            if path:
                outputs.append(path)
    else:
        outputs = digest_sessions_parallel(
            project=args.project,
            sessions=sessions,
            history_root=root,
            agent_type=args.agent,
        )

    if outputs:
        print(f"\nDigests saved: {len(outputs)}")
        for p in outputs:
            print(f"  {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
