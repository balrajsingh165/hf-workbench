#!/usr/bin/env python3
"""Non-price smoke for the chart agent.

Feeds a hand-crafted tool history with comparative fundamental figures
(quarterly revenue for two industrial-automation peers) so the chart agent
can render under the no-price-chart policy. Mirrors the structure of
scripts/smoke_chart_agent.py but with chartable shapes the policy permits.

Usage:
    uv run python scripts/smoke_chart_agent_nonprice.py
    uv run python scripts/smoke_chart_agent_nonprice.py --out artifact.png
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from src.agent.chart import run_chart_phase  # noqa: E402
from src.agent.config import get_agent_config  # noqa: E402
from src.agent.models import ThesisContext, ToolCallRecord  # noqa: E402


SAMPLE_RECORDS = [
    ToolCallRecord(
        tool_use_id="tu_nonprice_1",
        tool="search_evidence",
        input={"thesis_id": "thesis_002", "days_back": 30},
        output={
            "thesis_id": "thesis_002",
            "summary": {
                "supports": 7,
                "stresses": 2,
                "neutral": 3,
                "window_days": 30,
            },
            "items": [
                {"relation": "supports", "confidence": 0.82, "headline": "ROK reaffirms FY guidance on automation backlog"},
                {"relation": "supports", "confidence": 0.78, "headline": "EMR raises segment outlook on process automation demand"},
                {"relation": "stresses", "confidence": 0.71, "headline": "China industrial PMI contracts; potential drag on capex"},
            ],
        },
        status="success",
    )
]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", choices=["dark", "light"], default="dark")
    parser.add_argument(
        "--question",
        default=(
            "Show me a bar chart of how many supporting vs stressing vs neutral "
            "signals the industrial automation thesis has accumulated in the last 30 days."
        ),
    )
    parser.add_argument("--out", default=None, help="Optional path to save the rendered PNG")
    args = parser.parse_args()

    cfg = get_agent_config()
    print(
        f"nonprice-smoke: region={cfg.aws_region} profile={cfg.bedrock_profile} "
        f"model={cfg.bedrock_model_id} timeout={cfg.chart_agent_timeout_seconds}s",
        file=sys.stderr,
    )

    queue: asyncio.Queue = asyncio.Queue()
    request_id = f"nonprice_{uuid.uuid4().hex[:8]}"

    result = await run_chart_phase(
        ThesisContext(
            id="thesis_002",
            statement="Industrial automation backlog supports peer-group earnings power.",
            tickers=["ROK", "EMR"],
        ),
        args.question,
        SAMPLE_RECORDS,
        theme=args.theme,
        request_id=request_id,
        sse_queue=queue,
        cfg=cfg,
    )

    summary = {
        "request_id": request_id,
        "theme": args.theme,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "skipped": result.url is None,
        "skip_reason": result.skip_reason,
        "caption": result.caption,
        "url": result.url,
    }

    if result.url and args.out:
        try:
            with urllib.request.urlopen(result.url, timeout=20) as r:
                Path(args.out).write_bytes(r.read())
            summary["saved_to"] = args.out
        except Exception as exc:
            summary["save_error"] = str(exc)

    print(json.dumps(summary, indent=2))
    return 0 if not result.url is None or result.skip_reason else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
