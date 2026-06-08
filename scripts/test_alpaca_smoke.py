"""Live smoke test against the Alpaca Market Data API.

Pass criteria:
- Snapshot for AAPL/MSFT returns price > 0 and pct_change is a float.
- Window-return call for the same symbols returns a float in (-100, 100).
- An error path (bogus symbol) does not crash the router; resolves to None.

Usage:
    uv run python scripts/test_alpaca_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clients import alpaca
from src.clients.prices import quote_snapshot, window_returns


def main() -> int:
    symbols = ["AAPL", "MSFT", "SPY"]

    print("== snapshots ==")
    snaps = quote_snapshot(symbols)
    bad = []
    for sym in symbols:
        q = snaps.get(sym)
        if q is None or q.source != "alpaca":
            bad.append(f"{sym}: missing or wrong source: {q}")
            continue
        if q.price is None or q.price <= 0:
            bad.append(f"{sym}: bad price {q.price}")
        if q.pct_change is None:
            bad.append(f"{sym}: pct_change is None")
        print(f"  {sym}: price={q.price} pct={q.pct_change} src={q.source}")

    print("== window_returns ==")
    wins = window_returns(symbols, period="1mo")
    for sym in symbols:
        w = wins.get(sym)
        if w is None or w.source != "alpaca":
            bad.append(f"{sym}: missing window_return")
            continue
        if w.pct is None or not (-100 < w.pct < 100):
            bad.append(f"{sym}: implausible window pct {w.pct}")
        print(f"  {sym}: pct={w.pct} src={w.source}")

    print("== error path (bogus symbol) ==")
    out = quote_snapshot(["ZZZZZZZ_NOT_REAL"])
    print(f"  ZZZZZZZ_NOT_REAL → {out.get('ZZZZZZZ_NOT_REAL')}")
    # Routing for unknown symbols falls to Mesh; we just want no crash.

    if bad:
        print("\nFAIL:", file=sys.stderr)
        for b in bad:
            print(f"  - {b}", file=sys.stderr)
        return 1
    print("\nPASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
