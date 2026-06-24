"""Optional project-context loader — domain semantics for value/assertion quality.

Auto-detects `agent/project.md` (preferred) or a README near the target and injects a BOUNDED
excerpt into the generation prompt. Structure ALWAYS comes from deterministic type introspection;
this context only helps the LLM choose realistic VALUES and behaviour-aware assertions (valid
categories, status lifecycles, business rules). Honors the CLAUDE.md spend gate — a bounded
excerpt, never the whole tree. Non-fatal: returns None / "" when nothing is found.

`prompt_compressor.py` is a stub here, so we truncate; wire compression in once it's populated.
"""
from __future__ import annotations

from pathlib import Path

from scripts.logger import get_logger

logger = get_logger("context")

# Nearest-first; within a directory, project.md beats README.
_CANDIDATES = ["agent/project.md", "README.md", "README.rst", "readme.md"]
_MAX_CLIMB = 6   # don't escape into unrelated parent repos


def find_project_context(target_path: str | Path) -> Path | None:
    target = Path(target_path).resolve()
    dirs = [target.parent, *target.parent.parents][:_MAX_CLIMB]
    for d in dirs:
        for rel in _CANDIDATES:
            p = d / rel
            if p.is_file():
                return p
    return None


def context_excerpt(path: Path, max_chars: int = 2000) -> str:
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit("\n", 1)[0] + "\n…(truncated for token budget)"


def context_block(path: Path, text: str) -> str:
    """The block injected into the generation prompt."""
    return (
        f"\n\n## Project domain context (from `{path.name}` — for choosing realistic VALUES and "
        "behaviour-aware assertions ONLY; the type schemas above are the source of truth for "
        f"STRUCTURE)\n{text}\n"
    )


def load_context(target_path: str | Path, *, explicit: str | None = None,
                 max_chars: int = 2000) -> tuple[str, Path | None]:
    """Return (prompt_block, source_path) or ("", None). `explicit` overrides auto-detection."""
    path = Path(explicit) if explicit else find_project_context(target_path)
    if not path or not path.is_file():
        if explicit:
            logger.warning("Context file not found: %s", explicit)
        return "", None
    text = context_excerpt(path, max_chars)
    logger.info("Using project context: %s (%d chars).", path, len(text))
    return context_block(path, text), path
