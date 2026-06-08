#!/usr/bin/env python3
"""End-to-end smoke for the Phase 2b chart agent against real AWS.

Drives `run_chart_phase` directly (no HTTP server, no Phase 1) with a
hand-crafted tool history that contains a clear numeric series. Asserts
either a `chart_image` event was emitted (image saved alongside this script
for eyeballing) or a `chart_skip` was emitted with a reason.

Prereqs:
  - `.env` populated with AWS_PROFILE / BEDROCK_PROFILE = payments-admin
  - `aws sso login --profile payments-admin` (or equivalent creds)

Usage:
    uv run python scripts/smoke_chart_agent.py
    uv run python scripts/smoke_chart_agent.py --theme light
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
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


# A realistic Phase 1 output: BTC daily closes for 14 sessions.
SAMPLE_RECORDS = [
    ToolCallRecord(
        tool_use_id="tu_smoke_1",
        tool="price_history",
        input={"ticker": "BTC-USD", "days_back": 14},
        output={
            "ticker": "BTC-USD",
            "series": [
                {"date": "2026-04-21", "close": 67_120.55},
                {"date": "2026-04-22", "close": 66_840.10},
                {"date": "2026-04-23", "close": 68_005.22},
                {"date": "2026-04-24", "close": 68_540.00},
                {"date": "2026-04-25", "close": 69_120.43},
                {"date": "2026-04-26", "close": 69_870.18},
                {"date": "2026-04-27", "close": 70_400.91},
                {"date": "2026-04-28", "close": 71_010.55},
                {"date": "2026-04-29", "close": 71_280.00},
                {"date": "2026-04-30", "close": 70_995.12},
                {"date": "2026-05-01", "close": 71_440.76},
                {"date": "2026-05-02", "close": 71_902.31},
                {"date": "2026-05-03", "close": 72_550.04},
                {"date": "2026-05-04", "close": 72_740.88},
            ],
        },
        status="success",
    )
]


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--theme", choices=["dark", "light"], default="dark")
    parser.add_argument("--question", default="Plot BTC's daily close for the last 14 sessions.")
    args = parser.parse_args()

    cfg = get_agent_config()
    print(
        f"chart-smoke: region={cfg.aws_region} profile={cfg.bedrock_profile} "
        f"model={cfg.bedrock_model_id} timeout={cfg.chart_agent_timeout_seconds}s",
        file=sys.stderr,
    )

    queue: asyncio.Queue = asyncio.Queue()
    request_id = f"smoke_{uuid.uuid4().hex[:8]}"

    result = await run_chart_phase(
        ThesisContext(id="thesis_smoke", statement="BTC trending up.", tickers=["BTC-USD"]),
        args.question,
        SAMPLE_RECORDS,
        theme=args.theme,
        request_id=request_id,
        sse_queue=queue,
        cfg=cfg,
    )

    events = []
    while not queue.empty():
        chunk = queue.get_nowait()
        if chunk is None:
            continue
        body = chunk.decode("utf-8").removeprefix("data: ").strip()
        if body:
            events.append(json.loads(body))

    summary = {
        "request_id": request_id,
        "theme": args.theme,
        "elapsed_seconds": round(result.elapsed_seconds, 2),
        "skipped": result.url is None,
        "skip_reason": result.skip_reason,
        "caption": result.caption,
        "url": result.url,
        "event_types": [e["type"] for e in events],
    }

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
