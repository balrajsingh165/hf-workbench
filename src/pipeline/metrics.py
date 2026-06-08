"""Parse stdout/stderr from pipeline step subprocesses into metric dicts."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.pipeline.db import count_rows, count_story_rows
from src.pipeline.step import StepResult


def parse_ingest_metrics(
    result: StepResult,
    db_path: Path,
    *,
    before_news: int,
    before_links: int,
    before_stories: int,
) -> dict[str, Any]:
    blob = f"{result.stdout}\n{result.stderr}"
    ingested = re.findall(r"\bnews_\d{3}\b", blob)
    stories = re.findall(r"\bstory_\d{3}\b", blob)
    provider_calls = sum(
        1
        for line in blob.splitlines()
        if (
            ("exa:" in line or "firecrawl:" in line)
            and "exa: skipped" not in line
            and "firecrawl: skipped" not in line
        )
    )
    return {
        "news_before": before_news,
        "news_after": count_rows(db_path, "news"),
        "stories_before": before_stories,
        "stories_after": count_story_rows(db_path),
        "links_before": before_links,
        "links_after": count_rows(db_path, "thesis_story_links"),
        "news_ids_observed": sorted(set(ingested)),
        "story_ids_observed": sorted(set(stories)),
        "search_provider_calls_estimated": provider_calls,
        "exa_calls_estimated": provider_calls if "exa:" in blob else 0,
        "body_fetch_batches": blob.count("bodies: "),
    }


def parse_match_metrics(
    results: list[StepResult],
    db_path: Path,
    *,
    before_links: int,
) -> dict[str, Any]:
    judge_calls = 0
    failed_judges = 0
    failed_theses: list[str] = []
    for result in results:
        blob = f"{result.stdout}\n{result.stderr}"
        judge_calls += blob.count("chunk-win:")
        failed_judges += blob.count("could not parse judge response")
        if not result.ok:
            failed_theses.append(result.metrics.get("thesis_id", "unknown"))
    return {
        "theses_processed": len(results),
        "theses_failed": failed_theses,
        "judge_calls_estimated": judge_calls,
        "judge_failures": failed_judges,
        "links_before": before_links,
        "links_after": count_rows(db_path, "thesis_story_links"),
    }


def parse_judge_metrics(result: StepResult) -> dict[str, Any]:
    blob = f"{result.stdout}\n{result.stderr}"
    counts = {"good": 0, "unclear": 0, "no_value": 0}
    for line in blob.splitlines():
        for label in counts:
            if line.endswith(f": {label}"):
                counts[label] += 1
    return {
        "stories_judged": sum(counts.values()),
        "judge_label_good": counts["good"],
        "judge_label_unclear": counts["unclear"],
        "judge_label_no_value": counts["no_value"],
    }


def parse_score_metrics(result: StepResult) -> dict[str, Any]:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        payload = {}
    scored = payload.get("scored") if isinstance(payload, dict) else None
    return {
        "theses_scored": len(scored) if isinstance(scored, list) else None,
        "mesh_price_batches_estimated": 1 if "tailwind=" in result.stderr else 0,
    }


def parse_brief_metrics(result: StepResult) -> dict[str, Any]:
    blob = f"{result.stdout}\n{result.stderr}"
    themes_match = re.search(r"themes=(\d+)", blob)
    movers_match = re.search(r"movers=(\d+)/(\d+) quoted", blob)
    return {
        "themes": int(themes_match.group(1)) if themes_match else None,
        "movers_quoted": int(movers_match.group(1)) if movers_match else None,
        "movers_total": int(movers_match.group(2)) if movers_match else None,
        "llm_calls_estimated": 1 if "synthesizing themes" in blob else 0,
    }
