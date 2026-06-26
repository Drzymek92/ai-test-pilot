"""Tests for the LOCKED HumanEval holdout — loader + isolation, fully offline (no LLM, no network).

The kill-check mechanic itself is shared with `detect` and covered by `test_detection.py`. Here we
verify (1) the loader parses/filters/subsets deterministically, (2) `ensure_corpus` is non-fatal when
the corpus is absent, and (3) — the design-critical invariant — `run_holdout` writes ONLY to
benchmark/eval/holdout/ and NEVER touches the dev series (detection_baseline.json / EVAL_DETECTION.md).
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

from benchmark.corpora import humaneval as he
from scripts.config import load_config
from scripts.core import detection

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


# ── loader ─────────────────────────────────────────────────────────────────────
def _write_jsonl_gz(path: Path, records: list[dict]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec) + "\n")


_RECORDS = [
    # self-contained, defines entry point -> kept
    {"task_id": "HumanEval/2", "entry_point": "inc",
     "prompt": "def inc(x):\n    ", "canonical_solution": "return x + 1\n"},
    {"task_id": "HumanEval/0", "entry_point": "add",
     "prompt": "import math\n\n\ndef add(a, b):\n    ", "canonical_solution": "return a + b\n"},
    # third-party import -> dropped (not self-contained)
    {"task_id": "HumanEval/1", "entry_point": "frame",
     "prompt": "import pandas as pd\n\n\ndef frame():\n    ", "canonical_solution": "return pd.DataFrame()\n"},
    # entry point not defined -> dropped
    {"task_id": "HumanEval/3", "entry_point": "missing",
     "prompt": "def other():\n    ", "canonical_solution": "return 1\n"},
]


def test_self_contained_and_defines_filters():
    assert he._is_self_contained("import math\n\ndef f():\n    return math.pi\n")
    assert not he._is_self_contained("import pandas\n\ndef f():\n    return 1\n")
    assert he._defines("def f():\n    return 1\n", "f")
    assert not he._defines("def g():\n    return 1\n", "f")


def test_task_num_orders_numerically():
    assert he._task_num("HumanEval/0") == 0
    assert he._task_num("HumanEval/12") == 12
    assert he._task_num("weird") == 1 << 30


def test_load_problems_filters_and_sorts(tmp_path: Path):
    data = tmp_path / "HumanEval.jsonl.gz"
    _write_jsonl_gz(data, _RECORDS)
    problems = he.load_problems(data)
    # pandas import + undefined-entry records dropped; remaining sorted by task number (0 before 2)
    assert [p.task_id for p in problems] == ["HumanEval/0", "HumanEval/2"]
    assert problems[0].name == "add"
    assert problems[0].source == "import math\n\n\ndef add(a, b):\n    return a + b\n"


def test_subset_is_a_prefix(tmp_path: Path):
    data = tmp_path / "HumanEval.jsonl.gz"
    _write_jsonl_gz(data, _RECORDS)
    one = he.load_problems(data, max_count=1)
    two = he.load_problems(data, max_count=2)
    assert [p.task_id for p in one] == ["HumanEval/0"]            # smaller subset...
    assert [p.task_id for p in two][:1] == [p.task_id for p in one]  # ...is a prefix of the larger


def test_ensure_corpus_no_clone_when_absent(tmp_path: Path):
    assert he.ensure_corpus(tmp_path, clone=False) is None


def test_ensure_corpus_finds_existing_data_file(tmp_path: Path):
    repo = tmp_path / "human-eval" / "data"
    repo.mkdir(parents=True)
    data = repo / "HumanEval.jsonl.gz"
    _write_jsonl_gz(data, _RECORDS[:1])
    assert he.ensure_corpus(tmp_path, clone=False) == data        # found without cloning


# ── isolation: run_holdout must not touch the dev series ─────────────────────────
def _snapshot(path: Path):
    return path.read_bytes() if path.is_file() else None


def test_run_holdout_writes_only_holdout_and_leaves_dev_series_untouched(tmp_path, monkeypatch):
    """run_holdout writes a json+md under benchmark/eval/holdout/ and never writes the dev baseline /
    EVAL_DETECTION ledger — the anti-overfit invariant. Generation is stubbed (offline)."""
    canned = {"available": True, "problems": 2, "problems_with_green": 2,
              "mutants": 5, "killed": 4, "equivalent": 1, "kill_rate": 0.8,
              "cases": [{"task_id": "HumanEval/0", "name": "add", "green": 3,
                         "mutants": 5, "killed": 4, "equivalent": 1}]}
    monkeypatch.setattr(detection, "_run_humaneval", lambda *a, **k: canned)

    baseline = _PROJECT_ROOT / "benchmark" / "detection_baseline.json"
    ledger = _PROJECT_ROOT / "benchmark" / "eval" / "EVAL_DETECTION.md"
    before = (_snapshot(baseline), _snapshot(ledger))

    cfg = load_config(None)
    result = detection.run_holdout(cfg, _PROJECT_ROOT, subset=2, clone=False)

    assert result["locked"] is True
    assert result["kill_rate"] == 0.8
    report = Path(result["report_json"])
    assert report.is_file() and "holdout" in report.parts[-1]
    assert report.with_suffix(".md").is_file()
    assert "/eval/holdout/" in report.as_posix()
    # the dev series is byte-for-byte unchanged
    assert (_snapshot(baseline), _snapshot(ledger)) == before
    # cleanup the test's own holdout artifacts
    report.unlink(missing_ok=True)
    report.with_suffix(".md").unlink(missing_ok=True)
