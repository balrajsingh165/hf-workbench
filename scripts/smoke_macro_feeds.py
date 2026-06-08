"""Smoke test for Phase 6.1 — Tier A macro & first-source RSS feeds.

Read-only. No schema changes, no DB writes. Validates each candidate feed
on three axes:

  1. Reachability — does the URL parse and yield entries?
  2. Volume — items in the last 30 days; estimated daily rate.
  3. Gate behavior — pass rate through the existing three-tier ticker
     gate, with breakdowns by tier (T1 exchange / T2 alias / T3 macro).

Macro feeds are expected to fall through T1 (no `(NASDAQ: XYZ)`) and
T2 (no issuer mentioned). T3 (macro keyword) carries the gate, so the
report focuses on whether macro keywords trigger as intended and which
items would benefit from new patterns (`fed_action`, `macro_print`,
`tariff_action`, `sanctions`, `executive_order`).

Usage:
    uv run python scripts/smoke_macro_feeds.py
    uv run python scripts/smoke_macro_feeds.py --feeds 0 2 5    # subset
"""
from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import feedparser

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.firehose_gate import (
    LAWYER_SPAM,
    MACRO_KEYWORDS,
    build_alias_index,
    score_materiality,
    strip_html,
    tag_text,
)

# (label, url). Tier A per docs/plan-news-firehose-2k.md §Source diversity.
# Each feed is expected to be low volume and high signal; a single FOMC
# statement matters more than 200 PR-Newswire items.
#
# Sources removed after May 2026 reachability check:
# - US Treasury press releases — no public RSS endpoint exists
#   (`/news/press-releases/feed` and similar paths return 404). Will arrive
#   second-hand via Tier B (Reuters/CNBC/Yahoo Finance) within seconds of
#   release, so not load-bearing for v1.
# - BLS feeds (Empsit/CPI/PPI/JOLTS, all `bls.gov/feed/*.rss`) — Akamai
#   blocks non-browser User-Agents with HTTP 403 regardless of UA string.
#   Same Tier B mitigation applies; reach for the BLS public data API
#   (data.bls.gov/publicAPI/v2) if first-source becomes load-bearing.
FEEDS: tuple[tuple[str, str], ...] = (
    ("Federal Reserve press",
     "https://www.federalreserve.gov/feeds/press_all.xml"),
    ("Federal Reserve monetary",
     "https://www.federalreserve.gov/feeds/press_monetary.xml"),
    ("BEA news (GDP/PCE)",
     "https://apps.bea.gov/rss/rss.xml"),
    ("ECB press",
     "https://www.ecb.europa.eu/rss/press.html"),
    ("EIA news",
     "https://www.eia.gov/rss/press_rss.xml"),
    # ECB speeches feed (`/rss/speeches.html`) returned malformed XML
    # in May 2026 smoke; ECB press already includes Lagarde/de Guindos
    # speech announcements at lower frequency, so dropping for now.
    # Bank of England — full press / news / publications
    ("Bank of England news",
     "https://www.bankofengland.co.uk/rss/news"),
    # Reserve Bank of Australia — RBA monetary statements
    ("RBA press",
     "https://www.rba.gov.au/rss/rss-cb-media-releases.xml"),
)

WINDOW_DAYS = 30


@dataclass
class FeedReport:
    label: str
    url: str
    ok: bool
    error: str = ""
    n_entries: int = 0
    n_in_window: int = 0
    rate_per_day: float = 0.0
    span_first: str = "—"
    span_last: str = "—"
    via_exchange: int = 0
    via_alias: int = 0
    via_macro: int = 0
    dropped: int = 0
    materiality_buckets: dict[int, int] = field(default_factory=dict)
    sample_passed: list[tuple[str, str, int, list[str]]] = field(default_factory=list)
    sample_dropped: list[str] = field(default_factory=list)


