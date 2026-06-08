"""Shared primitives for the cluster → story pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ClusterSourceDoc:
    news_id: str
    title: str
    url: str
    publisher: str
    body: str
    published: str | None = None
    tickers: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)


__all__ = ["ClusterSourceDoc"]
