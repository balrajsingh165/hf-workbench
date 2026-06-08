"""Smoke test the two unknowns of the news-firehose plan.

Read-only. No schema changes, no DB writes. Validates:

  1. Press-wire RSS volume — do these feeds yield enough finance stories
     to extrapolate to >=500/day?
  2. Ticker-gate precision — does headline+description tagging catch real
     finance stories without drowning in noise?
  3. Alias gaps — which obviously-financial company names are NOT in the
     instruments registry?

Three-tier gate (per item, against title + first 400 chars of body):
  T1 — exchange-tagged: literal "(NASDAQ: ABCD)" / "(NYSE: XYZ)" / etc.
       Free, structural, near-zero false positives. Doesn't need registry.
  T2 — registry alias match against the 109-symbol instruments table.
  T3 — macro keyword (Fed, CPI, payrolls, GDP, ...).

Usage:
    uv run python scripts/smoke_press_wires.py
"""
from __future__ import annotations

import re
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

import feedparser

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.news.firehose_gate import (
    LAWYER_SPAM,
    build_alias_index,
    strip_html,
    tag_text,
)

PRESS_WIRE_FEEDS: tuple[str, ...] = (
    # PR Newswire — only the industry feeds confirmed to return distinct
    # content; many other "industry" URLs alias to a fallback firehose.
    "https://www.prnewswire.com/rss/news-releases-list.rss",
    "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss",
    "https://www.prnewswire.com/rss/general-business-latest-news/general-business-latest-news-list.rss",
    "https://www.prnewswire.com/rss/energy-latest-news/energy-latest-news-list.rss",
    "https://www.prnewswire.com/rss/health-latest-news/health-latest-news-list.rss",
    "https://www.prnewswire.com/rss/automotive-transportation-latest-news/automotive-transportation-latest-news-list.rss",
    "https://www.prnewswire.com/rss/consumer-technology-latest-news/consumer-technology-latest-news-list.rss",
    "https://www.prnewswire.com/rss/consumer-products-retail-latest-news/consumer-products-retail-latest-news-list.rss",
    "https://www.prnewswire.com/rss/policy-public-interest-latest-news/policy-public-interest-latest-news-list.rss",
    "https://www.prnewswire.com/rss/telecommunications-latest-news/telecommunications-latest-news-list.rss",
    # GlobeNewswire — subject-coded feeds (codes from rss/list page)
    "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies",
    "https://www.globenewswire.com/RssFeed/subjectcode/13-Earnings%20Releases%20And%20Operating%20Results/feedTitle/GlobeNewswire%20-%20Earnings",
    "https://www.globenewswire.com/RssFeed/subjectcode/27-Mergers%20and%20Acquisitions/feedTitle/GlobeNewswire%20-%20Mergers%20and%20Acquisitions",
    "https://www.globenewswire.com/RssFeed/subjectcode/7-Business%20Contracts/feedTitle/GlobeNewswire%20-%20Business%20Contracts",
    "https://www.globenewswire.com/RssFeed/subjectcode/17-Financing%20Agreements/feedTitle/GlobeNewswire%20-%20Financing%20Agreements",
    "https://www.globenewswire.com/RssFeed/subjectcode/21-Initial%20Public%20Offerings/feedTitle/GlobeNewswire%20-%20IPOs",
    "https://www.globenewswire.com/RssFeed/subjectcode/12-Dividend%20Reports%20And%20Estimates/feedTitle/GlobeNewswire%20-%20Dividends",
    "https://www.globenewswire.com/RssFeed/subjectcode/9-Company%20Announcement/feedTitle/GlobeNewswire%20-%20Company%20Announcement",
    "https://www.globenewswire.com/RssFeed/subjectcode/11-Directors%20And%20Officers/feedTitle/GlobeNewswire%20-%20Directors",
    "https://www.globenewswire.com/RssFeed/subjectcode/76-Equity%20Market%20Information/feedTitle/GlobeNewswire%20-%20Equity%20Market%20Information",
)

CAPS_PHRASE = re.compile(r"\b([A-Z][a-z]{1,}(?:\s+[A-Z][a-z]{1,}){1,3})\b")
COMMON_CAPS = frozenset({
    "First Quarter", "Second Quarter", "Third Quarter", "Fourth Quarter",
    "Annual Report", "Press Release", "New York", "Las Vegas", "United States",
    "North America", "South America", "European Union", "High School",
    "Wall Street", "Class Action", "Lead Plaintiff", "Securities Fraud",
    "Initial Public Offering", "Financial Results", "Announces Closing",
})


def extract_caps_phrases(text: str) -> list[str]:
    return [m for m in CAPS_PHRASE.findall(text) if m not in COMMON_CAPS]


def feed_rate_per_day(items: list[dict]) -> float:
    pubs = sorted(it["published"] for it in items if it.get("published"))
    if len(pubs) < 2:
        return 0.0
    try:
        t0 = datetime.fromisoformat(pubs[0])
        t1 = datetime.fromisoformat(pubs[-1])
    except ValueError:
        return 0.0
    hours = (t1 - t0).total_seconds() / 3600.0
    if hours <= 0:
        return 0.0
    return (len(pubs) - 1) / hours * 24.0


