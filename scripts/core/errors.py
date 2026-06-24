"""Typed pipeline errors → distinct, documented CLI exit codes (P2 fail-safe).

Kept tiny and dependency-free so every stage can raise these without import cycles.
The exit-code contract lives in `main.py` (and LIMITATIONS.md); these classes are how a
stage signals which contract code applies instead of a generic crash.
"""
from __future__ import annotations


class PipelineError(Exception):
    """Base for all expected, classified pipeline failures."""


class TargetError(PipelineError):
    """The target module can't be introspected — missing, unreadable, or a syntax error.

    Maps to a clean 'skip with reason' (exit 3), never a stack trace: a malformed target
    is a normal outcome when sweeping a project, not a tool bug.
    """


class LLMError(PipelineError):
    """The LLM call failed after retries/timeout (network, gateway, or timeout).

    Maps to exit 4. Raised only after the configured retries are exhausted; the tool never
    half-generates on a transport failure.
    """


class BudgetError(PipelineError):
    """A run/sweep estimate exceeded the token budget cap with `on_over='abort'` (P4).

    Maps to exit 6. A deliberate guardrail stop — not a crash — so a sweep can't silently
    overspend. With `on_over='warn'` (the default) the tool logs and proceeds instead.
    """
