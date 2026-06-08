#!/usr/bin/env python3
"""Retroactively rewrite all thesis titles + content into retail register.

Reads every ``global/theses/*.md`` file, lints for em-dashes and the banned
analyst-jargon vocabulary, and (for offending theses) calls an LLM to rewrite
the title + core_thesis + invalidations in plain trading-floor English while
preserving:
- the underlying market belief and direction,
- the named tickers (we do not touch ``entity_tickers``),
- the ``theses`` DB row (id, owner_count, created_at, origin, review_status,
  source_context),
- ``thesis_story_links`` (story matches stay intact),
- ``user_theses`` (user ownership stays intact).

Re-embeds ``thesis_match_chunks`` for each rewritten thesis (delete + re-insert
for that thesis_id only) so semantic search reflects the new wording.

Usage::

    uv run python scripts/retro_rewrite_thesis_titles.py --dry-run
    uv run python scripts/retro_rewrite_thesis_titles.py --ids thesis_044 thesis_045
    uv run python scripts/retro_rewrite_thesis_titles.py            # rewrite all dirty
    uv run python scripts/retro_rewrite_thesis_titles.py --force    # rewrite everything
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.clients.gemini import (  # noqa: E402
    GEMINI_3_FLASH_PREVIEW,
    batch_embed_contents,
    generate_text_with_retry,
)
from src.thesis.docs import build_thesis_chunks, parse_thesis_markdown  # noqa: E402
from src.thesis.lint import (  # noqa: E402
    lint_banned_vocab,
    lint_price_targets,
    lint_thesis_document,
)
from src.thesis.match_index import (  # noqa: E402
    THESIS_MATCH_EMBEDDING_DIMENSIONALITY,
    THESIS_MATCH_EMBEDDING_MODEL,
    ensure_match_index_schema,
    get_db_connection,
)

DB_PATH = ROOT / "db" / "hf.db"
THESES_DIR = ROOT / "global" / "theses"


REWRITE_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "core_thesis": {"type": "string"},
        "invalidations": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 2,
        },
    },
    "required": ["title", "core_thesis", "invalidations"],
}


REWRITE_SYSTEM_PROMPT = """\
You rewrite finance theses from analyst-deck register into plain \
trading-floor English. The reader is a retail trader, not a portfolio manager.

You receive an EXISTING thesis (title + core thesis + invalidation conditions) \
and must produce a rewritten version that:
- preserves the underlying market belief and direction EXACTLY,
- preserves the named tickers and companies (do not swap them out),
- uses plain English that a smart retail trader would actually say.

## Title rules
1. 8 to 12 words.
2. One claim, one direction. No "while", "despite", "but", "although".
3. Name the recognizable thing readers see in headlines: Trump, Powell, Fed, \
OPEC, China, Nvidia, Tesla, the BOE, the ECB, etc. Do NOT use faceless \
abstractions like "executive attacks on central bank independence" when \
"Trump pressuring the Fed" is what is actually happening.
4. Use accepted abbreviations: AI, EV, Fed, BOJ, ECB, OPEC, CPI, GDP, EPS, \
SaaS.
5. Use everyday verbs: pushes, lifts, drags, squeezes, caps, boosts, hits, \
knocks down, drives up, weighs on, fuels, keeps, breaks, holds, stalls.
6. AVOID this banned vocabulary EVERYWHERE (title, core_thesis, \
invalidations): compresses, re-rates, repricing, structural, valuations, \
outperformance, divergence, spillover, convergence, premium (except in the \
fixed phrase "risk premium"), elevated, headwinds, tailwinds, secular, \
multiples, broadens, unwind.
7. NO em-dashes. Do not use the long horizontal em-dash character anywhere. \
Use commas, periods, or semicolons.
8. No hedging: no "might", "could", "may", "possibly", "looks set to".
9. NO predicted price targets in the title or core_thesis. Do NOT write \
specific price predictions like "oil past $95", "gold past $3,500", \
"SPX above 7,000", "10Y to 5.5%". Price levels are noisy and brittle and \
make the belief feel like a forecast instead of a position. State the \
direction and the catalyst, not the number. Specific price thresholds DO \
belong in invalidation_conditions (those need to be testable; e.g. "WTI \
below $78 for five trading days"). This rule applies ONLY to the title \
and the core_thesis narrative.

