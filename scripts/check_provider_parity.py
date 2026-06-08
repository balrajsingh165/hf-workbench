"""Print Alpaca vs. Mesh window returns side-by-side.

For 10 symbols spanning both providers, fetch from both and tabulate
`(symbol, alpaca_pct, mesh_pct, abs_diff)`. Eyeball ≤ 0.2pp delta on
liquid US names; bigger drift signals a unit-mismatch or feed difference
worth investigating.

Usage:
    uv run python scripts/check_provider_parity.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.clients import alpaca, prices  # noqa: E402

ALPACA_ELIGIBLE = ["SPY", "QQQ", "AAPL", "NVDA", "TLT", "GLD"]
MESH_ONLY = ["BZ=F", "GC=F", "^TNX", "JPY=X"]


def main() -> int:
    rows: list[tuple[str, float | None, float | None]] = []

    a_returns = alpaca.window_returns(ALPACA_ELIGIBLE, days=31)
    m_alpaca_eligible = prices._mesh_window_returns(ALPACA_ELIGIBLE, period="1mo")
    for sym in ALPACA_ELIGIBLE:
        rows.append((sym, a_returns.get(sym), m_alpaca_eligible.get(sym.upper())))

    m_only = prices._mesh_window_returns(MESH_ONLY, period="1mo")
    for sym in MESH_ONLY:
        rows.append((sym, None, m_only.get(sym.upper())))

    print(f"{'symbol':<14} {'alpaca':>10} {'mesh':>10} {'abs_diff':>10}")
    for sym, a, m in rows:
        a_str = f"{a:+.3f}" if a is not None else "  -"
        m_str = f"{m:+.3f}" if m is not None else "  -"
        diff_str = f"{abs(a - m):.3f}" if (a is not None and m is not None) else "  -"
        print(f"{sym:<14} {a_str:>10} {m_str:>10} {diff_str:>10}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
