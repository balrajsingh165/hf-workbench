from collections import Counter

from agents.pipeline_scheduler import SchedulerConfig
from agents.route_news_clusters import (
    DEFAULT_SYNTH_BUDGET,
    _RoutedCluster,
    _admit_promotes,
    _admit_with_diversity,
    _sort_sharp_promotes,
)
from src.news.cluster import (
    PROMOTION_MAX_AGE_H_RELAXED,
    PROMOTION_MAX_AGE_H_STRICT,
    ClusterDecisionInput,
)
from src.news.routing import Decision, route_cluster


def cluster(
    *,
    max_materiality: int = 0,
    independent_pub_count: int = 1,
    has_tier1_primary: bool = False,
    has_institutional_primary: bool = False,
    tickers: set[str] | None = None,
    sectors: set[str] | None = None,
    regions: set[str] | None = None,
    event_class: str | None = None,
    has_press_wire_primary: bool = False,
    has_non_pr_news_primary: bool = False,
    min_member_age_h: float | None = None,
) -> ClusterDecisionInput:
    return ClusterDecisionInput(
        cluster_id="cluster_test",
        status="firehose",
        max_materiality=max_materiality,
        independent_pub_count=independent_pub_count,
        has_tier1_primary=has_tier1_primary,
        has_institutional_primary=has_institutional_primary,
        tickers=tickers or set(),
        sectors=sectors or set(),
        regions=regions or set(),
        event_class=event_class,
        has_press_wire_primary=has_press_wire_primary,
        has_non_pr_news_primary=has_non_pr_news_primary,
        min_member_age_h=min_member_age_h,
    )


def test_scheduler_default_targets_feed_scale_story_volume() -> None:
    cfg = SchedulerConfig()
    assert cfg.top_stories == DEFAULT_SYNTH_BUDGET == 40
    assert cfg.route_cluster_limit == 1200
    assert cfg.synth_workers == 6


def test_admit_promotes_respects_synth_budget_before_diversity() -> None:
    promotes = [
        _RoutedCluster(
            f"cluster_{i}",
            cluster(
                max_materiality=50 - i,
                tickers={"NVDA"},
                sectors={"technology.semiconductor"},
                regions={"north_america"},
                event_class="earnings",
            ),
            Decision("sharp_promote", "R7 mainstream asset single-source"),
        )
        for i in range(5)
    ]
    admitted, overflow_budget, overflow_diversity = _admit_promotes(
        promotes, synth_budget=2
    )
    assert len(admitted) == 2
    assert len(overflow_budget) == 3
    assert len(overflow_diversity) == 0


def test_sort_sharp_promotes_prefers_corroboration_and_tier1_over_thesis_overlap() -> None:
    r3 = _RoutedCluster(
        "cluster_r3",
        cluster(max_materiality=40, tickers={"SPY"}),
        Decision("sharp_promote", "R3 active thesis ticker overlap"),
    )
    r1 = _RoutedCluster(
        "cluster_r1",
        cluster(max_materiality=35, has_tier1_primary=True),
        Decision("sharp_promote", "R1 materiality>=30 and tier1 news"),
    )
    r0c = _RoutedCluster(
        "cluster_r0c",
        cluster(max_materiality=15, independent_pub_count=6, has_tier1_primary=True),
        Decision("sharp_promote", "R0c heavy corroboration + tier1 (mat override)"),
    )

    ordered = _sort_sharp_promotes([r3, r1, r0c])

    assert [item.cluster_id for item in ordered] == [
        "cluster_r0c",
        "cluster_r1",
        "cluster_r3",
    ]


def test_scheduler_downloads_story_images_by_default() -> None:
    assert SchedulerConfig().no_images is False


def test_r7_promotes_mainstream_macro_single_source() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=55,
            tickers={"SPY", "QQQ", "^TNX"},
            sectors={"macro.rates"},
            regions={"north_america"},
            event_class="macro_print",
            has_non_pr_news_primary=True,
        ),
    )

    assert decision.route == "sharp_promote"
    assert decision.reason == "R7 mainstream asset single-source"


def test_r7_promotes_mainstream_equity_earnings_single_source() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=50,
            tickers={"NVDA"},
            sectors={"technology.semiconductor"},
            regions={"north_america"},
            event_class="earnings",
            has_non_pr_news_primary=True,
        ),
    )

    assert decision.route == "sharp_promote"
    assert decision.reason == "R7 mainstream asset single-source"


