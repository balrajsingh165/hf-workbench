"""SemiAnalysis-style matplotlib helper for the chart agent.

Style spec (from the requirement):
  - No vertical grid lines.
  - Horizontal grid lines only.
  - No black border (no spines).
  - Dark mode + light mode share the rule set; only colors swap.

Counter-example to avoid: default matplotlib charts (heavy black bbox + full
grid). Reference image: https://newsletter.semianalysis.com/p/the-great-ai-silicon-shortage

This module is intentionally side-effect free at import time so it can be
read off disk and `writeFiles`d into the AgentCore Code Interpreter sandbox
verbatim.
"""

from __future__ import annotations

from typing import Literal


# Two palettes. Same rules; only colors differ.
_DARK = {
    "bg": "#0E0F11",
    "fg": "#E6E8EA",
    "muted": "#8A8F95",
    "grid": "#2A2D31",
    "series": [
        "#C0FF00",  # Heurist lime
        "#5FB3FF",
        "#FF7A59",
        "#A78BFA",
        "#34D399",
        "#F472B6",
        "#FBBF24",
        "#94A3B8",
    ],
}

_LIGHT = {
    "bg": "#FFFFFF",
    "fg": "#111418",
    "muted": "#5B6168",
    "grid": "#E3E6EA",
    "series": [
        "#0B7A3B",
        "#1E5DBF",
        "#C4502A",
        "#6E47C7",
        "#0F8F6E",
        "#B83A86",
        "#A77410",
        "#3F4750",
    ],
}


def palette(theme: Literal["dark", "light"] = "dark") -> dict:
    return _DARK if theme == "dark" else _LIGHT


def apply_style(theme: Literal["dark", "light"] = "dark") -> None:
    """Apply the SemiAnalysis-style rcParams. Call once before plotting.

    Sets color palette + sane typography defaults. Per-axes rules
    (no spines, no vertical grid, horizontal-only grid) are also applied
    by `style_axes()` for any axes that exist after plotting — call that
    after creating each axes, or call `finalize_figure()` at the end.
    """
    import matplotlib as mpl

    p = palette(theme)
    mpl.rcParams.update(
        {
            "figure.facecolor": p["bg"],
            "axes.facecolor": p["bg"],
            "savefig.facecolor": p["bg"],
            "savefig.edgecolor": p["bg"],
            "text.color": p["fg"],
            "axes.labelcolor": p["fg"],
            "axes.titlecolor": p["fg"],
            "xtick.color": p["muted"],
            "ytick.color": p["muted"],
            "axes.edgecolor": p["bg"],          # no visible bbox
            "axes.linewidth": 0,
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.spines.left": False,
            "axes.spines.bottom": False,
            "axes.grid": True,
            "axes.grid.axis": "y",              # horizontal only
            "grid.color": p["grid"],
            "grid.linestyle": "-",
            "grid.linewidth": 0.8,
            "grid.alpha": 0.6,
            "axes.prop_cycle": mpl.cycler(color=p["series"]),
            "axes.titlesize": 13,
            "axes.titleweight": "semibold",
            "axes.labelsize": 10,
            "xtick.labelsize": 9,
            "ytick.labelsize": 9,
            "legend.frameon": False,
            "legend.fontsize": 9,
            "font.family": ["DejaVu Sans"],
            "figure.figsize": (8.0, 4.5),
            "figure.dpi": 110,
            "savefig.dpi": 144,
            "savefig.bbox": "tight",
            "savefig.pad_inches": 0.25,
        }
    )


def style_axes(ax) -> None:
    """Per-axes hardening. Idempotent. Call after each `ax = ...` if needed."""
    for side in ("top", "right", "bottom", "left"):
        ax.spines[side].set_visible(False)
    ax.xaxis.grid(False)
    ax.yaxis.grid(True, linestyle="-", alpha=0.6)
    ax.tick_params(axis="both", which="both", length=0)
    ax.set_axisbelow(True)


def finalize_figure(fig) -> None:
    """Apply per-axes rules to every axes in the figure. Call right before save."""
    for ax in fig.get_axes():
        style_axes(ax)


__all__ = ["apply_style", "style_axes", "finalize_figure", "palette"]
