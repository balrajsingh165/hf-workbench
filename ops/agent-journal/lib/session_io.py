"""Unified session I/O for Claude CLI, Codex CLI, and Factory Droid sessions.

Used by agent-journal tooling.

Normalizes all three JSONL schemas into a common SessionContent structure.

Schema differences:
- Claude:  ~/.claude/projects/<project>/<session-uuid>.jsonl
           Records: type=user|assistant|system|progress with message.content[]
- Codex:   ~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl
           Records: type=session_meta|response_item|event_msg|turn_context
           Messages in response_item.payload with role/content
- Factory: ~/.factory/sessions/<project>/<session-uuid>.jsonl
           Records: type=session_start|message|todo_state
           Messages in message.role/content (same structure as Claude)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default history roots for each agent type
CLAUDE_ROOT = Path.home() / ".claude" / "projects"
CODEX_ROOT = Path.home() / ".codex" / "sessions"
FACTORY_ROOT = Path.home() / ".factory" / "sessions"

TAG_PATTERN = re.compile(r"</?[^>]+>")

# Patterns that indicate a user message is tool output noise, not human speech
_NOISE_PATTERNS = [
    re.compile(r"^\d+[\s:]+\S"),  # line-numbered content (code dumps, file reads)
    re.compile(r"^[A-Fa-f0-9]{16,}\s+toolu_"),  # subagent task IDs
    re.compile(r'^{"'),  # raw JSON records
    re.compile(r"^(Command completed|Process exited|Error:)"),  # tool result boilerplate
    re.compile(r"^\?\?\s+"),  # git status untracked lines
    re.compile(r"^(TODO List Updated|Spec mode is active)"),
    re.compile(r"^User has approved your plan"),
    re.compile(r"^User system info"),  # system reminder preamble
    re.compile(r"^(Warning:|Traceback|fatal:)"),  # error output
    re.compile(r"^[A-Z][a-z]+: .+\.(py|sh|md|jsonl|json)"),  # file path listings
    re.compile(r"^[\w/.-]+\.(py|sh|md|jsonl|json):\d+:"),  # grep-style file:line output
    re.compile(r"^(total \d|drwx|lrwx|-rw)"),  # ls output
    re.compile(r"^(diff --git|@@\s)"),  # git diff output
]


def is_noise_message(text: str) -> bool:
    """Return True if the message is likely tool output or boilerplate, not human speech."""
    if not text or len(text) < 5:
        return True
    for pattern in _NOISE_PATTERNS:
        if pattern.search(text[:200]):
            return True
    # High ratio of digits/punctuation to letters suggests tool dump
    alpha = sum(1 for c in text[:300] if c.isalpha())
    if len(text) > 50 and alpha < len(text[:300]) * 0.3:
        return True
    return False

AGENT_TYPES = ("claude", "codex", "factory")

# Distinct single-char tags to avoid ambiguity (claude and codex both start with C)
AGENT_TAGS: dict[str, str] = {
    "claude": "C",
    "codex": "X",
    "factory": "F",
}


def agent_tag(agent_type: str) -> str:
    """Return the single-char display tag for an agent type."""
    return AGENT_TAGS.get(agent_type, agent_type[0].upper())

# Map of agent type -> default root
DEFAULT_ROOTS: dict[str, Path] = {
    "claude": CLAUDE_ROOT,
    "codex": CODEX_ROOT,
    "factory": FACTORY_ROOT,
}


def safe_json_parse(line: str) -> dict[str, Any] | None:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def read_jsonl_records(file_path: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with file_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = safe_json_parse(line)
            if record is not None:
                records.append(record)
    return records


def content_items_to_text(items: Any) -> str:
    if isinstance(items, str):
        return items
    if not isinstance(items, list):
        return ""
    chunks: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        for key in ("text", "thinking", "content", "input_text"):
            value = item.get(key)
            if isinstance(value, str) and value:
                chunks.append(value)
                break
    return "\n\n".join(chunks)


def normalize_prompt_text(text: str) -> str:
    cleaned = TAG_PATTERN.sub(" ", text)
    cleaned = " ".join(cleaned.split())
    if cleaned.startswith("Caveat: The messages below"):
        return ""
    if cleaned.startswith("Base directory for this skill:"):
        return ""
    return cleaned


@dataclass
class SessionInfo:
    project: str
    session_id: str
    file_path: Path
    started_at: str | None
    updated_at: str | None
    record_count: int
    title: str
    agent_type: str = "claude"


@dataclass
class SessionContent:
    info: SessionInfo
    user_messages: list[dict[str, Any]]
    assistant_messages: list[dict[str, Any]]
    tool_uses: list[dict[str, Any]]
    tool_results: list[dict[str, Any]]
    thinking_blocks: list[dict[str, Any]]
    compact_summaries: list[str]


# ---------------------------------------------------------------------------
# Agent-type detection
# ---------------------------------------------------------------------------

def detect_agent_type(file_path: Path) -> str:
    """Detect agent type from file path or first record."""
    path_str = str(file_path)
    if "/.codex/" in path_str:
        return "codex"
    if "/.factory/" in path_str:
        return "factory"
    if "/.claude/" in path_str:
        return "claude"
    # Fallback: check first record
    records = read_jsonl_records(file_path)
    if records:
        first = records[0]
        if first.get("type") == "session_meta" and "codex" in str(first.get("payload", {}).get("originator", "")):
            return "codex"
        if first.get("type") == "session_start":
            return "factory"
    return "claude"


# ---------------------------------------------------------------------------
# Session listing (per agent type)
# ---------------------------------------------------------------------------

def _list_claude_sessions(project: str, root: Path) -> list[SessionInfo]:
    project_path = root / project
    if not project_path.is_dir():
        return []
    sessions = []
    for f in sorted(project_path.iterdir(), key=lambda p: p.name, reverse=True):
        if not f.is_file() or f.suffix != ".jsonl":
            continue
        records = read_jsonl_records(f)
        timestamps = sorted(
            ts for ts in (r.get("timestamp") for r in records)
            if isinstance(ts, str) and ts
        )
        title = _get_claude_title(records, f.stem)
        sessions.append(SessionInfo(
            project=project, session_id=f.stem, file_path=f,
            started_at=timestamps[0] if timestamps else None,
            updated_at=timestamps[-1] if timestamps else None,
            record_count=len(records), title=title, agent_type="claude",
        ))
    return sessions


def _get_claude_title(records: list[dict[str, Any]], fallback: str) -> str:
    for record in records:
        if record.get("isMeta"):
            continue
        if record.get("type") == "user" and isinstance(record.get("message"), dict):
            text = normalize_prompt_text(
                content_items_to_text(record["message"].get("content"))
            )
            if text and not record.get("isCompactSummary"):
                return text[:120]
    return fallback


def _list_factory_sessions(project: str, root: Path) -> list[SessionInfo]:
    project_path = root / project
    if not project_path.is_dir():
        return []
    sessions = []
    for f in sorted(project_path.iterdir(), key=lambda p: p.name, reverse=True):
        if not f.is_file() or f.suffix != ".jsonl":
            continue
        records = read_jsonl_records(f)
        timestamps = sorted(
            ts for ts in (r.get("timestamp") for r in records)
            if isinstance(ts, str) and ts
        )
        title = _get_factory_title(records, f.stem)
        sessions.append(SessionInfo(
            project=project, session_id=f.stem, file_path=f,
            started_at=timestamps[0] if timestamps else None,
            updated_at=timestamps[-1] if timestamps else None,
            record_count=len(records), title=title, agent_type="factory",
        ))
    return sessions


def _get_factory_title(records: list[dict[str, Any]], fallback: str) -> str:
    for record in records:
        if record.get("type") == "session_start":
            title = record.get("sessionTitle") or record.get("title", "")
            if title:
                return title[:120]
    # Fallback to first user message
    for record in records:
        if record.get("type") == "message":
            msg = record.get("message", {})
            if msg.get("role") == "user":
                text = normalize_prompt_text(content_items_to_text(msg.get("content")))
                if text:
                    return text[:120]
    return fallback


def _list_codex_sessions(root: Path) -> list[SessionInfo]:
    """List all Codex sessions across date directories."""
    sessions = []
    if not root.is_dir():
        return sessions
    # Walk YYYY/MM/DD structure
    for year_dir in sorted(root.iterdir(), reverse=True):
        if not year_dir.is_dir():
            continue
        for month_dir in sorted(year_dir.iterdir(), reverse=True):
            if not month_dir.is_dir():
                continue
            for day_dir in sorted(month_dir.iterdir(), reverse=True):
                if not day_dir.is_dir():
                    continue
                for f in sorted(day_dir.iterdir(), key=lambda p: p.name, reverse=True):
                    if not f.is_file() or f.suffix != ".jsonl":
                        continue
                    records = read_jsonl_records(f)
                    timestamps = sorted(
                        ts for ts in (r.get("timestamp") for r in records)
                        if isinstance(ts, str) and ts
                    )
                    title = _get_codex_title(records, f.stem)
                    session_id = _get_codex_session_id(records, f.stem)
                    sessions.append(SessionInfo(
                        project="codex",
                        session_id=session_id,
                        file_path=f,
                        started_at=timestamps[0] if timestamps else None,
                        updated_at=timestamps[-1] if timestamps else None,
                        record_count=len(records),
                        title=title,
                        agent_type="codex",
                    ))
    return sessions


def _get_codex_session_id(records: list[dict[str, Any]], fallback: str) -> str:
    for r in records:
        if r.get("type") == "session_meta":
            sid = r.get("payload", {}).get("id", "")
            if sid:
                return sid
    return fallback


def _get_codex_title(records: list[dict[str, Any]], fallback: str) -> str:
    # Skip the first user message (typically AGENTS.md / system instructions)
    user_msg_count = 0
    for r in records:
        if r.get("type") != "response_item":
            continue
        p = r.get("payload", {})
        if p.get("role") == "user" and p.get("type") == "message":
            user_msg_count += 1
            if user_msg_count <= 1:
                continue
            text = content_items_to_text(p.get("content", []))
            text = normalize_prompt_text(text)
            if text:
                return text[:120]
    # Fallback: use agent_message commentary from event_msg
    for r in records:
        if r.get("type") == "event_msg":
            p = r.get("payload", {})
            if p.get("type") == "agent_message":
                msg = p.get("message", "")
                if msg:
                    return msg[:120]
    return fallback


def list_sessions(
    project: str | None = None,
    history_root: Path | None = None,
    limit: int | None = None,
    agent_type: str = "claude",
) -> list[SessionInfo]:
    """List sessions for a given agent type.

    For claude/factory: project is required.
    For codex: project is ignored (sessions are date-organized).
    """
    root = history_root or DEFAULT_ROOTS.get(agent_type, CLAUDE_ROOT)

    if agent_type == "codex":
        sessions = _list_codex_sessions(root)
    elif agent_type == "factory":
        sessions = _list_factory_sessions(project or "", root)
    else:
        sessions = _list_claude_sessions(project or "", root)

    sessions.sort(key=lambda s: s.updated_at or "", reverse=True)
    if limit:
        sessions = sessions[:limit]
    return sessions


def list_all_sessions(
    limit_per_type: int | None = None,
) -> list[SessionInfo]:
    """List sessions across all agent types, merged by time."""
    all_sessions: list[SessionInfo] = []
    for agent_type in AGENT_TYPES:
        root = DEFAULT_ROOTS[agent_type]
        if not root.exists():
            continue
        if agent_type in ("claude", "factory"):
            # List all projects
            for proj_dir in root.iterdir():
                if proj_dir.is_dir():
                    sessions = list_sessions(
                        project=proj_dir.name,
                        agent_type=agent_type,
                        limit=limit_per_type,
                    )
                    all_sessions.extend(sessions)
        else:
            sessions = list_sessions(agent_type=agent_type, limit=limit_per_type)
            all_sessions.extend(sessions)

    all_sessions.sort(key=lambda s: s.updated_at or "", reverse=True)
    return all_sessions


# ---------------------------------------------------------------------------
# Session resolution by prefix
# ---------------------------------------------------------------------------

def resolve_session_by_prefix(prefix: str) -> SessionInfo | None:
    """Find a session by short ID prefix across all agent types.

    Scans Claude, Codex, and Factory roots. Returns the first match,
    or None if no session matches the prefix.
    """
    all_sessions = list_all_sessions(limit_per_type=200)
    for s in all_sessions:
        if s.session_id.startswith(prefix):
            return s
    return None


# ---------------------------------------------------------------------------
# Session content loading (per agent type)
# ---------------------------------------------------------------------------

def _load_claude_or_factory_content(
    file_path: Path, session_id: str, project: str, agent_type: str,
) -> SessionContent:
    records = read_jsonl_records(file_path)
    timestamps = sorted(
        ts for ts in (r.get("timestamp") for r in records)
        if isinstance(ts, str) and ts
    )

    if agent_type == "factory":
        title = _get_factory_title(records, session_id)
    else:
        title = _get_claude_title(records, session_id)

    info = SessionInfo(
        project=project, session_id=session_id, file_path=file_path,
        started_at=timestamps[0] if timestamps else None,
        updated_at=timestamps[-1] if timestamps else None,
        record_count=len(records), title=title, agent_type=agent_type,
    )

    user_msgs: list[dict[str, Any]] = []
    assistant_msgs: list[dict[str, Any]] = []
    tool_uses: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    thinking_blocks: list[dict[str, Any]] = []
    compact_summaries: list[str] = []
    tool_names: dict[str, str] = {}

    for record in records:
        rtype = record.get("type")
        ts = record.get("timestamp", "")

        # Factory uses type=message wrapping role-based messages
        if rtype == "message" and agent_type == "factory":
            msg = record.get("message", {})
            role = msg.get("role", "")
            content = msg.get("content")
            _extract_role_content(
                role, content, ts, record,
                user_msgs, assistant_msgs, tool_uses, tool_results,
                thinking_blocks, compact_summaries, tool_names,
            )
            continue

        # Claude uses type=user / type=assistant directly
        if rtype in ("user", "assistant"):
            message = record.get("message") or {}
            content = message.get("content")
            role = rtype
            is_compact = record.get("isCompactSummary", False)
            if is_compact and role == "user":
                text = content_items_to_text(content)
                if text:
                    compact_summaries.append(text)
                continue
            _extract_role_content(
                role, content, ts, record,
                user_msgs, assistant_msgs, tool_uses, tool_results,
                thinking_blocks, compact_summaries, tool_names,
            )

    return SessionContent(
        info=info,
        user_messages=user_msgs,
        assistant_messages=assistant_msgs,
        tool_uses=tool_uses,
        tool_results=tool_results,
        thinking_blocks=thinking_blocks,
        compact_summaries=compact_summaries,
    )


def _extract_role_content(
    role: str,
    content: Any,
    ts: str,
    record: dict[str, Any],
    user_msgs: list,
    assistant_msgs: list,
    tool_uses: list,
    tool_results: list,
    thinking_blocks: list,
    compact_summaries: list,
    tool_names: dict,
) -> None:
    """Extract messages, tool calls, and thinking from role-based content."""
    if role == "user":
        text = content_items_to_text(content)
        text = normalize_prompt_text(text) if text else ""
        if text:
            user_msgs.append({"timestamp": ts, "text": text})
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict) and item.get("type") == "tool_result":
                    tool_use_id = item.get("tool_use_id", "")
                    result_text = item.get("content", "")
                    if isinstance(result_text, list):
                        result_text = content_items_to_text(result_text)
                    tool_results.append({
                        "timestamp": ts,
                        "tool_name": tool_names.get(tool_use_id, "unknown"),
                        "tool_use_id": tool_use_id,
                        "text": str(result_text)[:500],
                        "is_error": bool(item.get("is_error")),
                    })

    elif role == "assistant":
        if isinstance(content, str):
            assistant_msgs.append({"timestamp": ts, "text": content})
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                itype = item.get("type", "")
                if itype == "text":
                    assistant_msgs.append({"timestamp": ts, "text": item.get("text", "")})
                elif itype == "thinking":
                    thinking_blocks.append({"timestamp": ts, "text": item.get("thinking", "")})
                elif itype == "tool_use":
                    tool_use_id = item.get("id", "")
                    tool_name = item.get("name", "unknown")
                    tool_names[tool_use_id] = tool_name
                    tool_input = item.get("input", {})
                    tool_uses.append({
                        "timestamp": ts,
                        "tool_name": tool_name,
                        "tool_use_id": tool_use_id,
                        "input_preview": json.dumps(tool_input, ensure_ascii=False)[:300],
                    })


def _load_codex_content(
    file_path: Path, session_id: str,
) -> SessionContent:
    records = read_jsonl_records(file_path)
    timestamps = sorted(
        ts for ts in (r.get("timestamp") for r in records)
        if isinstance(ts, str) and ts
    )
    title = _get_codex_title(records, session_id)

    info = SessionInfo(
        project="codex", session_id=session_id, file_path=file_path,
        started_at=timestamps[0] if timestamps else None,
        updated_at=timestamps[-1] if timestamps else None,
        record_count=len(records), title=title, agent_type="codex",
    )

    user_msgs: list[dict[str, Any]] = []
    assistant_msgs: list[dict[str, Any]] = []
    tool_uses: list[dict[str, Any]] = []
    tool_results: list[dict[str, Any]] = []
    thinking_blocks: list[dict[str, Any]] = []

    for record in records:
        rtype = record.get("type")
        ts = record.get("timestamp", "")

        if rtype != "response_item":
            continue

        payload = record.get("payload", {})
        ptype = payload.get("type", "")
        role = payload.get("role", "")

        if ptype == "message":
            content = payload.get("content", [])
            text = content_items_to_text(content)
            text = normalize_prompt_text(text) if text else ""
            if not text:
                continue
            if role == "user":
                user_msgs.append({"timestamp": ts, "text": text})
            elif role == "assistant":
                assistant_msgs.append({"timestamp": ts, "text": text})

        elif ptype == "reasoning":
            # Codex reasoning (like thinking)
            summary = ""
            for s in payload.get("summary", []):
                if isinstance(s, dict) and s.get("type") == "summary_text":
                    summary += s.get("text", "")
            if summary:
                thinking_blocks.append({"timestamp": ts, "text": summary})

        elif ptype == "function_call":
            tool_name = payload.get("name", "unknown")
            call_id = payload.get("call_id", "")
            arguments = payload.get("arguments", "")
            tool_uses.append({
                "timestamp": ts,
                "tool_name": tool_name,
                "tool_use_id": call_id,
                "input_preview": arguments[:300],
            })

        elif ptype == "function_call_output":
            call_id = payload.get("call_id", "")
            output = payload.get("output", "")
            tool_results.append({
                "timestamp": ts,
                "tool_name": "unknown",
                "tool_use_id": call_id,
                "text": str(output)[:500],
                "is_error": False,
            })

    return SessionContent(
        info=info,
        user_messages=user_msgs,
        assistant_messages=assistant_msgs,
        tool_uses=tool_uses,
        tool_results=tool_results,
        thinking_blocks=thinking_blocks,
        compact_summaries=[],
    )


def load_session_content(
    project: str | None = None,
    session_id: str = "",
    history_root: Path | None = None,
    agent_type: str | None = None,
    file_path: Path | None = None,
) -> SessionContent:
    """Load and normalize session content from any agent type.

    Can be called with explicit file_path (auto-detects agent type),
    or with project + session_id + agent_type.
    """
    if file_path:
        detected = agent_type or detect_agent_type(file_path)
        sid = session_id or file_path.stem
        if detected == "codex":
            return _load_codex_content(file_path, sid)
        proj = project or file_path.parent.name
        return _load_claude_or_factory_content(file_path, sid, proj, detected)

    atype = agent_type or "claude"
    root = history_root or DEFAULT_ROOTS.get(atype, CLAUDE_ROOT)

    if atype == "codex":
        # For codex, find the file by session_id in date dirs
        for year_dir in sorted(root.iterdir(), reverse=True):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir(), reverse=True):
                if not month_dir.is_dir():
                    continue
                for day_dir in sorted(month_dir.iterdir(), reverse=True):
                    if not day_dir.is_dir():
                        continue
                    for f in day_dir.iterdir():
                        if f.is_file() and f.suffix == ".jsonl" and session_id in f.name:
                            return _load_codex_content(f, session_id)
        raise FileNotFoundError(f"Codex session {session_id} not found under {root}")

    fpath = root / (project or "") / f"{session_id}.jsonl"
    return _load_claude_or_factory_content(fpath, session_id, project or "", atype)


# ---------------------------------------------------------------------------
# Output helpers (shared across tools)
# ---------------------------------------------------------------------------

def extract_conversation_text(
    content: SessionContent,
    max_chars: int = 80000,
    include_thinking: bool = False,
) -> str:
    """Build a chronological conversation transcript for LLM consumption."""
    events: list[tuple[str, str, str]] = []

    for msg in content.user_messages:
        events.append((msg["timestamp"], "USER", msg["text"]))
    for msg in content.assistant_messages:
        events.append((msg["timestamp"], "ASSISTANT", msg["text"]))
    if include_thinking:
        for block in content.thinking_blocks:
            events.append((block["timestamp"], "THINKING", block["text"]))

    events.sort(key=lambda e: e[0])

    lines: list[str] = []
    total = 0
    for ts, role, text in events:
        text_trunc = text[:3000] if len(text) > 3000 else text
        line = f"[{ts[:19]}] {role}: {text_trunc}"
        if total + len(line) > max_chars:
            lines.append(f"\n... [truncated at {max_chars} chars] ...")
            break
        lines.append(line)
        total += len(line)

    return "\n\n".join(lines)


def extract_tool_summary(content: SessionContent) -> str:
    """Summarize tools used in the session."""
    from collections import Counter
    tool_counts = Counter(tu["tool_name"] for tu in content.tool_uses)
    error_count = sum(1 for tr in content.tool_results if tr.get("is_error"))
    lines = [f"Tools used (agent: {content.info.agent_type}):"]
    for tool, count in tool_counts.most_common(15):
        lines.append(f"  {tool}: {count}")
    lines.append(f"Errors: {error_count}")
    return "\n".join(lines)