def test_r7_keeps_non_whitelisted_single_source_pr_in_firehose() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=85,
            tickers={"KLXE"},
            sectors={"energy.services"},
            regions={"north_america"},
            event_class="earnings",
            has_press_wire_primary=True,
        ),
    )

    assert decision.route == "firehose_store"


def test_r7_keeps_press_wire_with_ambiguous_mainstream_ticker_in_firehose() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=75,
            tickers={"MSTR"},
            sectors={"technology.software"},
            regions={"north_america"},
            event_class="earnings",
            has_press_wire_primary=True,
        ),
    )

    assert decision.route == "firehose_store"


def test_r7_keeps_ambiguous_alias_ticker_out_of_single_source_lane() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=50,
            tickers={"TGT"},
            sectors={"consumer.retail"},
            regions={"north_america"},
            event_class="guidance",
            has_non_pr_news_primary=True,
        ),
    )

    assert decision.route == "firehose_store"


def test_r7_rejects_routine_event_class_even_for_mainstream_asset() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=80,
            tickers={"AAPL"},
            sectors={"technology.hardware"},
            regions={"north_america"},
            event_class="conference",
            has_non_pr_news_primary=True,
        ),
    )

    assert decision.route == "firehose_store"


def test_r8_promotes_untagged_macro_commentary_single_source() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=100,
            event_class="fed_action",
            has_non_pr_news_primary=True,
        ),
    )

    assert decision.route == "sharp_promote"
    assert decision.reason == "R8 macro commentary single-source"


def test_r8_rejects_low_materiality_macro_commentary() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=45,
            event_class="macro_print",
            has_non_pr_news_primary=True,
        ),
    )

    assert decision.route == "firehose_store"


def test_r8_rejects_press_wire_macro_commentary() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=80,
            event_class="macro_print",
            has_press_wire_primary=True,
        ),
    )

    assert decision.route == "firehose_store"


def test_diversity_quota_starts_after_twenty_accepts() -> None:
    candidate = cluster(
        max_materiality=45,
        tickers={"NVDA"},
        sectors={"technology.semiconductor"},
        regions={"north_america"},
        event_class="earnings",
    )
    accepted_subjects = Counter({("technology", "north_america"): 19})

    assert _admit_with_diversity(candidate, accepted_subjects, accepted_count=19)


def test_diversity_quota_blocks_bucket_above_sixty_percent_after_start() -> None:
    candidate = cluster(
        max_materiality=45,
        tickers={"NVDA"},
        sectors={"technology.semiconductor"},
        regions={"north_america"},
        event_class="earnings",
    )
    accepted_subjects = Counter({("technology", "north_america"): 12})

    assert not _admit_with_diversity(candidate, accepted_subjects, accepted_count=20)


def test_diversity_quota_exempts_material_macro_events() -> None:
    candidate = cluster(
        max_materiality=55,
        tickers={"SPY"},
        sectors={"macro.rates"},
        regions={"north_america"},
        event_class="macro_print",
    )
    accepted_subjects = Counter({("macro", "north_america"): 20})

    assert _admit_with_diversity(candidate, accepted_subjects, accepted_count=20)


# ---------------------------------------------------------------------------
# Recency cap behavior
# ---------------------------------------------------------------------------


def _stale_strict() -> float:
    return PROMOTION_MAX_AGE_H_STRICT + 1


def _stale_relaxed() -> float:
    return PROMOTION_MAX_AGE_H_RELAXED + 1


def test_r7_rejects_stale_single_source_mainstream_promotion() -> None:
    """Regression: the GameStop/eBay case. A 9-day-old single-source CoinDesk
    article must not promote via R7 even when materiality and ticker checks
    pass."""
    decision = route_cluster(
        cluster(
            max_materiality=50,
            tickers={"GME", "EBAY"},
            sectors={"consumer.retail"},
            regions={"north_america"},
            event_class="m_a",
            has_non_pr_news_primary=True,
            min_member_age_h=_stale_strict(),
        ),
    )

    assert decision.route == "firehose_store"


def test_r7_promotes_when_within_strict_recency_window() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=50,
            tickers={"NVDA"},
            sectors={"technology.semiconductor"},
            regions={"north_america"},
            event_class="earnings",
            has_non_pr_news_primary=True,
            min_member_age_h=PROMOTION_MAX_AGE_H_STRICT - 1,
        ),
    )

    assert decision.route == "sharp_promote"
    assert decision.reason == "R7 mainstream asset single-source"


def test_r8_rejects_stale_macro_commentary() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=60,
            event_class="macro_print",
            has_non_pr_news_primary=True,
            min_member_age_h=_stale_strict(),
        ),
    )

    assert decision.route == "firehose_store"


