import sys
from pathlib import Path

import pytest

# Make `scripts` importable when running pytest from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


_SAMPLE_MODULE = '''\
"""A tiny module used as an introspection target in tests."""


def add(a: int, b: int = 1) -> int:
    """Add two ints."""
    return a + b


def needs_positive(n: int) -> int:
    if n < 0:
        raise ValueError("n must be >= 0")
    return n * 2


def reads_a_file(path):
    return open(path).read()
'''


@pytest.fixture
def sample_module(tmp_path: Path) -> Path:
    p = tmp_path / "sample_target.py"
    p.write_text(_SAMPLE_MODULE, encoding="utf-8")
    return p
