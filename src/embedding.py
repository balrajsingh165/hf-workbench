"""Shared embedding utilities.

Keep this module tiny — it exists so `src/story/match_index.py` and
`src/thesis/match_index.py` don't carry divergent copies of the same
numeric helpers.
"""

from __future__ import annotations

import math


def cosine_similarity(left: list[float], right: list[float]) -> float:
    numerator = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(a * a for a in left))
    right_norm = math.sqrt(sum(b * b for b in right))
    if left_norm == 0 or right_norm == 0:
        return 0.0
    return numerator / (left_norm * right_norm)


__all__ = ["cosine_similarity"]
