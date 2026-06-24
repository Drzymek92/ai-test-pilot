"""Stage 6 — RECORD. Self-tracking run ledger (DuckDB).

Every generation run appends a RunRecord; `accept` backfills how many tests the human kept.
The ledger answers, deterministically: which prompt version / model tier actually produces tests
you keep, per adapter and target. Mirrors the token_usage.csv / librarian-catalog pattern. Zero
tokens. DuckDB is a lightweight embedded analytical database (a single pinned dependency).
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from scripts.core.models import RunRecord
from scripts.logger import get_logger

logger = get_logger("ledger")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          VARCHAR PRIMARY KEY,
    ts              TIMESTAMP,
    adapter         VARCHAR,
    target          VARCHAR,
    model           VARCHAR,
    prompt_version  VARCHAR,
    generated       INTEGER,
    passed          INTEGER,
    failed          INTEGER,
    triage          VARCHAR,           -- JSON {verdict: count}
    accepted        INTEGER,           -- NULL until `accept`
    tokens_in       INTEGER,
    tokens_out      INTEGER,
    cost_est        DOUBLE,
    acceptance_rate DOUBLE             -- NULL until `accept`
)
"""


def _connect(path: str | Path) -> duckdb.DuckDBPyConnection:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect(str(p))
    con.execute(_SCHEMA)
    return con


def append(record: RunRecord, path: str | Path) -> None:
    con = _connect(path)
    try:
        con.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [record.run_id, record.ts, record.adapter, record.target, record.model,
             record.prompt_version, record.generated, record.passed, record.failed,
             json.dumps(record.triage), record.accepted, record.tokens_in, record.tokens_out,
             record.cost_est, record.acceptance_rate],
        )
        logger.info("Ledger: recorded run %s (%s).", record.run_id, path)
    finally:
        con.close()


def backfill_acceptance(run_id: str, kept: int, path: str | Path) -> bool:
    """Record how many proposed tests the human kept; compute acceptance_rate. Returns found."""
    con = _connect(path)
    try:
        row = con.execute("SELECT generated FROM runs WHERE run_id = ?", [run_id]).fetchone()
        if not row:
            return False
        generated = row[0] or 0
        rate = (kept / generated) if generated else None
        con.execute("UPDATE runs SET accepted = ?, acceptance_rate = ? WHERE run_id = ?",
                    [kept, rate, run_id])
        logger.info("Ledger: run %s accepted=%d/%d (rate=%s).", run_id, kept, generated, rate)
        return True
    finally:
        con.close()


def prompt_version_stats(adapter: str, path: str | Path) -> list[tuple[str, float, int]]:
    """[(prompt_version, avg_acceptance, n_runs)] for runs that have been accepted, best first."""
    p = Path(path)
    if not p.is_file():
        return []
    con = _connect(path)
    try:
        rows = con.execute(
            "SELECT prompt_version, AVG(acceptance_rate), COUNT(*) FROM runs "
            "WHERE adapter = ? AND acceptance_rate IS NOT NULL "
            "GROUP BY prompt_version ORDER BY AVG(acceptance_rate) DESC",
            [adapter],
        ).fetchall()
        return [(r[0], float(r[1]), int(r[2])) for r in rows]
    finally:
        con.close()


def best_accepted_runs(adapter: str, target: str, path: str | Path, *,
                       min_rate: float = 0.6, limit: int = 3) -> list[tuple[str, float]]:
    """[(run_id, acceptance_rate)] for this adapter+target with acceptance >= min_rate.

    Best-and-most-recent first. Drives M5 `auto` mode: the run_ids point at persisted
    scenario JSONs to reuse as few-shot exemplars. Empty if the ledger has no accepted
    history for this target yet.
    """
    p = Path(path)
    if not p.is_file():
        return []
    con = _connect(path)
    try:
        rows = con.execute(
            "SELECT run_id, acceptance_rate FROM runs "
            "WHERE adapter = ? AND target = ? AND acceptance_rate IS NOT NULL "
            "AND acceptance_rate >= ? "
            "ORDER BY acceptance_rate DESC, ts DESC LIMIT ?",
            [adapter, target, min_rate, limit],
        ).fetchall()
        return [(r[0], float(r[1])) for r in rows]
    finally:
        con.close()


def target_acceptance(adapter: str, target: str, path: str | Path) -> tuple[float, int] | None:
    """(avg_acceptance, n) for a given target, or None if no accepted runs yet."""
    p = Path(path)
    if not p.is_file():
        return None
    con = _connect(path)
    try:
        row = con.execute(
            "SELECT AVG(acceptance_rate), COUNT(*) FROM runs "
            "WHERE adapter = ? AND target = ? AND acceptance_rate IS NOT NULL",
            [adapter, target],
        ).fetchone()
        if not row or row[1] == 0:
            return None
        return float(row[0]), int(row[1])
    finally:
        con.close()


def avg_tokens_per_scenario(adapter: str, path: str | Path) -> float | None:
    """Avg output tokens per generated scenario for an adapter (P4 budget estimation), or None.

    Uses only rows that actually recorded usage (tokens_out > 0, generated > 0)."""
    p = Path(path)
    if not p.is_file():
        return None
    con = _connect(path)
    try:
        row = con.execute(
            "SELECT AVG(CAST(tokens_out AS DOUBLE) / generated) FROM runs "
            "WHERE adapter = ? AND tokens_out > 0 AND generated > 0",
            [adapter],
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    finally:
        con.close()
