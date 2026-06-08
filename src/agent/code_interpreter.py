"""Thin wrapper over `strands_tools.code_interpreter.AgentCoreCodeInterpreter`.

Mirrors the lifecycle pattern in awsstrat/heurist_finance_agent (init session,
write the chart_style helper as a file, execute Python, extract image bytes
back over the wire). Adapted for the chart agent: returns base64 image bytes
to the caller instead of writing them to a local artifacts directory.

The Strands `code_interpreter` callable speaks JSON action payloads:
  - {"action": {"type": "initSession", "session_name": "...", "description": "..."}}
  - {"action": {"type": "writeFiles", "session_name": "...", "content": [{"path": "...", "text": "..."}]}}
  - {"action": {"type": "executeCode", "session_name": "...", "language": "python", "code": "..."}}
  - {"action": {"type": "listFiles", "session_name": "...", "path": "."}}
"""

from __future__ import annotations

import ast
import base64
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from strands_tools.code_interpreter import AgentCoreCodeInterpreter


CHART_STYLE_PATH = Path(__file__).resolve().parent / "chart_style.py"


@dataclass(slots=True)
class ImagePayload:
    image_b64: str
    mime: str
    name: str
    size_bytes: int


def make_session(*, region: str, session_name: str) -> AgentCoreCodeInterpreter:
    """Construct an AgentCoreCodeInterpreter bound to a region + session name.

    The Strands wrapper is lightweight — `initSession` is the lifecycle event
    that actually allocates the sandbox.
    """
    return AgentCoreCodeInterpreter(region=region, session_name=session_name)


def init_session(ci: AgentCoreCodeInterpreter, session_name: str, *, description: str = "chart agent session") -> dict[str, Any]:
    return ci.code_interpreter(
        {
            "action": {
                "type": "initSession",
                "session_name": session_name,
                "description": description,
            }
        }
    )


def write_chart_style(ci: AgentCoreCodeInterpreter, session_name: str) -> dict[str, Any]:
    """Push the local `chart_style.py` into the sandbox so generated code can
    `from chart_style import apply_style, finalize_figure`."""
    text = CHART_STYLE_PATH.read_text(encoding="utf-8")
    return ci.code_interpreter(
        {
            "action": {
                "type": "writeFiles",
                "session_name": session_name,
                "content": [{"path": "chart_style.py", "text": text}],
            }
        }
    )


def execute_code(ci: AgentCoreCodeInterpreter, session_name: str, code: str) -> dict[str, Any]:
    return ci.code_interpreter(
        {
            "action": {
                "type": "executeCode",
                "session_name": session_name,
                "language": "python",
                "code": code,
            }
        }
    )


def _extract_text_payload(tool_result: dict[str, Any]) -> str:
    """Adapted from awsstrat/heurist_finance_agent/artifact_export.py."""
    content = tool_result.get("content", [])
    if not content:
        raise ValueError(f"Missing tool content: {tool_result}")
    text_blob = content[0].get("text")
    if not text_blob:
        raise ValueError(f"Missing text payload: {tool_result}")
    parsed = ast.literal_eval(text_blob)
    if not parsed or "text" not in parsed[0]:
        raise ValueError(f"Unexpected tool payload: {tool_result}")
    return parsed[0]["text"]


def _extract_json_payload(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("Empty payload text")
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    try:
        parsed = ast.literal_eval(stripped)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    for line in reversed([line.strip() for line in stripped.splitlines() if line.strip()]):
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(line)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                continue
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = stripped[start : end + 1]
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            parsed = ast.literal_eval(candidate)
            if isinstance(parsed, dict):
                return parsed
    raise ValueError(f"Could not parse JSON payload from text: {text[:500]}")


def fetch_image(ci: AgentCoreCodeInterpreter, session_name: str, remote_path: str) -> ImagePayload:
    """Read a remote file inside the sandbox, base64-encode, return to caller."""
    code = f"""
import base64, json, mimetypes
from pathlib import Path
p = Path({remote_path!r})
if not p.exists():
    raise FileNotFoundError(str(p))
print(json.dumps({{
    "name": p.name,
    "mime_type": mimetypes.guess_type(str(p))[0] or "application/octet-stream",
    "base64": base64.b64encode(p.read_bytes()).decode(),
}}))
"""
    raw = execute_code(ci, session_name, code)
    payload_text = _extract_text_payload(raw)
    payload = _extract_json_payload(payload_text)
    image_b64 = payload["base64"]
    name = payload.get("name") or "chart.png"
    mime = payload.get("mime_type") or mimetypes.guess_type(name)[0] or "image/png"
    size = len(base64.b64decode(image_b64))
    return ImagePayload(image_b64=image_b64, mime=mime, name=name, size_bytes=size)


def terminate(ci: AgentCoreCodeInterpreter, session_name: str) -> None:
    """Best-effort cleanup. Strands' wrapper does not expose a terminate verb
    in the public action set; sessions are reaped by AgentCore on idle."""
    # Intentionally a no-op: AgentCore reaps idle sessions; the SDK does not
    # expose a teardown action. Left as an explicit hook in case Strands adds
    # one later.
    return None


__all__ = [
    "CHART_STYLE_PATH",
    "ImagePayload",
    "execute_code",
    "fetch_image",
    "init_session",
    "make_session",
    "terminate",
    "write_chart_style",
]