def fetch_feed(url: str, max_items: int = 100) -> tuple[str, list[dict]]:
    parsed = feedparser.parse(url)
    label = (parsed.feed.get("title") or url[:60])[:80]
    out: list[dict] = []
    for ent in (parsed.entries or [])[:max_items]:
        link = (ent.get("link") or "").strip()
        if not link:
            continue
        title = (ent.get("title") or "").strip() or link
        summary = ent.get("summary") or ent.get("description") or ""
        body = strip_html(summary)
        pub_struct = ent.get("published_parsed") or ent.get("updated_parsed")
        published: str | None = None
        if pub_struct:
            try:
                published = time.strftime("%Y-%m-%dT%H:%M:%SZ", pub_struct)
            except (TypeError, ValueError, OverflowError):
                published = None
        out.append({
            "source": label,
            "title": title,
            "body": body,
            "link": link,
            "published": published,
        })
    return label, out


def main() -> None:
    ci_index, cs_index = build_alias_index()
    n_symbols = len({s for v in ci_index.values() for s in v} |
                    {s for v in cs_index.values() for s in v})
    print(f"Alias index: ci={len(ci_index)} (multi-word + 6+char names) "
          f"cs={len(cs_index)} (ticker-shape) → {n_symbols} symbols\n")

    print(f"=== Per-feed volume ({len(PRESS_WIRE_FEEDS)} feeds) ===")
    feed_buckets: list[tuple[str, list[dict]]] = []
    for url in PRESS_WIRE_FEEDS:
        label, items = fetch_feed(url)
        feed_buckets.append((label, items))

    total_per_day = 0.0
    for label, items in feed_buckets:
        rate = feed_rate_per_day(items)
        total_per_day += rate
        pubs = sorted(it["published"] for it in items if it.get("published"))
        first = pubs[0][:16] if pubs else "—"
        last = pubs[-1][:16] if pubs else "—"
        print(f"  [{label[:32]:32}] n={len(items):3d}  "
              f"≈{rate:5.1f}/day  span={first} → {last}")

    seen_links: set[str] = set()
    deduped: list[dict] = []
    for _, items in feed_buckets:
        for it in items:
            if it["link"] in seen_links:
                continue
            seen_links.add(it["link"])
            deduped.append(it)

    print(f"\n  RAW total items   : {sum(len(b) for _, b in feed_buckets)}")
    print(f"  DEDUP unique links: {len(deduped)}")
    print(f"  Sum-of-feed-rates : ~{total_per_day:.0f}/day  (upper bound; "
          f"counts cross-listed items multiple times)")
    # Best honest estimate: count unique items published in the last 24h
    # window of the snapshot. Robust to outlier feeds (e.g. low-volume IPOs
    # feed showing 10-day-old items).
    pub_times: list[datetime] = []
    for it in deduped:
        if not it.get("published"):
            continue
        try:
            pub_times.append(datetime.fromisoformat(it["published"]))
        except ValueError:
            continue
    if pub_times:
        latest = max(pub_times)
        cutoff = latest.timestamp() - 24 * 3600
        last_24h = sum(1 for t in pub_times if t.timestamp() >= cutoff)
        print(f"  Last-24h unique items: {last_24h} (best-honest /day estimate)")
    print()

    tagged: list[tuple[str, str, set[str], set[str], set[str]]] = []
    dropped: list[tuple[str, str, str]] = []
    via_exchange = via_alias = via_macro = spam = 0
    for it in deduped:
        ex, syms, macros = tag_text(it["title"], it["body"], ci_index, cs_index)
        if ex or syms or macros:
            tagged.append((it["source"], it["title"], ex, syms, macros))
            if ex:
                via_exchange += 1
            elif syms:
                via_alias += 1
            elif macros:
                via_macro += 1
            if LAWYER_SPAM.search(it["title"]):
                spam += 1
        else:
            dropped.append((it["source"], it["title"], it["body"][:200]))

    total = len(deduped)
    pass_rate = (len(tagged) / total * 100.0) if total else 0.0
    spam_rate = (spam / len(tagged) * 100.0) if tagged else 0.0
    print("=== Gate results (deduped) ===")
    print(f"  passed         : {len(tagged):3d}  ({pass_rate:.1f}%)")
    print(f"    via exchange : {via_exchange}")
    print(f"    via alias    : {via_alias}")
    print(f"    via macro    : {via_macro}")
    print(f"    lawyer spam  : {spam} ({spam_rate:.1f}%)")
    print(f"  dropped        : {len(dropped):3d}\n")

    print("--- Sample TAGGED non-spam (up to 25) ---")
    shown = 0
    for source, title, ex, syms, macros in tagged:
        if LAWYER_SPAM.search(title):
            continue
        ex_s = f"ex={','.join(sorted(ex))}" if ex else ""
        sym_s = f"sym={','.join(sorted(syms))}" if syms else ""
        mac_s = f"macro={','.join(sorted(macros))}" if macros else ""
        tags = " ".join(t for t in [ex_s, sym_s, mac_s] if t)
        print(f"  [{source[:18]:18}] {title[:88]}\n      → {tags}")
        shown += 1
        if shown >= 25:
            break

    print("\n--- Sample DROPPED (up to 15, with body excerpt) ---")
    for source, title, body in dropped[:15]:
        print(f"  [{source[:18]:18}] {title[:90]}")
        if body:
            print(f"      …{body[:120]}")

    gaps: Counter[str] = Counter()
    for _, title, _ in dropped:
        for phrase in extract_caps_phrases(title):
            gaps[phrase] += 1
    print("\n--- Alias-gap candidates (top 20 capitalized phrases in dropped titles) ---")
    for phrase, n in gaps.most_common(20):
        print(f"  {n:2d}  {phrase}")


if __name__ == "__main__":
    main()
