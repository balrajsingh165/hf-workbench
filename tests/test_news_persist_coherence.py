"""Tests for the cluster coherence gate in `src.news.persist`.

The gate prunes cluster members whose embeddings disagree with their peers
so the synth doesn't Frankenstein unrelated articles into one narrative.
"""

from __future__ import annotations

import math

from src.news.persist import (
    COHERENCE_MIN_PEER_SIM,
    _coherent_members,
    _select_centroid,
)
from src.news.types import ClusterSourceDoc


def _doc(news_id: str) -> ClusterSourceDoc:
    return ClusterSourceDoc(
        news_id=news_id, title=news_id, url="https://x", publisher="P", body=""
    )


def _unit(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vec))
    return [x / norm for x in vec] if norm else vec


def test_singleton_short_circuits():
    members = [_doc("n1")]
    kept, dropped = _coherent_members(members, {"n1": _unit([1.0, 0.0])})
    assert kept == members
    assert dropped == []


def test_coherent_pair_keeps_both():
    members = [_doc("n1"), _doc("n2")]
    embs = {"n1": _unit([1.0, 0.05]), "n2": _unit([1.0, 0.03])}
    kept, dropped = _coherent_members(members, embs)
    assert {m.news_id for m in kept} == {"n1", "n2"}
    assert dropped == []


def test_pair_below_threshold_drops_one():
    # Two members orthogonal → cosine ≈ 0. Worst-score == 0, gets pruned
    # iteratively until one remains.
    members = [_doc("n1"), _doc("n2")]
    embs = {"n1": _unit([1.0, 0.0]), "n2": _unit([0.0, 1.0])}
    kept, dropped = _coherent_members(members, embs)
    assert len(kept) == 1
    assert len(dropped) == 1


def test_triplet_with_one_outlier_keeps_coherent_pair():
    """n1 ≈ n2 (high sim), n3 orthogonal to both. n3 should be dropped."""
    members = [_doc("n1"), _doc("n2"), _doc("n3")]
    embs = {
        "n1": _unit([1.0, 0.1, 0.0]),
        "n2": _unit([1.0, 0.08, 0.0]),
        "n3": _unit([0.0, 0.0, 1.0]),
    }
    kept, dropped = _coherent_members(members, embs)
    assert {m.news_id for m in kept} == {"n1", "n2"}
    assert dropped == ["n3"]


def test_missing_embedding_is_safe_default():
    """Missing-embedding members score as 'no peers to compare' — we
    don't punish missing data, so they stay."""
    members = [_doc("n1"), _doc("n2")]
    embs: dict[str, list[float]] = {}  # both missing
    kept, dropped = _coherent_members(members, embs)
    assert len(kept) == 2
    assert dropped == []


def test_threshold_constant_is_in_valid_range():
    # Sanity guard against accidental misconfig (e.g. setting to 1.5).
    assert 0.0 < COHERENCE_MIN_PEER_SIM <= 1.0


def test_select_centroid_keeps_kept_member():
    survivors = [_doc("n1"), _doc("n2")]
    assert _select_centroid("n2", {"n1", "n2"}, survivors) == "n2"


def test_select_centroid_falls_back_when_centroid_was_dropped():
    """`recompute_cluster_features` set centroid to n3 before the gate
    ran. After the gate drops n3, the persisted story must NOT reference
    it — fall back to the first surviving member.
    """
    survivors = [_doc("n1"), _doc("n2")]
    assert _select_centroid("n3", {"n1", "n2"}, survivors) == "n1"


def test_select_centroid_falls_back_on_null():
    survivors = [_doc("n1"), _doc("n2")]
    assert _select_centroid(None, {"n1", "n2"}, survivors) == "n1"
    assert _select_centroid("", {"n1", "n2"}, survivors) == "n1"
