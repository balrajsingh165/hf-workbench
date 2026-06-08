"""Fixed 8-ticker mover set for the Daily Brief.

Editorial order is deterministic: equity indices first, then FX, rates,
commodities, vol. Provider routing is handled by the scheduled-job router in
`src.clients.prices` — SPY/QQQ resolve via Alpaca, the rest via Mesh.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from src.cache import load_json, save_json
from src.clients.prices import Quote, quote_snapshot


@dataclass(frozen=True)
class MoverSpec:
    rank: int
    symbol: str         # Yahoo canonical; display name resolved via instruments registry
    asset_class: str    # equity_index | fx | rate | commodity | vol


# Fixed set. Per-day LLM-curated movers are out of scope for MVP.
MOVER_SET: tuple[MoverSpec, ...] = (
    MoverSpec(1, "SPY",      "equity_index"),
    MoverSpec(2, "QQQ",      "equity_index"),
    MoverSpec(3, "DX-Y.NYB", "fx"),
    MoverSpec(4, "^TNX",     "rate"),
    MoverSpec(5, "BZ=F",     "commodity"),
    MoverSpec(6, "GC=F",     "commodity"),
    MoverSpec(7, "^VIX",     "vol"),
    MoverSpec(8, "JPY=X",    "fx"),
)


@dataclass(slots=True)
class MoverReading:
    spec: MoverSpec
    price: float | None
    pct_change: float | None
    source: str | None = None  # 'alpaca' | 'mesh' — debug aid; None for cache hits


def fetch_movers(*, cache_path: Path | None = None) -> list[MoverReading]:
    """Snapshot all 8 movers in one router call.

    If `cache_path` is given and exists, read the cached JSON instead of
    hitting providers — supports deterministic reruns (see the `--force`
    flag on `agents.daily_brief`).
    """
    if cache_path is not None:
        cached = load_json(cache_path)
        if cached is not None:
            return [
                MoverReading(
                    spec=spec,
                    price=cached.get(spec.symbol, {}).get("price"),
                    pct_change=cached.get(spec.symbol, {}).get("pct_change"),
                    source=cached.get(spec.symbol, {}).get("source"),
                )
                for spec in MOVER_SET
            ]

    quotes: dict[str, Quote] = quote_snapshot([m.symbol for m in MOVER_SET])
    readings: list[MoverReading] = []
    for spec in MOVER_SET:
        q = quotes.get(spec.symbol)
        readings.append(
            MoverReading(
                spec=spec,
                price=q.price if q else None,
                pct_change=q.pct_change if q else None,
                source=q.source if q else None,
            )
        )

    if cache_path is not None:
        payload = {
            r.spec.symbol: {"price": r.price, "pct_change": r.pct_change, "source": r.source}
            for r in readings
        }
        save_json(cache_path, payload, sort_keys=True, trailing_newline=True)

    return readings


__all__ = ["MOVER_SET", "MoverReading", "MoverSpec", "fetch_movers"]