def test_r4_rejects_stale_sharp_event_even_with_corroboration() -> None:
    # Use independent corroboration only (no tier1) so R1/R2b can't pre-empt.
    # R2 needs 3 independents; with 2 we land on R4 which has the strict cap.
    decision = route_cluster(
        cluster(
            max_materiality=40,
            event_class="m_a",
            independent_pub_count=2,
            min_member_age_h=_stale_strict(),
        ),
    )

    assert decision.route == "firehose_store"


def test_r0_institutional_rejects_stale_macro_release() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=80,
            has_institutional_primary=True,
            event_class="macro_print",
            min_member_age_h=_stale_strict(),
        ),
    )

    assert decision.route == "firehose_store"


def test_r1_tier1_accepts_within_relaxed_window_but_rejects_beyond() -> None:
    """R1 (corroborated tier-1) tolerates up to the relaxed cap, but stale
    beyond a week should not auto-promote."""
    fresh = route_cluster(
        cluster(
            max_materiality=40,
            has_tier1_primary=True,
            min_member_age_h=PROMOTION_MAX_AGE_H_RELAXED - 1,
        ),
    )
    stale = route_cluster(
        cluster(
            max_materiality=40,
            has_tier1_primary=True,
            min_member_age_h=_stale_relaxed(),
        ),
    )

    assert fresh.route == "sharp_promote"
    assert fresh.reason == "R1 materiality>=30 and tier1 news"
    assert stale.route == "firehose_store"


def test_r3_thesis_ticker_rejects_stale() -> None:
    decision = route_cluster(
        cluster(
            max_materiality=30,
            tickers={"NVDA"},
            min_member_age_h=_stale_relaxed(),
        ),
        active_thesis_tickers={"NVDA"},
    )

    assert decision.route == "firehose_store"


def test_unknown_age_does_not_block_promotion() -> None:
    """Legacy/backfill rows without a parseable timestamp must still route
    normally — the recency check is opt-in via min_member_age_h."""
    decision = route_cluster(
        cluster(
            max_materiality=50,
            tickers={"NVDA"},
            event_class="earnings",
            has_non_pr_news_primary=True,
            min_member_age_h=None,
        ),
    )

    assert decision.route == "sharp_promote"
    assert decision.reason == "R7 mainstream asset single-source"


def test_r0c_corroboration_override_promotes_low_materiality_tier1() -> None:
    """R0c: a cluster with 3+ independent publisher groups AND a Tier-1
    anchor promotes regardless of headline-regex materiality.

    Regression for the May-2026 overnight underflow: cluster_6617
    (Iran/oil/bonds, 20 members, 6 independent groups, has_tier1=True,
    materiality=15) failed every other rule because its members'
    headlines didn't match the commodity/macro regex set. The override
    catches it on corroboration strength alone — what 3 desks bother
    to cover IS material."""
    decision = route_cluster(
        cluster(
            max_materiality=15,
            independent_pub_count=6,
            has_tier1_primary=True,
            tickers={"CL=F", "USO", "XLE"},
            sectors={"macro.commodities"},
            regions={"global", "north_america"},
            min_member_age_h=2.0,
        ),
    )

    assert decision.route == "sharp_promote"
    assert decision.reason == "R0c heavy corroboration + tier1 (mat override)"


def test_r0c_requires_tier1_anchor() -> None:
    """Without a Tier-1 source, the corroboration override must NOT fire —
    keeps PR-wire echo chambers from sneaking through."""
    decision = route_cluster(
        cluster(
            max_materiality=15,
            independent_pub_count=6,
            has_tier1_primary=False,
            min_member_age_h=2.0,
        ),
    )

    assert decision.route == "firehose_store"


def test_r0c_requires_three_independent_groups() -> None:
    """Two-publisher coverage is not enough for the override — falls
    through to R1/R2b which both require materiality ≥ 25."""
    decision = route_cluster(
        cluster(
            max_materiality=15,
            independent_pub_count=2,
            has_tier1_primary=True,
            min_member_age_h=2.0,
        ),
    )

    assert decision.route == "firehose_store"


def test_r0c_rejects_stale_clusters() -> None:
    """The corroboration override still respects the relaxed-freshness
    cap — week-old multi-pub coverage shouldn't auto-promote."""
    decision = route_cluster(
        cluster(
            max_materiality=15,
            independent_pub_count=6,
            has_tier1_primary=True,
            min_member_age_h=_stale_relaxed(),
        ),
    )

    assert decision.route == "firehose_store"
