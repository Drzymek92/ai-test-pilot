"""Scenario cache / lock — reproducibility (P1, resolves G2).

The one nondeterministic stage is generation. Keyed by everything that determines the LLM
output — the exact system + human prompt (which already embeds the target source via the
contract block, the prompt version, context/fixture/few-shot blocks), the resolved model
*version* (H1: model drift must invalidate), the temperature, and the scenario count — a
re-run on an unchanged target replays the same scenarios instead of paying for (and risking
drift from) a fresh call. Any change to a key component misses and regenerates.

Deterministic, stdlib-only, zero tokens. The cache is an optimisation + a determinism guard,
never correctness-critical: a corrupt/unreadable entry simply misses.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from scripts.core.models import ScenarioSet
from scripts.logger import get_logger

logger = get_logger("cache")

_SEP = "\x00"


def cache_key(*, system: str, human: str, model: str, temperature: float, count: int) -> str:
    """Stable sha256 over the full generation inputs. `model` should be the RESOLVED name."""
    payload = _SEP.join([system, human, model or "", f"{temperature:.4f}", str(count)])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _path(cache_dir: Path, key: str) -> Path:
    return cache_dir / f"{key}.json"


def load(cache_dir: Path, key: str) -> ScenarioSet | None:
    """Return the cached ScenarioSet for `key`, or None on miss/corrupt entry."""
    p = _path(cache_dir, key)
    if not p.is_file():
        return None
    try:
        ss = ScenarioSet.model_validate_json(p.read_text(encoding="utf-8"))
        logger.info("Cache hit %s -- replaying %d scenario(s), no LLM call.", key[:12], len(ss.scenarios))
        return ss
    except Exception as exc:                           # noqa: BLE001 — a bad entry is a miss
        logger.warning("Cache entry %s unreadable (%s) -- regenerating.", key[:12], exc)
        return None


def store(cache_dir: Path, key: str, scenario_set: ScenarioSet) -> Path:
    """Persist a freshly-generated ScenarioSet (write-temp-then-rename)."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    p = _path(cache_dir, key)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(scenario_set.model_dump_json(indent=2), encoding="utf-8")
    tmp.replace(p)
    logger.info("Cached scenarios under %s.", key[:12])
    return p
