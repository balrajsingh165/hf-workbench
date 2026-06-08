from __future__ import annotations

import json
from dataclasses import dataclass, field

from src.clients.gemini import (
    GEMINI_3_FLASH_PREVIEW,
    GeminiUsage,
    generate_text_with_retry,
)
from src.news.taxonomies import CANONICAL_REGIONS, CANONICAL_SECTORS, normalize_regions, normalize_sectors
from src.news.themes import ALL_TAGS, THEME_TAGS
from src.news.types import ClusterSourceDoc


_CLUSTER_SYNTH_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "what_changed": {"type": "string"},
        "overview": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "source_doc_ids": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                },
                "required": ["text", "source_doc_ids", "confidence"],
            },
        },
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "claim_type": {"type": "string"},
                    "source_doc_ids": {"type": "array", "items": {"type": "string"}},
                    "stance": {"type": "string"},
                },
                "required": ["text", "claim_type", "source_doc_ids", "stance"],
            },
        },
        "quotes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "speaker": {"type": "string"},
                    "speaker_title": {"type": "string"},
                    "source_doc_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "speaker", "source_doc_ids"],
            },
        },
        "market_relevance": {
            "type": "object",
            "properties": {
                "tickers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "symbol": {"type": "string"},
                            "source_doc_id": {"type": "string"},
                            "evidence_span": {"type": "string"},
                        },
                        "required": ["symbol", "source_doc_id", "evidence_span"],
                    },
                },
                "sectors": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tag": {"type": "string", "enum": list(CANONICAL_SECTORS)},
                            "source_doc_id": {"type": "string"},
                        },
                        "required": ["tag", "source_doc_id"],
                    },
                },
                "regions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tag": {"type": "string", "enum": list(CANONICAL_REGIONS)},
                            "source_doc_id": {"type": "string"},
                        },
                        "required": ["tag", "source_doc_id"],
                    },
                },
                "direction": {"type": "string", "enum": ["bullish", "bearish", "mixed", "neutral"]},
                "horizon": {"type": "string", "enum": ["days", "weeks", "months"]},
            },
            "required": ["tickers", "sectors", "regions", "direction", "horizon"],
        },
        "theme_tag": {
            "type": "string",
            "enum": list(ALL_TAGS),
            "description": (
                "Closed taxonomy tag for the durable, multi-week market context this "
                "story belongs to. Emit `other` when no tag fits — `other` stories "
                "are excluded from thesis discovery."
            ),
        },
    },
    "required": [
        "headline",
        "what_changed",
        "overview",
        "claims",
        "quotes",
        "market_relevance",
        "theme_tag",
    ],
}


@dataclass
class ClusterSynthesis:
    headline: str
    what_changed: str
    overview: list[dict]
    claims: list[dict]
    quotes: list[dict]
    market_relevance: dict
    theme_tag: str = "other"
    model_id: str = GEMINI_3_FLASH_PREVIEW
    latency_seconds: float | None = None
    usage: GeminiUsage = field(default_factory=GeminiUsage)
    cost_usd: float = 0.0

    @property
    def tickers(self) -> list[str]:
        out: list[str] = []
        for t in self.market_relevance.get("tickers") or []:
            sym = t.get("symbol") if isinstance(t, dict) else t
            sym = str(sym or "").strip().upper()
            if sym:
                out.append(sym)
        return out

    @property
    def sectors(self) -> list[str]:
        raw = [
            (s.get("tag") if isinstance(s, dict) else s) or ""
            for s in self.market_relevance.get("sectors") or []
        ]
        return normalize_sectors([str(s) for s in raw])

    @property
    def regions(self) -> list[str]:
        raw = [
            (r.get("tag") if isinstance(r, dict) else r) or ""
            for r in self.market_relevance.get("regions") or []
        ]
        return normalize_regions([str(r) for r in raw])

    def flatten_market_relevance(self) -> None:
        """Collapse object-form tickers/sectors/regions to plain strings.

        Called after evidence-anchor verification passes — downstream storage
        and deterministic backfills work on flat string lists.
        """
        rel = dict(self.market_relevance or {})
        rel["tickers"] = self.tickers
        rel["sectors"] = self.sectors
        rel["regions"] = self.regions
        self.market_relevance = rel

    def as_payload(self) -> dict:
        return {
            "headline": self.headline,
            "what_changed": self.what_changed,
            "overview": self.overview,
            "claims": self.claims,
            "quotes": self.quotes,
            "market_relevance": self.market_relevance,
            "theme_tag": self.theme_tag,
        }


