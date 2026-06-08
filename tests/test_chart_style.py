"""Offline checks for the SemiAnalysis-style matplotlib helper.

Asserts the three style rules the requirement spelled out:
  - No vertical grid lines.
  - Horizontal grid lines only.
  - No black border (no spines).

Plus dark/light parity (same rule set, only colors swap).
"""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")  # headless

import matplotlib.pyplot as plt
import pytest

from src.agent.chart_style import apply_style, finalize_figure, palette, style_axes


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_apply_style_no_spines(theme):
    apply_style(theme)
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3, 4, 5], [1, 2, 1, 3, 2])
    finalize_figure(fig)
    for side in ("top", "right", "bottom", "left"):
        assert not ax.spines[side].get_visible(), f"{theme}: spine {side} visible"
    plt.close(fig)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_apply_style_no_vertical_grid(theme):
    apply_style(theme)
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [1, 2, 3])
    finalize_figure(fig)
    # x-axis grid must be off (no vertical grid lines)
    assert not ax.xaxis._major_tick_kw.get("gridOn", True), f"{theme}: vertical grid on"
    plt.close(fig)


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_apply_style_horizontal_grid_on(theme):
    apply_style(theme)
    fig, ax = plt.subplots()
    ax.plot([1, 2, 3], [1, 2, 3])
    finalize_figure(fig)
    assert ax.yaxis._major_tick_kw.get("gridOn", False), f"{theme}: horizontal grid off"
    plt.close(fig)


def test_palette_dark_vs_light_differ():
    d = palette("dark")
    l = palette("light")
    assert d["bg"] != l["bg"]
    assert d["fg"] != l["fg"]
    assert d["series"] != l["series"]
    assert set(d.keys()) == set(l.keys())  # parity of rule set


def test_style_axes_idempotent():
    apply_style("dark")
    fig, ax = plt.subplots()
    style_axes(ax)
    style_axes(ax)
    for side in ("top", "right", "bottom", "left"):
        assert not ax.spines[side].get_visible()
    plt.close(fig)
