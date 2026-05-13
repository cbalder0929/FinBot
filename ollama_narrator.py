"""Narrate pre-computed report numbers into prose.

This is the only place where Ollama writes free-form text in FinScrape.
The contract is strict and one-way:

  - **The LLM never sees raw transaction rows.** It only sees the
    aggregate dict produced by ``reports.py``.
  - **The LLM never does arithmetic.** Any number it utters must come
    verbatim from the input dict.
  - The LLM only paraphrases. If a fact isn't in the dict, it isn't in
    the narration.

If Ollama is unreachable, ``narrate()`` returns ``None`` — the caller
serves the numbers without prose. The narration is always optional.

Public API:
  - ``narrate(section, payload) -> (str | None, str | None)``
    where ``section`` is one of ``topline | by_category | trend | monthly``.
    Returns ``(text, None)`` on success or ``(None, reason)`` on failure
    so the caller can surface the real reason in the UI (timeout vs.
    unreachable vs. empty response).
"""
from __future__ import annotations

import json
from typing import Optional, Tuple

from ollama_client import DEFAULT_HOST, DEFAULT_MODEL, OllamaError, chat, is_reachable


# Temperature is higher than for categorization because we want a
# little variation; still low enough to stay grounded.
NARRATION_TEMPERATURE = 0.4

# Generous timeout — Phase 2 runs on a Reports page where the user
# expects to wait. On CPU-only Ollama (~0.8 tok/s) a 64-token paragraph
# takes ~80s of generation plus prompt eval and a possible cold load,
# so the budget needs real headroom. 90s was too tight and surfaced as
# "Ollama not reachable" in the UI even though Ollama was healthy.
NARRATION_TIMEOUT = 300.0
NARRATION_MAX_TOKENS = 56


_SYSTEM_BASE = (
    "You are a personal finance assistant. Write a short, plain-language "
    "summary of the numbers you are given. Rules:\n"
    "  - Use only the numbers in the JSON payload. Do not invent figures.\n"
    "  - Round dollar amounts to whole dollars in prose (e.g. '$312').\n"
    "  - Be concrete: name the top category or merchant by name.\n"
    "  - 2-3 sentences. No bullet points, no markdown, no headings.\n"
    "  - No moralizing or advice — just describe what happened.\n"
)


def _build_prompt(section: str, payload: dict) -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for a given section."""
    if section == "topline":
        extra = (
            "This is the overall summary across the entire period covered."
            " Mention total spending, net cashflow, and the top spending category."
        )
    elif section == "by_category":
        extra = (
            "These are categories the user spent money on. Mention the top 2-3 by"
            " spend and the share of total they represent if obvious from the numbers."
        )
    elif section == "trend":
        extra = (
            "These are month-over-month numbers. Describe the trajectory:"
            " is spending growing, shrinking, or steady? Mention the most"
            " recent month by name."
        )
    elif section == "monthly":
        extra = (
            "This is one specific month. Cover spending, income, the standout"
            " category or merchant, and how the month compares to the trailing"
            " 6-month average (the payload includes 'spending_vs_6mo_avg')."
        )
    else:
        extra = "Summarize the numbers concisely."

    system = _SYSTEM_BASE + "\nContext: " + extra
    user = "JSON payload:\n" + json.dumps(payload, indent=2, default=str)
    return system, user


def narrate(
    section: str,
    payload: dict,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
) -> Tuple[Optional[str], Optional[str]]:
    """Return ``(prose, error_reason)`` describing ``payload``.

    On success: ``(text, None)``.
    On failure: ``(None, "<short human-readable reason>")``.

    Never raises — callers can drop the text straight into a JSON
    response with ``narration: null`` being a legitimate value, and
    surface ``error_reason`` so the UI doesn't have to guess whether
    Ollama was down or just slow.
    """
    if not is_reachable(host):
        return None, "Ollama is not reachable at localhost:11434."

    system, user = _build_prompt(section, payload)
    try:
        text = chat(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            model=model,
            host=host,
            json_mode=False,        # free-form prose
            temperature=NARRATION_TEMPERATURE,
            num_predict=NARRATION_MAX_TOKENS,
            timeout=NARRATION_TIMEOUT,
        )
    except OllamaError as e:
        msg = str(e)
        print(f"[narrate] {section}: {msg}")
        # urllib raises URLError("timed out") on socket timeout, which the
        # client wraps as "failed to reach Ollama at <host>: timed out".
        # Distinguish that from a real connection failure so the UI can
        # suggest the right fix (smaller model / longer wait, not "is it
        # running?").
        if "timed out" in msg.lower() or "timeout" in msg.lower():
            return None, (
                f"Narration timed out after {int(NARRATION_TIMEOUT)}s — "
                "your machine is generating too slowly for this model. "
                "Try a smaller model (e.g. `ollama pull qwen2.5:0.5b` then "
                "set OLLAMA_MODEL=qwen2.5:0.5b)."
            )
        return None, f"Ollama failed: {msg}"

    text = (text or "").strip()
    # Defensive: an empty response or a single-line refusal is useless.
    if not text or len(text) < 20:
        return None, "Ollama returned an empty or too-short response."
    return text, None