def _cluster_prompt_payload(items: list[ClusterSourceDoc]) -> str:
    payload = []
    for item in items:
        payload.append({
            "news_id": item.news_id,
            "title": item.title,
            "url": item.url,
            "publisher": item.publisher,
            "published": item.published,
            "tickers": item.tickers,
            "sectors": item.sectors,
            "regions": item.regions,
            "body": item.body[:8000],
        })
    return json.dumps(payload, indent=2)


def synthesize_cluster(
    items: list[ClusterSourceDoc],
    *,
    sector_prior: list[str] | None = None,
    region_prior: list[str] | None = None,
    event_class: str | None = None,
    ticker_candidates: list[dict] | None = None,
) -> ClusterSynthesis:
    if not items:
        raise ValueError("no cluster members to synthesize")

    payload = _cluster_prompt_payload(items[:3])
    if ticker_candidates:
        slate_lines: list[str] = []
        for c in ticker_candidates:
            sym = c.get("symbol") or ""
            display = c.get("display") or sym
            aliases = c.get("aliases") or [display]
            aliases_json = json.dumps(aliases, ensure_ascii=False)
            slate_lines.append(f"- {sym} — {display} | aliases: {aliases_json}")
        slate_block = (
            "Ticker candidate slate (closed list — pick ONLY symbols on this slate; "
            "the verifier rejects any symbol not listed):\n" + "\n".join(slate_lines)
        )
    else:
        slate_block = (
            "Ticker candidate slate is EMPTY — return tickers: []. The verifier "
            "rejects any symbol; do not invent one even if the body mentions a "
            "company by name."
        )

    prompt = f"""You synthesize a market story from a cluster of raw news documents.

Inputs are source documents with stable `news_id` values:
{payload}

{slate_block}

Hard rules:
- Return exactly the JSON schema.
- Every overview bullet, claim, and quote must cite source_doc_ids from the inputs.
- Do not cite a source unless the cited document directly supports that text.
- Quotes must be verbatim substrings from a cited source body.
- Every quote must name a speaker — the person, their title, the company, or the originating publication. Use the cited body to identify who said it; fall back to the publisher name only when the body genuinely has no attribution. Never leave speaker blank.
- Use direct, declarative language. Avoid hedging words: may, could, might, perhaps.
- The headline is one factual sentence for the event, not a copied source headline.
- what_changed states the new information that makes this story matter now.
- market_relevance.tickers uses Yahoo-form symbols only, max 6, **chosen exclusively from the candidate slate above**.
- market_relevance.sectors must use only this enum: {", ".join(CANONICAL_SECTORS)}
- market_relevance.regions must use only this enum: {", ".join(CANONICAL_REGIONS)}

Ticker selection (slate-constrained, evidence-anchored, verification rejects unsupported entries):
- Choose `symbol` from the candidate slate above and nowhere else. Symbols not on the slate are rejected by the verifier — return [] if no slate symbol fits.
- Set `evidence_span` to the verbatim alias from the symbol's `aliases` list as it appears in the cited body. For POWL with aliases ["Powell Industries"], evidence_span MUST be "Powell Industries" (or a longer form containing it) — never "Powell" alone, which refers to Jerome Powell. The verifier enforces alias-substring matching.
- Set `source_doc_id` to the input `news_id` whose body literally contains that alias.
- Emit an issuer ticker only when the cited body names the issuer as materially exposed to the event (acquirer, target, manufacturer, named exposed party). Name-drops and "could affect X" speculation do not qualify.
- For FDA/regulatory approvals: tag the manufacturer's ticker only when (a) the manufacturer is on the slate AND (b) the cited body names it; otherwise return [].
- Macro / thematic stories (rates, currency, commodities, central-bank policy): do NOT staple unrelated mega-cap equities like AAPL/NVDA/MSFT to the story. The slate seeds thematic instruments (e.g. TLT for Treasury yields, USO for crude oil, GLD for gold, DXY for the dollar, IBIT for Bitcoin) for exactly this case — pick them when their alias appears verbatim in the cited body. If no slate symbol fits, return [].

Sectors / regions (evidence-anchored):
- Each entry needs `source_doc_id` set to the input `news_id` whose body materially supports that taxonomy tag.
- Do not tag a sector or region the cited body only loosely associates with the event.

Taxonomy priors from source/ticker classification:
- sectors: {", ".join(normalize_sectors(sector_prior or [])) or "none"}
- regions: {", ".join(normalize_regions(region_prior or [])) or "none"}
- event_class: {event_class or "none"}

theme_tag rules:
- Pick exactly one tag from the closed taxonomy below that names the durable, multi-week market context this story belongs to.
- A theme is broader than the headline event. It should outlive any single news cycle and frame how the story would inform a multi-week trading thesis.
- Emit `other` when no tag fits — single-name earnings without sector implication, isolated regulatory matters, structural news with limited cross-reading. `other` stories are excluded from thesis discovery, so do not force a tag to keep a story in the pipeline.
- Do NOT invent new tags. The list is closed.

Closed theme taxonomy (tag — meaning):
{chr(10).join(f"- {tag}: {desc}" for tag, desc in THEME_TAGS.items())}
"""
    res = generate_text_with_retry(
        prompt,
        model=GEMINI_3_FLASH_PREVIEW,
        response_mime_type="application/json",
        response_json_schema=_CLUSTER_SYNTH_SCHEMA,
        thinking_level="low",
    )
    data = json.loads(res.text)
    relevance = dict(data.get("market_relevance") or {})
    # Keep object form so the verifier can check source_doc_id + evidence_span
    # against cited member bodies. Flattening to plain string lists happens
    # post-verification (see ClusterSynthesis.flatten_market_relevance).
    relevance["tickers"] = [
        {
            "symbol": str(t.get("symbol") or "").strip().upper(),
            "source_doc_id": str(t.get("source_doc_id") or "").strip(),
            "evidence_span": str(t.get("evidence_span") or "").strip(),
        }
        for t in (relevance.get("tickers") or [])
        if isinstance(t, dict)
    ]
    relevance["sectors"] = [
        {
            "tag": str(s.get("tag") or "").strip(),
            "source_doc_id": str(s.get("source_doc_id") or "").strip(),
        }
        for s in (relevance.get("sectors") or [])
        if isinstance(s, dict)
    ]
    relevance["regions"] = [
        {
            "tag": str(r.get("tag") or "").strip(),
            "source_doc_id": str(r.get("source_doc_id") or "").strip(),
        }
        for r in (relevance.get("regions") or [])
        if isinstance(r, dict)
    ]
    theme_tag = str(data.get("theme_tag") or "").strip()
    if theme_tag not in ALL_TAGS:
        theme_tag = "other"
    return ClusterSynthesis(
        headline=str(data.get("headline") or "").strip(),
        what_changed=str(data.get("what_changed") or "").strip(),
        overview=list(data.get("overview") or []),
        claims=list(data.get("claims") or []),
        quotes=list(data.get("quotes") or []),
        market_relevance=relevance,
        theme_tag=theme_tag,
        model_id=res.model,
        latency_seconds=res.latency_seconds,
        usage=res.usage,
        cost_usd=res.cost_usd,
    )

__all__ = ["ClusterSynthesis", "synthesize_cluster"]
