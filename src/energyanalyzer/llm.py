"""Shared local-LLM (Ollama) client.

The project uses a local Ollama model (default ``lfm2.5``) as a **best-effort,
optional** tier: deterministic code does the load-bearing work, and the LLM only
helps with genuinely fuzzy judgement calls the deterministic path can't settle
cleanly -- adjudicating whether two differently-named plan listings are the same
plan (``app.common`` dedup), and proposing structured fields for an EFL the
static parser couldn't read confidently (``eflparse.llm_repair``).

Everything here degrades gracefully: if Ollama isn't running or the model isn't
pulled, callers get an exception (or ``None`` from :func:`chat_json`) and fall
back to their deterministic behaviour -- the app never *depends* on the LLM.

Test seam: pass ``chat_fn`` to :func:`chat_json` -- ``(messages, model,
ollama_url, timeout) -> response_dict`` -- so tests never touch the network.
"""

from __future__ import annotations

import json
import logging
from typing import Callable, Optional

logger = logging.getLogger(__name__)

OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_MODEL = "lfm2.5"
# Ollama's model-list endpoint, derived from OLLAMA_URL, for the availability
# probe (a cheap GET that doesn't run inference).
OLLAMA_TAGS_URL = OLLAMA_URL.replace("/api/chat", "/api/tags")


def ollama_chat(
    messages: list[dict],
    model: str = OLLAMA_MODEL,
    ollama_url: str = OLLAMA_URL,
    timeout: float = 60.0,
) -> dict:
    """POST a chat completion to Ollama with JSON-forced output. Raises on any
    transport/HTTP error (server down, model not pulled)."""
    import httpx

    payload = {"model": model, "messages": messages, "stream": False, "format": "json"}
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(ollama_url, json=payload)
        resp.raise_for_status()
        return resp.json()


def chat_json(
    messages: list[dict],
    model: str = OLLAMA_MODEL,
    ollama_url: str = OLLAMA_URL,
    timeout: float = 60.0,
    chat_fn: Optional[Callable[[list[dict], str, str, float], dict]] = None,
) -> Optional[dict]:
    """Run a chat completion and return the model's JSON content as a dict, or
    ``None`` if the call fails or the content isn't valid JSON.

    Never raises -- this is the graceful entry point callers use so a missing/
    broken Ollama simply means "no LLM help this time", not a crashed refresh.
    """
    fn = chat_fn or ollama_chat
    try:
        resp = fn(messages, model, ollama_url, timeout)
    except Exception as exc:  # noqa: BLE001 -- LLM tier is best-effort
        logger.info("LLM call failed (%s, model=%s): %r", ollama_url, model, exc)
        return None
    content = (resp or {}).get("message", {}).get("content", "")
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        logger.info("LLM returned non-JSON content despite format=json: %r", content[:200])
        return None
    return parsed if isinstance(parsed, dict) else None


def available(ollama_url: str = OLLAMA_URL, timeout: float = 3.0) -> bool:
    """Best-effort check that an Ollama server is reachable (a cheap GET to
    /api/tags, no inference). Used to skip LLM stages up front when it's down."""
    import httpx

    tags_url = ollama_url.replace("/api/chat", "/api/tags")
    try:
        with httpx.Client(timeout=timeout) as client:
            return client.get(tags_url).status_code == 200
    except Exception:  # noqa: BLE001
        return False
