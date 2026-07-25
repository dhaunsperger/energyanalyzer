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
# Model choice is MEASURED, not assumed -- see scripts/eval_efl.py, which scores
# each candidate against the hand-verified corpus in
# tests/fixtures/efl_texts/real/ground_truth.yaml. Results on the dev box
# (RTX 3070 8 GB, WSL capped at ~7 GB system RAM), parser + LLM suggestion tier,
# at the deterministic settings in _OPTIONS below:
#
#   model             load-bearing correct   silent-wrong   speed/EFL
#   ---------------   -------------------   ------------   ---------
#   (parser alone)         99/100                 0            --
#   gemma3:4b             100/100                 0           0.9s
#   qwen3:4b              100/100                 0          13.9s
#   granite4:micro         99/100                 1           1.0s
#   lfm2.5-thinking        98/100                 1           3.1s
#
# gemma3:4b and qwen3:4b both reach 100% -- the difference is that qwen3's
# thinking mode costs 15x the wall time for no accuracy gain, so gemma3:4b wins
# on speed at equal quality (and is the US-origin model, which the owner prefers
# where performance is equivalent). The LLM's contribution over the deterministic
# parser is exactly one EFL: Atlantex's corrupted-font PDF, where "ae Charge
# $19.95 per ill" is legible to a language model and not to a regex.
#
# The two small models are worse than NO LLM at all -- both introduce a
# confidently-wrong value the parser alone never produced. That is the whole
# reason silent-wrong is the headline metric.
#
# Review-queue size is deliberately absent from this table: the tier is
# assist-only (see eflparse.llm_repair), so it never clears needs_review and the
# queue is 6/26 for every row.
#
# HARD CONSTRAINT: the model must fit ENTIRELY in the 8 GB of VRAM alongside its
# KV cache. When Ollama can't fit a model it silently offloads layers to CPU,
# and on WSL that means paging weights through ~7 GB of system RAM -- which is
# what hung this machine with the 8.5B `lfm2.5` (5.2 GB). Stay at or below ~3 GB
# of weights unless you have measured otherwise. Do not switch models without
# re-running scripts/eval_efl.py --compare.
OLLAMA_MODEL = "gemma3:4b"
# Ollama's model-list endpoint, derived from OLLAMA_URL, for the availability
# probe (a cheap GET that doesn't run inference).
OLLAMA_TAGS_URL = OLLAMA_URL.replace("/api/chat", "/api/tags")


# Ollama's own defaults are wrong for this workload and must be set explicitly.
#
# num_ctx: Ollama defaults to 4096 tokens. An EFL prompt is the ~700-token schema
#   system message plus up to 6000 chars of document text, which can exceed that
#   -- and when it does Ollama SILENTLY DROPS THE OLDEST TOKENS, i.e. the system
#   prompt carrying the schema, so the model answers a question it can no longer
#   see. 8192 fits the largest EFL in the corpus with headroom. Raising this
#   costs VRAM (KV cache), so it trades against the model-size ceiling documented
#   above; 8192 on a ~3 GB model is comfortable on an 8 GB card.
# temperature: Ollama defaults to 0.8. Reading a number off a rate table is not a
#   creative task -- sampling only adds run-to-run variance, which also makes the
#   eval harness non-reproducible. 0 is the only defensible setting here.
_OPTIONS = {"temperature": 0.0, "num_ctx": 8192}


def ollama_chat(
    messages: list[dict],
    model: str = OLLAMA_MODEL,
    ollama_url: str = OLLAMA_URL,
    timeout: float = 60.0,
) -> dict:
    """POST a chat completion to Ollama with JSON-forced output. Raises on any
    transport/HTTP error (server down, model not pulled)."""
    import httpx

    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": _OPTIONS,
    }
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
    message = (resp or {}).get("message", {})
    # Reasoning models (qwen3, lfm2.5-thinking) return their chain of thought in
    # a SEPARATE `thinking` field, not in `content`. It is logged, never parsed:
    # it is the only window into why a proposal came out the way it did, and
    # reading it is how we learned lfm2.5-thinking answers correctly in its
    # first paragraph and then talks itself out of it. Debug level -- these
    # traces run to several thousand characters.
    thinking = message.get("thinking")
    if thinking:
        logger.debug("LLM reasoning trace (model=%s, %d chars):\n%s", model, len(thinking), thinking)
    content = message.get("content", "")
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
