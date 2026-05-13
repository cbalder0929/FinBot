"""Tiny Ollama HTTP client.

Single-file wrapper around ``POST /api/chat`` with three guarantees:
  1. **Loopback only** — refuses any host that isn't localhost / 127.0.0.1
     / ::1, so transaction data can never leave the machine (D6).
  2. **Bounded timeout** — every call has a deadline; if Ollama is down,
     callers see a fast ``OllamaError`` instead of a hung pipeline.
  3. **stdlib only** — uses ``urllib`` so the pipeline keeps zero
     non-essential dependencies.

Public API:
  - ``chat(messages, model=..., host=..., json_mode=True, timeout=10)``
    → assistant message content as a string. Raises ``OllamaError``.
  - ``is_reachable(host, timeout)`` → ``bool``. Never raises.

Environment knobs:
  - ``OLLAMA_HOST``  (default ``http://127.0.0.1:11434``)
  - ``OLLAMA_MODEL`` (default ``llama3.2:3b``)
  - ``OLLAMA_TIMEOUT`` seconds, float (default ``10``)
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request


DEFAULT_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
DEFAULT_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
# 60s default. The *first* call after the daemon starts (or after a fresh
# pull) cold-loads the model into RAM, which on small CPU-only machines
# can take 15-30s. Steady-state calls are typically 200-800 ms.
DEFAULT_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60"))

_LOOPBACK_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}


class OllamaError(RuntimeError):
    """Any failure talking to Ollama: connection, timeout, malformed reply."""


def _assert_loopback(host: str) -> None:
    """Privacy guard. Refuse any host that isn't loopback.

    This is the only mechanism between this client and a misconfigured
    ``OLLAMA_HOST`` env var pointing at a remote box. Keep it strict.
    """
    parsed = urllib.parse.urlparse(host)
    hostname = (parsed.hostname or "").lower()
    if hostname not in _LOOPBACK_HOSTNAMES:
        raise OllamaError(
            f"OLLAMA_HOST must be loopback (localhost / 127.0.0.1 / ::1); "
            f"got {hostname!r}. This guard prevents transaction data from "
            f"leaving the machine — change FinScrape's source if you really "
            f"intend to send personal finance data over the network."
        )


def chat(
    messages: list[dict],
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    json_mode: bool = True,
    temperature: float = 0.0,
    num_predict: int | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """POST a chat completion to Ollama and return the assistant content.

    ``json_mode=True`` instructs Ollama to constrain output to valid JSON
    (``format: "json"``), which is what every caller in this project
    wants. Set ``temperature`` higher for narrative tasks; leave at 0 for
    categorization.
    """
    _assert_loopback(host)

    options: dict = {"temperature": temperature}
    if num_predict is not None:
        options["num_predict"] = num_predict

    payload: dict = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": options,
    }
    if json_mode:
        payload["format"] = "json"

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{host.rstrip('/')}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            envelope = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise OllamaError(f"Ollama HTTP {e.code}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise OllamaError(f"failed to reach Ollama at {host}: {e.reason}") from e
    except json.JSONDecodeError as e:
        raise OllamaError(f"Ollama returned non-JSON envelope: {e}") from e

    try:
        return envelope["message"]["content"]
    except (KeyError, TypeError) as e:
        raise OllamaError(f"unexpected Ollama response shape: {envelope!r}") from e


def is_reachable(host: str = DEFAULT_HOST, timeout: float = 2.0) -> bool:
    """Cheap health check. Never raises — returns True only on HTTP 200."""
    try:
        _assert_loopback(host)
    except OllamaError:
        return False

    req = urllib.request.Request(f"{host.rstrip('/')}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status == 200
    except Exception:
        return False


def list_models(host: str = DEFAULT_HOST, timeout: float = 2.0) -> list[str]:
    """Return the models Ollama has pulled. Empty list on any error."""
    try:
        _assert_loopback(host)
    except OllamaError:
        return []
    req = urllib.request.Request(f"{host.rstrip('/')}/api/tags", method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [m.get("name", "") for m in data.get("models", []) if m.get("name")]
    except Exception:
        return []
