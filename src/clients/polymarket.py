"""Polymarket Gamma API client — public market data, no auth required.

Docs / verified endpoints (2026-04-25):
  GET https://gamma-api.polymarket.com/markets
    ?active=true&closed=false&limit=100&offset=N
  Returns a JSON array of market objects directly (not wrapped).

Volume field: `volume` is a USDC float (string or numeric).  The API also
exposes `volumeNum` on some versions — we probe both to be safe.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

GAMMA_BASE = "https://gamma-api.polymarket.com"
DEFAULT_TIMEOUT = 30.0
PAGE_SIZE = 100


@dataclass(slots=True)
class PolymarketMarket:
    id: str           # conditionId (preferred) or numeric id
    slug: str
    question: str
    description: str
    volume_usd: float
    closes_at: str | None

    @property
    def url(self) -> str:
        slug = self.slug or self.id
        return f"https://polymarket.com/event/{slug}"

    @property
    def embed_text(self) -> str:
        parts = [self.question]
        if self.description:
            parts.append(self.description[:400])
        return "\n\n".join(parts).strip()


def _parse_volume(raw: dict) -> float:
    for field in ("volumeNum", "volume_num", "volume"):
        v = raw.get(field)
        if v is None:
            continue
        try:
            return float(v)
        except (ValueError, TypeError):
            pass
    return 0.0


def _parse_market(raw: dict) -> PolymarketMarket | None:
    question = (raw.get("question") or raw.get("title") or "").strip()
    if not question:
        return None
    end = (
        raw.get("endDate") or raw.get("end_date") or
        raw.get("endDateIso") or ""
    )
    return PolymarketMarket(
        id=str(raw.get("conditionId") or raw.get("id") or ""),
        slug=str(raw.get("slug") or ""),
        question=question,
        description=(raw.get("description") or "").strip(),
        volume_usd=_parse_volume(raw),
        closes_at=end[:10] or None,
    )


def fetch_open_markets(
    min_volume_usd: float = 50_000,
    *,
    max_results: int = 1000,
    timeout: float = DEFAULT_TIMEOUT,
) -> list[PolymarketMarket]:
    """Return active, non-closed markets with volume >= min_volume_usd."""
    out: list[PolymarketMarket] = []
    offset = 0

    with httpx.Client(timeout=timeout) as client:
        while len(out) < max_results:
            resp = client.get(
                f"{GAMMA_BASE}/markets",
                params={
                    "active": "true",
                    "closed": "false",
                    "limit": PAGE_SIZE,
                    "offset": offset,
                    "order": "volumeNum",
                    "ascending": "false",
                },
            )
            resp.raise_for_status()
            page: list = resp.json()
            if not isinstance(page, list) or not page:
                break
            page_below_floor = False
            for raw in page:
                if len(out) >= max_results:
                    break
                if not isinstance(raw, dict):
                    continue
                vol = _parse_volume(raw)
                if vol < min_volume_usd:
                    # Sorted by volume desc — once we drop below the floor we are done.
                    page_below_floor = True
                    break
                market = _parse_market(raw)
                if market:
                    out.append(market)
            offset += len(page)
            if page_below_floor or len(page) < PAGE_SIZE or len(out) >= max_results:
                break

    return out


__all__ = ["PolymarketMarket", "fetch_open_markets"]
