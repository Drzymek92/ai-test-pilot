"""Before/after run comparison — classify each keyed result as regressed, improved,
unchanged, added, or removed.

The single home for "did this change make a specific case worse?" logic, promoted
under the rule of three. Two users with different value types:
  - the skill eval harness (`skills/helpers/eval_runner.py`) — boolean pass/fail per
    case, via `pass_fail_regressions`;
  - ai-test-pilot's benchmark comparator (`projects/ai-test-pilot/benchmark/compare.py`)
    — numeric coverage % per file, via `metric_regressions`.

Project-agnostic, stdlib-only, type-hinted; uses `logging.getLogger(__name__)` so the
caller's logging config applies.
"""
from __future__ import annotations

import logging
from typing import Callable, Hashable, Mapping, TypeVar

logger = logging.getLogger(__name__)

K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


def classify_changes(
    before: Mapping[K, V],
    after: Mapping[K, V],
    *,
    regressed: Callable[[V, V], bool],
    improved: Callable[[V, V], bool] | None = None,
) -> dict[str, list[K]]:
    """Compare two keyed result maps key-by-key.

    `regressed(old, new)` and the optional `improved(old, new)` decide the direction
    for keys present in BOTH maps. A key only in `after` is ``added``; only in
    `before` is ``removed`` (so a case that stopped being evaluated never counts as a
    regression). Returns a dict of sorted key lists:
    ``regressed`` / ``improved`` / ``unchanged`` / ``added`` / ``removed``.
    """
    buckets: dict[str, list[K]] = {
        k: [] for k in ("regressed", "improved", "unchanged", "added", "removed")
    }
    for key in set(before) | set(after):
        in_before, in_after = key in before, key in after
        if in_before and not in_after:
            buckets["removed"].append(key)
        elif in_after and not in_before:
            buckets["added"].append(key)
        else:
            old, new = before[key], after[key]
            if regressed(old, new):
                buckets["regressed"].append(key)
            elif improved is not None and improved(old, new):
                buckets["improved"].append(key)
            else:
                buckets["unchanged"].append(key)
    return {name: sorted(keys, key=str) for name, keys in buckets.items()}


def pass_fail_regressions(
    before: Mapping[K, bool], after: Mapping[K, bool]
) -> dict[str, list[K]]:
    """Boolean pass/fail maps: a regression is ``pass -> fail`` (truthy -> falsy);
    an improvement is ``fail -> pass``."""
    return classify_changes(
        before, after,
        regressed=lambda o, n: bool(o) and not bool(n),
        improved=lambda o, n: not bool(o) and bool(n),
    )


def metric_regressions(
    before: Mapping[K, float], after: Mapping[K, float], *, min_delta: float = 0.0
) -> dict[str, list[K]]:
    """Numeric maps (e.g. coverage %): a regression is a drop greater than
    ``min_delta``; an improvement is a rise greater than ``min_delta``. Equal-within-
    tolerance values are ``unchanged``."""
    return classify_changes(
        before, after,
        regressed=lambda o, n: (o - n) > min_delta,
        improved=lambda o, n: (n - o) > min_delta,
    )
