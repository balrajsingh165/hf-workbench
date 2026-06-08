"""Exercise every Strands tool the chat agent can call and grade the output.

The four tools wired into the Sage research phase live in `src/agent/tools.py`
(`HF_TOOLS`). They dispatch to FastAPI handlers in `app.py`. This script calls
them in-process — same code path the agent hits at runtime — across a small
matrix of inputs and prints a quality report.

Run:
    uv run python scripts/smoke_agent_tools.py
    uv run python scripts/smoke_agent_tools.py --json > tool_audit.json

Grading dimensions per call:
  ok            — call returned without raising
  size_chars    — JSON length of the returned payload (relative cost)
  latency_ms    — wall time of the dispatch
  signal_count  — count of the headline list-shaped field (evidence items,
                  series rows, bars, etc.) — what the model actually consumes
  note          — recovery note attached by the handler when result is empty
  flags         — qualitative pass/fail markers (see _flag_call)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from src.agent.tools import HF_TOOLS, _dispatch  # type: ignore[import-not-found]


CASES: list[dict[str, Any]] = [
    # ── search_evidence ──────────────────────────────────────────────────
    {
        "tool": "search_evidence",
        "label": "thesis_001 — dense evidence, no filter",
        "args": {"thesis_id": "thesis_001"},
    },
    {
        "tool": "search_evidence",
        "label": "thesis_001 — stresses only",
        "args": {"thesis_id": "thesis_001", "direction": "stresses"},
    },
    {
        "tool": "search_evidence",
        "label": "thesis_002 — thin evidence (1 story)",
        "args": {"thesis_id": "thesis_002"},
    },
    {
        "tool": "search_evidence",
        "label": "thesis_999 — bogus id",
        "args": {"thesis_id": "thesis_999"},
    },
    # ── price_summary / price_history ────────────────────────────────────
    {
        "tool": "price_summary",
        "label": "SPY summary (default window)",
        "args": {"ticker": "SPY"},
    },
    {
        "tool": "price_summary",
        "label": "ROK summary 90d",
        "args": {"ticker": "ROK", "days_back": 90},
    },
    {
        "tool": "price_summary",
        "label": "ZZZZ summary (bogus)",
        "args": {"ticker": "ZZZZ"},
    },
    {
        "tool": "price_history",
        "label": "SPY history 1mo",
        "args": {"ticker": "SPY"},
    },
    {
        "tool": "price_history",
        "label": "SPY history 90d",
        "args": {"ticker": "SPY", "days_back": 90},
    },
    # ── recent_filings / recent_insider ──────────────────────────────────
    {
        "tool": "recent_filings",
        "label": "XOM filings (flat)",
        "args": {"ticker": "XOM"},
    },
    {
        "tool": "recent_insider",
        "label": "XOM insider (flat per-transaction)",
        "args": {"ticker": "XOM"},
    },
    # ── search_macro ─────────────────────────────────────────────────────
    {
        "tool": "search_macro",
        "label": "macro histories for Fed-pivot thesis",
        "args": {
            "series": [
                {"series_key": "core_cpi", "view": "yoy"},
                {"series_key": "core_cpi", "view": "mom_annualized"},
                {"series_key": "ust_10y", "view": "level"},
            ],
            "limit": 12,
        },
    },
    {
        "tool": "search_macro",
        "label": "empty args rejected",
        "args": {},
    },
    # ── get_related_theses ───────────────────────────────────────────────
    {
        "tool": "get_related_theses",
        "label": "thesis_001 related (user_1)",
        "args": {"thesis_id": "thesis_001"},
    },
    {
        "tool": "get_related_theses",
        "label": "thesis_003 (not owned by user_1)",
        "args": {"thesis_id": "thesis_003"},
    },
]


# Per-tool list field whose length is the "signal count" — the items the
# response phase actually paraphrases. Hand-picked from each handler's shape.
SIGNAL_FIELDS = {
    "search_evidence": ("evidence",),
    "price_summary": (),          # scalar response; signal is field presence
    "price_history": ("bars",),
    "recent_filings": ("filings",),
    "recent_insider": ("transactions",),
    "search_macro": ("series",),
    "get_related_theses": ("related",),
}


def _first_list_count(payload: Any, candidates: tuple[str, ...]) -> int:
    """Walk one level deep for the first list-shaped field in `candidates`."""
    if not isinstance(payload, dict):
        return 0
    for key in candidates:
        v = payload.get(key)
        if isinstance(v, list):
            return len(v)
    # market handler nests under `snapshot.results[*].data.bars`
    snap = payload.get("snapshot")
    if isinstance(snap, dict):
        results = snap.get("results")
        if isinstance(results, list) and results:
            data = results[0].get("data") if isinstance(results[0], dict) else None
            if isinstance(data, dict):
                bars = data.get("bars")
                if isinstance(bars, list):
                    return len(bars)
    return 0


def _flag_call(tool: str, args: dict[str, Any], payload: Any,
               size: int, latency_ms: float, signal: int) -> list[str]:
    """Apply structural quality checks per tool."""
    flags: list[str] = []
    if not isinstance(payload, dict):
        flags.append("non-dict-payload")
        return flags

    note = payload.get("note")

    # Universal
    if size > 12_000:
        flags.append(f"oversize:{size}")
    if latency_ms > 5_000:
        flags.append(f"slow:{int(latency_ms)}ms")

    if tool == "search_evidence":
        if signal == 0 and not note:
            flags.append("empty-without-note")
        if signal == 0 and note and not note.lower().startswith("no "):
            flags.append("empty-note-not-leading-with-no")
        if args.get("thesis_id", "").endswith("999"):
            note_text = str(note or "").lower()
            if "does not exist" not in note_text and "not found" not in note_text:
                flags.append("missing-thesis-not-diagnosed")
        # invalidation_watch_list must always be present (possibly empty).
        watch_list = payload.get("invalidation_watch_list")
        if not isinstance(watch_list, dict):
            flags.append("missing-invalidation-watch-list")
            watch_list = {}
        # Verify the new summary/total_links surface lands.
        if signal > 0:
            if "summary" not in payload or "total_links" not in payload:
                flags.append("missing-summary-fields")
            for item in payload.get("evidence") or []:
                if isinstance(item, dict) and len(item.get("rationale", "")) > 141:
                    flags.append("rationale-not-trimmed")
                    break
        if signal:
            # Subtract the per-thesis constant overhead (invalidation_watch_list +
            # summary + note scaffolding) before dividing by item count, so
            # single-item responses aren't unfairly flagged.
            framing_cost = len(str(watch_list.get("framing") or ""))
            triggers_cost = sum(
                len(t) + 6 for t in (watch_list.get("conditions") or [])
            )
            adjusted = max(size - framing_cost - triggers_cost - 80, 0)
            per = adjusted // signal
            # Each item is story_id + headline + created_at + relation +
            # confidence + 140-char rationale + JSON scaffolding — ~380
            # chars is the floor after the truncation pass.
            if per > 450:
                flags.append(f"verbose-per-item:{per}")

    if tool == "price_summary":
        if size > 1_400:
            flags.append(f"oversize-summary:{size}")
        if "bars" in payload:
            flags.append("summary-leaking-bars")
        if (
            args.get("ticker", "").upper() != "ZZZZ"
            and payload.get("latest_close") is None
            and not note
        ):
            flags.append("missing-latest-close")

    if tool == "price_history":
        if signal < 5 and not note:
            flags.append(f"sparse-bars:{signal}")
        if signal >= 22 and size > 6_000:
            flags.append("history-bulky")

    if tool == "recent_filings":
        if signal > 0:
            first = (payload.get("filings") or [{}])[0]
            if not all(k in first for k in ("form", "filing_date")):
                flags.append("filings-not-flat")

    if tool == "recent_insider":
        if signal > 0:
            first = (payload.get("transactions") or [{}])[0]
            if not all(k in first for k in ("filing_date", "reporting_person")):
                flags.append("insider-not-flat")

    if tool == "search_macro":
        if not args:
            if "requires non-empty series" not in (note or ""):
                flags.append("empty-args-not-rejected")
            return flags
        # Macro series must be temporal windows so the chart agent can render
        # non-price macro charts.
        if signal:
            sample = payload.get("series", [{}])[0]
            if isinstance(sample, dict):
                observations = sample.get("observations")
                if not isinstance(observations, list) or len(observations) < 5:
                    flags.append("series-history-too-sparse")

    if tool == "get_related_theses":
        if signal == 0 and not note:
            # the handler raises 404 for missing source; otherwise empty is silent
            flags.append("empty-related-silent")
        if signal > 0:
            first = (payload.get("related") or [{}])[0]
            if "relation" not in first:
                flags.append("missing-relation-field")
            elif first.get("relation") not in {
                "related_to_statement",
                "related_to_invalidation",
            }:
                flags.append(f"bad-relation-value:{first.get('relation')}")

    return flags


def _excerpt(payload: Any, max_chars: int = 240) -> str:
    txt = json.dumps(payload, default=str)
    return txt if len(txt) <= max_chars else txt[:max_chars] + "…"


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    tool = case["tool"]
    args = case["args"]
    t0 = time.monotonic()
    err: str | None = None
    payload: Any
    try:
        payload = _dispatch(tool, args, user_id="user_1")
    except Exception as exc:  # noqa: BLE001 — boundary; report it
        payload = {"_error": f"{type(exc).__name__}: {exc}"}
        err = str(exc)
    latency_ms = (time.monotonic() - t0) * 1_000

    size = len(json.dumps(payload, default=str))
    signal = _first_list_count(payload, SIGNAL_FIELDS.get(tool, ()))
    note = payload.get("note") if isinstance(payload, dict) else None
    flags = _flag_call(tool, args, payload, size, latency_ms, signal)

    return {
        "tool": tool,
        "label": case["label"],
        "args": args,
        "ok": err is None,
        "error": err,
        "size_chars": size,
        "latency_ms": round(latency_ms, 1),
        "signal_count": signal,
        "note": note,
        "flags": flags,
        "excerpt": _excerpt(payload),
    }


def print_human(results: list[dict[str, Any]]) -> None:
    by_tool: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_tool.setdefault(r["tool"], []).append(r)

    for tool in (td.tool_name for td in HF_TOOLS):
        rows = by_tool.get(tool, [])
        print(f"\n=== {tool} ({len(rows)} calls) ===")
        for r in rows:
            head = f"  [{ 'ok' if r['ok'] else 'ERR'}] {r['label']}"
            print(head)
            print(f"    args         : {r['args']}")
            print(f"    size_chars   : {r['size_chars']:>6}   "
                  f"latency: {r['latency_ms']:>6} ms   "
                  f"signal: {r['signal_count']}")
            if r["note"]:
                print(f"    note         : {r['note']}")
            if r["flags"]:
                print(f"    FLAGS        : {', '.join(r['flags'])}")
            if r["error"]:
                print(f"    error        : {r['error']}")
            print(f"    excerpt      : {r['excerpt']}")

    # Aggregate per-tool roll-up
    print("\n=== Aggregate roll-up ===")
    print(f"{'tool':<22}{'calls':>6}{'avg_ms':>10}{'avg_size':>10}"
          f"{'flag_calls':>12}")
    for tool in (td.tool_name for td in HF_TOOLS):
        rows = by_tool.get(tool, [])
        if not rows:
            continue
        flagged = sum(1 for r in rows if r["flags"])
        avg_ms = sum(r["latency_ms"] for r in rows) / len(rows)
        avg_size = sum(r["size_chars"] for r in rows) / len(rows)
        print(f"{tool:<22}{len(rows):>6}{avg_ms:>10.1f}{avg_size:>10.0f}"
              f"{flagged:>12}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true",
                        help="emit JSON instead of human report")
    parser.add_argument("--tool", default=None,
                        help="limit to a single tool name")
    args = parser.parse_args()

    cases = CASES if not args.tool else [c for c in CASES if c["tool"] == args.tool]
    results = [run_case(c) for c in cases]

    if args.json:
        print(json.dumps(results, indent=2, default=str))
    else:
        print_human(results)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
