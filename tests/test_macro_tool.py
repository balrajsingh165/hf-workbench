from __future__ import annotations

from typing import Any

import pytest
from fastapi import HTTPException

from app import _compact_macro_point, get_macro
from src.agent.tools import _dispatch


def _history_payload(series_key: str, view: str) -> dict[str, Any]:
    return {
        "status": "success",
        "data": {
            "series": {
                "key": series_key,
                "series_id": series_key.upper(),
                "title": f"{series_key} title",
                "pillar": "rates",
                "frequency": "Monthly",
                "units": "Percent",
            },
            "resolved_window": {"period": "2y"},
            "view": view,
            "observations": [
                {"date": f"2026-0{i}-01", "value": float(i), "unit": "pct"}
                for i in range(1, 7)
            ],
            "latest_summary": {"date": "2026-06-01", "value": 6.0, "unit": "pct"},
            "point_in_time_safe": False,
        },
    }


def test_compact_macro_point_percent_level() -> None:
    assert _compact_macro_point(
        {"date": "2026-05-06", "value": 4.36, "unit": "Percent"}
    ) == {"date": "2026-05-06", "value": "4.36%"}


def test_compact_macro_point_pct_view() -> None:
    assert _compact_macro_point(
        {
            "date": "2026-01-01",
            "value": 3.0557,
            "unit": "pct",
            "comparison_date": "2025-01-01",
        }
    ) == {
        "date": "2026-01-01",
        "value": "3.0557%",
        "comparison_date": "2025-01-01",
    }


def test_compact_macro_point_drops_redundant_unit_for_index() -> None:
    assert _compact_macro_point(
        {
            "date": "2026-02-01",
            "value": 327.46,
            "unit": "Index 1982-1984=100",
        }
    ) == {"date": "2026-02-01", "value": 327.46}


def test_get_macro_requires_explicit_series() -> None:
    with pytest.raises(HTTPException) as exc:
        get_macro(series_keys=None)

    assert exc.value.status_code == 400
    assert "requires explicit series" in str(exc.value.detail)


def test_get_macro_uses_history_only(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_fred_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, arguments))
        assert tool_name == "macro_series_history"
        return _history_payload(arguments["series_key"], arguments.get("view", "level"))

    monkeypatch.setattr("src.clients.mesh.fred_tool", fake_fred_tool)

    result = get_macro(
        series_keys=["core_cpi", "core_cpi", "ust_10y"],
        views=["yoy", "mom_annualized", "level"],
        limit=12,
    )

    assert [name for name, _args in calls] == [
        "macro_series_history",
        "macro_series_history",
        "macro_series_history",
    ]
    assert [s["view"] for s in result.series] == ["yoy", "mom_annualized", "level"]
    assert all(len(s["observations"]) >= 5 for s in result.series)
    level_obs = next(s["observations"][0] for s in result.series if s["view"] == "level")
    assert level_obs == {"date": "2026-01-01", "value": "1%"}
    yoy_obs = next(s["observations"][0] for s in result.series if s["view"] == "yoy")
    assert yoy_obs["value"] == "1%"
    assert "unit" not in yoy_obs
    assert result.note is None


def test_search_macro_empty_args_rejected() -> None:
    result = _dispatch("search_macro", {}, user_id="user_1")

    assert "note" in result
    assert "requires non-empty series specs" in result["note"]


def test_search_macro_dispatches_series_specs(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def fake_fred_tool(tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool_name, arguments))
        return _history_payload(arguments["series_key"], arguments.get("view", "level"))

    monkeypatch.setattr("src.clients.mesh.fred_tool", fake_fred_tool)

    result = _dispatch(
        "search_macro",
        {
            "series": [
                {"series_key": "core_cpi", "view": "yoy"},
                {"series_key": "ust_10y", "view": "level"},
            ],
            "limit": 12,
        },
        user_id="user_1",
    )

    assert [args["series_key"] for _name, args in calls] == ["core_cpi", "ust_10y"]
    assert [s["series_key"] for s in result["series"]] == ["core_cpi", "ust_10y"]
    assert result["series"][1]["observations"][0]["value"] == "1%"
    assert "unit" not in result["series"][1]["observations"][0]
    assert "regime" not in result
    assert "releases" not in result