## Core thesis rules
- 2 to 4 sentences.
- Same plain English as the title.
- Name the catalyst (who and what), the mechanism (why prices move), the \
tickers, and the timeline.
- Same banned vocabulary, same no-em-dash rule, same no-price-target rule.

## Invalidation rules
- Preserve the existing conditions. Just rephrase into plain English if \
needed.
- Preserve specific numbers and thresholds verbatim (price levels ARE \
welcome here, they make the invalidation testable).
- Same no-em-dash rule.
- Return at least 2 conditions.

## Title gold standard, match this register
- "The Fed won't cut rates this year, sticky inflation keeps yields high."
- "Trump pressuring the Fed pushes long-bond yields higher."
- "Cheap UK stocks attract a takeover wave from foreign buyers."
- "Rising bond yields drag crypto miners and exchanges down."
- "OPEC keeps cuts in place, oil stays bid through summer driving season."
- "AI server demand lifts Dell, HPE, and memory makers."
- "Central banks keep buying gold, the rally has more room to run."
- "China's rare earth controls boost Western mining stocks."

Return JSON: {"title": str, "core_thesis": str, "invalidations": [str]}.
"""


def render_thesis_markdown(title: str, core: str, invalidations: list[str]) -> str:
    lines = [f"# Thesis: {title}", ""]
    lines.append("## Core Thesis")
    lines.append(core)
    lines.append("")
    lines.append("## Invalidation Conditions")
    for inv in invalidations:
        lines.append(f"- {inv}")
    lines.append("")
    return "\n".join(lines)


def reembed_thesis(conn, thesis_id: str) -> int:
    doc = parse_thesis_markdown(THESES_DIR / f"{thesis_id}.md")
    chunks = build_thesis_chunks(doc)
    text_batches = [[c.chunk_text] for c in chunks]
    results = batch_embed_contents(
        text_batches,
        model=THESIS_MATCH_EMBEDDING_MODEL,
        output_dimensionality=THESIS_MATCH_EMBEDDING_DIMENSIONALITY,
        task_type="RETRIEVAL_DOCUMENT",
    )
    embeddings = [e for r in results for e in r.embeddings]
    with conn:
        conn.execute(
            "DELETE FROM thesis_match_chunks WHERE thesis_id = ?", (thesis_id,)
        )
        for chunk, embedding in zip(chunks, embeddings, strict=True):
            conn.execute(
                """
                INSERT INTO thesis_match_chunks (
                    thesis_id, chunk_key, chunk_kind, chunk_text,
                    tickers_json, sectors_json, embedding_model, embedding_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%dT%H:%M:%SZ','now'))
                """,
                (
                    chunk.thesis_id,
                    chunk.chunk_key,
                    chunk.chunk_kind,
                    chunk.chunk_text,
                    json.dumps(chunk.tickers),
                    json.dumps(chunk.sectors),
                    THESIS_MATCH_EMBEDDING_MODEL,
                    json.dumps(embedding),
                ),
            )
    return len(chunks)


def rewrite_one(doc, *, attempts: int = 2) -> dict:
    """Call the LLM up to *attempts* times to produce a clean rewrite."""
    existing_block = (
        f"EXISTING TITLE: {doc.title}\n\n"
        f"EXISTING CORE THESIS:\n{doc.core_thesis}\n\n"
        f"EXISTING INVALIDATION CONDITIONS:\n"
        + "\n".join(f"- {inv}" for inv in doc.invalidations)
        + "\n\n"
        f"EXISTING TICKERS (preserve these): "
        + ", ".join(doc.tickers)
    )

    last_residual: list[str] = []
    last_parsed: dict | None = None
    for attempt in range(attempts):
        nudge = ""
        if attempt > 0 and last_residual:
            nudge = (
                "\n\nYour previous attempt still contained banned content: "
                + ", ".join(last_residual)
                + ". Rewrite WITHOUT those tokens, without em-dashes, "
                "and without specific price targets in the title or core_thesis."
            )
        res = generate_text_with_retry(
            contents=existing_block + nudge,
            model=GEMINI_3_FLASH_PREVIEW,
            system_instruction=REWRITE_SYSTEM_PROMPT,
            thinking_level="low",
            response_mime_type="application/json",
            response_json_schema=REWRITE_SCHEMA,
        )
        parsed = json.loads(res.text)
        title = parsed.get("title", "")
        core = parsed.get("core_thesis", "")
        invalidations_text = "\n".join(parsed.get("invalidations", []))
        residual = (
            lint_banned_vocab(title + "\n" + core + "\n" + invalidations_text)
            + [f"price:{p}" for p in lint_price_targets(title + "\n" + core)]
        )
        last_residual = residual
        last_parsed = parsed
        if not residual:
            return parsed
    # Return the best attempt even if residual remains; caller decides.
    return last_parsed or {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ids", nargs="*", help="Specific thesis IDs to rewrite")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print rewrites but do not write files or update DB")
    ap.add_argument("--no-embed", action="store_true",
                    help="Skip re-embedding (just rewrite markdown)")
    ap.add_argument("--force", action="store_true",
                    help="Rewrite even if markdown is already clean")
    ap.add_argument("--limit", type=int, default=None,
                    help="Cap how many theses to process this run")
    args = ap.parse_args()

    if args.ids:
        thesis_ids = list(args.ids)
    else:
        thesis_ids = sorted(p.stem for p in THESES_DIR.glob("thesis_*.md"))
    if args.limit:
        thesis_ids = thesis_ids[: args.limit]

    print(f"Processing {len(thesis_ids)} theses (dry_run={args.dry_run})...\n")

    conn = None
    if not args.dry_run:
        conn = get_db_connection(DB_PATH)
        ensure_match_index_schema(conn)

    rewritten = 0
    skipped_clean = 0
    errors = 0

    try:
        for tid in thesis_ids:
            path = THESES_DIR / f"{tid}.md"
            if not path.exists():
                print(f"  {tid}: file missing, skipping")
                continue
            try:
                doc = parse_thesis_markdown(path)
            except ValueError as e:
                print(f"  {tid}: parse failed: {e}")
                errors += 1
                continue

            report = lint_thesis_document(doc)
            if report.is_clean and not args.force:
                skipped_clean += 1
                continue

            issues: list[str] = []
            issues += [f"banned-title:{w}" for w in report.banned_in_title]
            issues += [f"banned-core:{w}" for w in report.banned_in_core]
            issues += [f"banned-inv:{w}" for w in report.banned_in_invalidations]
            issues += [f"price-title:{p}" for p in report.price_targets_in_title]
            issues += [f"price-core:{p}" for p in report.price_targets_in_core]
            issue_str = (
                ", ".join(issues[:5]) + ("..." if len(issues) > 5 else "")
                if issues else "forced"
            )
            print(f"  {tid}: rewriting (issues: {issue_str})")

            try:
                parsed = rewrite_one(doc)
            except Exception as e:
                print(f"    LLM call failed: {e}")
                errors += 1
                continue

            new_title = parsed.get("title", "").strip()
            new_core = parsed.get("core_thesis", "").strip()
            new_invs = [i.strip() for i in parsed.get("invalidations", []) if i.strip()]

            if not new_title or not new_core or len(new_invs) < 2:
                print(f"    rewrite invalid (title/core/invalidations missing), skipping")
                errors += 1
                continue

            residual = (
                lint_banned_vocab(new_title + "\n" + new_core + "\n" + "\n".join(new_invs))
                + [f"price:{p}" for p in lint_price_targets(new_title + "\n" + new_core)]
            )
            if residual:
                print(f"    WARNING: rewrite still contains: {', '.join(residual)}")

            print(f"    OLD: {doc.title}")
            print(f"    NEW: {new_title}")

            if args.dry_run:
                rewritten += 1
                continue

            new_md = render_thesis_markdown(new_title, new_core, new_invs)
            path.write_text(new_md, encoding="utf-8")

            if not args.no_embed and conn is not None:
                try:
                    n = reembed_thesis(conn, tid)
                    print(f"    re-embedded {n} chunks")
                except Exception as e:
                    print(f"    re-embed failed: {e}")
                    errors += 1
                    continue

            rewritten += 1
            # Polite rate-limit pause for big batches.
            time.sleep(0.2)
    finally:
        if conn is not None:
            conn.close()

    print(
        f"\nDone. Rewritten: {rewritten}, skipped (already clean): {skipped_clean}, "
        f"errors: {errors}"
    )
    return 0 if errors == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
