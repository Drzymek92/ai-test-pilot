"""LLM gateway client — any OpenAI-compatible endpoint.

Thin wrapper over langchain-openai's ChatOpenAI pointed at an OpenAI-compatible gateway
(set LLM_BASE_URL / LLM_MODEL / LLM_API_KEY). Key behaviours:
  - Loads config/.env so a standalone run picks up credentials.
  - `_require()` raises a clear, actionable error on a missing var instead of a swallowed KeyError.
  - Owns its own timeout + exponential-backoff retry so failures are logged and classified (P2),
    and surfaces token usage for spend accounting (P4).
"""
import os
import time
from pathlib import Path

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from scripts.core.errors import LLMError
from scripts.logger import get_logger

logger = get_logger("llm_client")

DEFAULT_TEMPERATURE = 0.0
DEFAULT_TIMEOUT = 60.0     # seconds per LLM request (P2)
DEFAULT_RETRIES = 2        # transient-failure retries after the first attempt (P2)
_BACKOFF_BASE = 2.0        # seconds; doubles each retry

# Load env from config/.env (project root, regardless of cwd) so a standalone run picks up
# credentials. dotenv does not override vars already set, so an explicit environment wins.
try:
    from dotenv import load_dotenv

    _here = Path(__file__).resolve()
    for _p in (Path("config") / ".env", _here.parents[1] / "config" / ".env"):
        if _p.is_file():
            load_dotenv(_p)
except Exception:
    pass


def _require(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"{name} not set. Add it to config/.env (see config/.env.example)."
        )
    return val


def _api_key() -> str:
    key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "LLM API key not set. Add LLM_API_KEY to config/.env "
            "(see config/.env.example)."
        )
    return key


def resolve_model(model: str | None = None) -> str:
    """The effective model name, without raising — for cache keys / logging (H1).

    Returns the explicit override, else the configured LLM_MODEL, else "" (so a key is
    still stable even before env is loaded). Use `_require("LLM_MODEL")` when a live call
    actually needs the value.
    """
    return model or os.environ.get("LLM_MODEL") or ""


def get_llm(model: str | None = None, temperature: float = DEFAULT_TEMPERATURE,
            timeout: float | None = DEFAULT_TIMEOUT, **kwargs) -> ChatOpenAI:
    # max_retries=0: this module owns retry/backoff (so we can log + classify), not langchain.
    return ChatOpenAI(
        model=model or _require("LLM_MODEL"),
        base_url=_require("LLM_BASE_URL"),
        api_key=_api_key(),
        temperature=temperature,
        timeout=timeout,
        max_retries=0,
        **kwargs,
    )


def _extract_usage(msg) -> dict[str, int]:
    """Best-effort token usage from an AIMessage (P4-1). OpenAI-compatible gateways populate
    `usage_metadata`; fall back to `response_metadata['token_usage']`; else zeros."""
    um = getattr(msg, "usage_metadata", None)
    if um:
        return {"input_tokens": int(um.get("input_tokens", 0)),
                "output_tokens": int(um.get("output_tokens", 0))}
    tu = (getattr(msg, "response_metadata", None) or {}).get("token_usage") or {}
    return {"input_tokens": int(tu.get("prompt_tokens", 0)),
            "output_tokens": int(tu.get("completion_tokens", 0))}


def llm_call(prompt: str, system: str | None = None, model: str | None = None,
             temperature: float = DEFAULT_TEMPERATURE, timeout: float | None = DEFAULT_TIMEOUT,
             retries: int = DEFAULT_RETRIES, return_usage: bool = False, **kwargs):
    """Raw text completion with timeout + exponential-backoff retry (P2).

    Retries transient failures (network/gateway/timeout) up to `retries` times, then raises
    LLMError — the caller never sees a half-result. The caller owns any JSON parsing/repair.
    Returns the text, or `(text, {input_tokens, output_tokens})` when `return_usage=True` (P4-1).
    """
    llm = get_llm(model=model, temperature=temperature, timeout=timeout, **kwargs)
    messages = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))

    last_err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            msg = llm.invoke(messages)
            return (msg.content, _extract_usage(msg)) if return_usage else msg.content
        except Exception as exc:                       # noqa: BLE001 — classify, then retry/raise
            last_err = exc
            if attempt < retries:
                delay = _BACKOFF_BASE * (2 ** attempt)
                logger.warning("LLM call failed (attempt %d/%d): %s -- retrying in %.0fs.",
                               attempt + 1, retries + 1, exc, delay)
                time.sleep(delay)
            else:
                logger.error("LLM call failed after %d attempt(s): %s", retries + 1, exc)
    raise LLMError(f"LLM call failed after {retries + 1} attempt(s): {last_err}") from last_err


def smoke_test(model: str | None = None) -> str:
    """One tiny live call to verify gateway connectivity + credentials.

    Raises with a clear message on any failure; returns the model's reply on success.
    """
    return llm_call("Reply with exactly: ok", model=model)
