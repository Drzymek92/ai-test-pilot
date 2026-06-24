"""LLM gateway client — any OpenAI-compatible endpoint.

Thin wrapper over langchain-openai's ChatOpenAI pointed at an OpenAI-compatible gateway
(set LLM_BASE_URL / LLM_MODEL / LLM_API_KEY). `_require()` raises a clear, actionable error
on a missing var instead of a swallowed KeyError.
"""
import os
from pathlib import Path

from langchain_openai import ChatOpenAI
from langchain_core.messages import SystemMessage, HumanMessage

from scripts.logger import get_logger

logger = get_logger("llm_client")

DEFAULT_TEMPERATURE = 0.0

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


def get_llm(model: str | None = None, temperature: float = DEFAULT_TEMPERATURE, **kwargs) -> ChatOpenAI:
    return ChatOpenAI(
        model=model or _require("LLM_MODEL"),
        base_url=_require("LLM_BASE_URL"),
        api_key=_api_key(),
        temperature=temperature,
        **kwargs,
    )


def llm_call(prompt: str, system: str | None = None, model: str | None = None,
             temperature: float = DEFAULT_TEMPERATURE, **kwargs) -> str:
    """Raw text completion. The caller owns any JSON parsing/repair."""
    llm = get_llm(model=model, temperature=temperature, **kwargs)
    messages = []
    if system:
        messages.append(SystemMessage(content=system))
    messages.append(HumanMessage(content=prompt))
    resp = llm.invoke(messages)
    return resp.content


def smoke_test(model: str | None = None) -> str:
    """One tiny live call to verify gateway connectivity + credentials.

    Raises with a clear message on any failure; returns the model's reply on success.
    """
    return llm_call("Reply with exactly: ok", model=model)
