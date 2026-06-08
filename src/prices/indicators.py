"""Technical indicator computation over bar arrays.

All functions accept a list of close prices (floats) and return arrays of the
same length with None in warm-up positions. Inputs are assumed to be already
aligned and free of NaN.
"""

from __future__ import annotations

import math
from typing import NamedTuple


def sma(closes: list[float], period: int) -> list[float | None]:
    out: list[float | None] = [None] * len(closes)
    for i in range(period - 1, len(closes)):
        out[i] = sum(closes[i - period + 1 : i + 1]) / period
    return out


def ema(closes: list[float], period: int) -> list[float | None]:
    if len(closes) < period:
        return [None] * len(closes)
    out: list[float | None] = [None] * len(closes)
    k = 2.0 / (period + 1)
    # Seed with SMA of first `period` values.
    seed = sum(closes[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(closes)):
        val = closes[i] * k + prev * (1 - k)
        out[i] = val
        prev = val
    return out


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    if len(closes) <= period:
        return [None] * len(closes)
    out: list[float | None] = [None] * len(closes)
    gains: list[float] = []
    losses: list[float] = []
    for i in range(1, period + 1):
        delta = closes[i] - closes[i - 1]
        gains.append(max(delta, 0.0))
        losses.append(max(-delta, 0.0))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100 - (100 / (1 + rs))
    for i in range(period + 1, len(closes)):
        delta = closes[i] - closes[i - 1]
        gain = max(delta, 0.0)
        loss = max(-delta, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100 - (100 / (1 + rs))
    return out


class IndicatorSet(NamedTuple):
    rsi_14: list[float | None]
    sma_20: list[float | None]
    sma_50: list[float | None]
    sma_200: list[float | None]
    ema_20: list[float | None]
    ema_50: list[float | None]
    ema_200: list[float | None]
    disabled: list[str]
    disabled_reason: str | None


def compute(closes: list[float]) -> IndicatorSet:
    """Compute the v1 indicator set over the full closes array.

    The caller is responsible for passing a warm-up window; this function slices
    nothing — it returns arrays the same length as `closes`.
    """
    disabled: list[str] = []
    reason: str | None = None

    def _try(fn, *args):
        result = fn(closes, *args)
        if all(v is None for v in result):
            return None
        return result

    rsi14 = _try(rsi, 14)
    s20 = _try(sma, 20)
    s50 = _try(sma, 50)
    s200 = _try(sma, 200)
    e20 = _try(ema, 20)
    e50 = _try(ema, 50)
    e200 = _try(ema, 200)

    for name, val in [("sma_200", s200), ("ema_200", e200)]:
        if val is None:
            disabled.append(name)
    if disabled:
        reason = "insufficient_history"

    empty: list[float | None] = [None] * len(closes)
    return IndicatorSet(
        rsi_14=rsi14 or empty,
        sma_20=s20 or empty,
        sma_50=s50 or empty,
        sma_200=s200 or empty,
        ema_20=e20 or empty,
        ema_50=e50 or empty,
        ema_200=e200 or empty,
        disabled=disabled,
        disabled_reason=reason,
    )
