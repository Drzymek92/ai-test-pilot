"""P6 — defense-in-depth subprocess bounds: a hung child degrades gracefully, never wedges the tool."""
import subprocess
from pathlib import Path

from scripts.core import discover, golden


def test_golden_probe_timeout_degrades(tmp_path: Path, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="probe", timeout=k.get("timeout", 60))
    monkeypatch.setattr(golden.subprocess, "run", boom)
    captured, stderr = golden._run_probe(tmp_path / "probe.py", None, tmp_path)
    assert captured == {} and "timed out" in stderr        # no goldens locked, no crash


def test_git_changed_timeout_returns_none(tmp_path: Path, monkeypatch):
    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)
    monkeypatch.setattr(discover.subprocess, "run", boom)
    assert discover.git_changed_py(tmp_path) is None       # caller falls back to a full scan


def test_git_change_calls_pass_timeout(tmp_path: Path, monkeypatch):
    seen = {"timeouts": []}

    class _R:
        stdout = ""
    def fake(*a, **k):
        seen["timeouts"].append(k.get("timeout"))
        return _R()
    monkeypatch.setattr(discover.subprocess, "run", fake)
    discover.git_changed_py(tmp_path)
    assert seen["timeouts"] and all(t == 30 for t in seen["timeouts"])   # every git child is bounded