def parse_published(ent) -> datetime | None:
    pub_struct = ent.get("published_parsed") or ent.get("updated_parsed")
    if not pub_struct:
        return None
    try:
        return datetime.fromtimestamp(time.mktime(pub_struct), tz=timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return None


def evaluate_feed(label: str, url: str, ci_index, cs_index) -> FeedReport:
    rep = FeedReport(label=label, url=url, ok=False)
    try:
        parsed = feedparser.parse(url)
    except Exception as exc:
        rep.error = f"parse exception: {exc!r}"
        return rep

    if getattr(parsed, "bozo", 0) and not parsed.entries:
        rep.error = f"bozo+empty: {parsed.bozo_exception!r}"
        return rep

    entries = parsed.entries or []
    if not entries:
        rep.error = "no entries"
        return rep

    rep.ok = True
    rep.n_entries = len(entries)

    now = datetime.now(timezone.utc)
    cutoff = now.timestamp() - WINDOW_DAYS * 86400
    in_window: list[tuple[datetime, str, str]] = []
    for ent in entries:
        ts = parse_published(ent)
        title = (ent.get("title") or "").strip()
        if not title:
            continue
        body = strip_html(ent.get("summary") or ent.get("description") or "")
        if ts is None:
            # Some gov feeds publish without dates; assume "recent"
            in_window.append((now, title, body))
            continue
        if ts.timestamp() < cutoff:
            continue
        in_window.append((ts, title, body))

    rep.n_in_window = len(in_window)
    if in_window:
        ts_only = sorted(t for t, _, _ in in_window)
        span_days = (ts_only[-1] - ts_only[0]).total_seconds() / 86400.0 or 1
        rep.rate_per_day = len(in_window) / span_days
        rep.span_first = ts_only[0].strftime("%Y-%m-%d %H:%M")
        rep.span_last = ts_only[-1].strftime("%Y-%m-%d %H:%M")

    for _, title, body in in_window:
        ex, syms, macros = tag_text(title, body, ci_index, cs_index)
        score, classes = score_materiality(title, body)
        bucket = (score // 10) * 10
        rep.materiality_buckets[bucket] = rep.materiality_buckets.get(bucket, 0) + 1
        if ex:
            rep.via_exchange += 1
            tier = "T1"
        elif syms:
            rep.via_alias += 1
            tier = "T2"
        elif macros:
            rep.via_macro += 1
            tier = "T3"
        else:
            rep.dropped += 1
            if len(rep.sample_dropped) < 10:
                rep.sample_dropped.append(title[:120])
            continue
        if len(rep.sample_passed) < 10:
            tags: list[str] = []
            if ex:
                tags.append(f"T1:{','.join(sorted(ex))}")
            if syms:
                tags.append(f"T2:{','.join(sorted(syms))}")
            if macros:
                tags.append(f"T3:{','.join(sorted(macros))}")
            rep.sample_passed.append((tier, title[:110], score, tags))
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--feeds", type=int, nargs="+", metavar="IDX",
                    help=f"Subset of feed indexes 0..{len(FEEDS)-1}")
    args = ap.parse_args()

    if args.feeds:
        try:
            feeds = [FEEDS[i] for i in args.feeds]
        except IndexError:
            print(f"Feed index out of range (0..{len(FEEDS)-1}).", file=sys.stderr)
            return 2
    else:
        feeds = list(FEEDS)

    ci_index, cs_index = build_alias_index()
    n_symbols = len({s for v in ci_index.values() for s in v} |
                    {s for v in cs_index.values() for s in v})
    print(f"Alias index: ci={len(ci_index)} cs={len(cs_index)} → {n_symbols} symbols")
    print(f"Macro keyword set: {len(MACRO_KEYWORDS)} terms")
    print(f"Window: last {WINDOW_DAYS} days\n")

    reports: list[FeedReport] = []
    for label, url in feeds:
        print(f"…fetching {label}", flush=True)
        rep = evaluate_feed(label, url, ci_index, cs_index)
        reports.append(rep)

    print("\n=== Reachability + volume ===")
    print(f"{'Feed':<34} {'OK':<3} {'entries':>7} {'in_win':>7} {'~/day':>7}  span")
    for r in reports:
        ok = "✓" if r.ok else "✗"
        rate = f"{r.rate_per_day:.2f}" if r.ok else "—"
        if r.ok:
            span = f"{r.span_first[:10]} → {r.span_last[:10]}"
        else:
            span = r.error[:50]
        print(f"{r.label[:34]:<34} {ok:<3} {r.n_entries:>7} {r.n_in_window:>7} {rate:>7}  {span}")

    print("\n=== Gate breakdown (per feed, items in window only) ===")
    print(f"{'Feed':<34} {'T1':>3} {'T2':>3} {'T3':>3} {'drop':>4}  pass%")
    total_pass = total_drop = 0
    for r in reports:
        if not r.ok:
            continue
        n = r.n_in_window
        passed = r.via_exchange + r.via_alias + r.via_macro
        rate = (passed / n * 100.0) if n else 0.0
        total_pass += passed
        total_drop += r.dropped
        print(f"{r.label[:34]:<34} {r.via_exchange:>3} {r.via_alias:>3} "
              f"{r.via_macro:>3} {r.dropped:>4}  {rate:5.1f}%")
    overall = (total_pass / (total_pass + total_drop) * 100.0) if (total_pass + total_drop) else 0.0
    print(f"{'TOTAL':<34} {'':>3} {'':>3} {'':>3} {'':>4}  {overall:5.1f}%")

    print("\n=== Sample PASSED (per feed, up to 5) ===")
    for r in reports:
        if not r.ok or not r.sample_passed:
            continue
        print(f"\n[{r.label}]")
        for tier, title, score, tags in r.sample_passed[:5]:
            print(f"  {tier} score={score:>3}  {title}")
            print(f"      {' '.join(tags)}")

    print("\n=== Sample DROPPED (per feed, up to 5) ===")
    for r in reports:
        if not r.ok or not r.sample_dropped:
            continue
        print(f"\n[{r.label}]")
        for title in r.sample_dropped[:5]:
            print(f"  {title}")

    print("\n=== Materiality distribution (all feeds combined) ===")
    combined: dict[int, int] = {}
    for r in reports:
        for k, v in r.materiality_buckets.items():
            combined[k] = combined.get(k, 0) + v
    for k in sorted(combined):
        print(f"  {k:>3}–{k+9:<3}: {combined[k]:>4}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
