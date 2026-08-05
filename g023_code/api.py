"""
DeepSeek Responses API client.

The whole program talks to ``POST /responses`` — the orchestrator loop, the
subagents, and compaction alike. That endpoint is the only place DeepSeek
exposes its server-side ``web_search`` tool, so using it everywhere is what lets
web search be a first-class tool of the loop rather than something bolted on.

This is deliberately a thin wrapper over httpx rather than an SDK:

* the Responses shapes we need are small and we want them unmangled — the raw
  ``output`` items go straight back into the next request, which is both the
  highest-fidelity way to carry history and the most prefix-cache friendly;
* it keeps the request body open, so a field DeepSeek adds tomorrow needs no
  client release to reach.

Nothing here interprets the payload. Item semantics live in
:mod:`g023_code.orchestrator`.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator, Dict, Optional

import httpx

from .config import load_api_key, settings

# The API is strict about tool-call bookkeeping in both directions: a
# ``function_call`` with no matching ``function_call_output`` is a 400, and so is
# an output whose ``call_id`` was never called. Anything that edits history has
# to preserve that pairing — see ``Orchestrator._rollback_to_last_complete``.
DEFAULT_TIMEOUT = 300.0


class DeepSeekError(RuntimeError):
    """A non-200 from the API, carrying the server's own message where it has one."""

    def __init__(self, status: int, message: str, body: str = ""):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body

    @classmethod
    def from_response(cls, status: int, text: str) -> "DeepSeekError":
        message = text[:400]
        try:
            message = json.loads(text)["error"]["message"]
        except Exception:
            pass
        return cls(status, message, text)


class ResponsesClient:
    """Async client for ``/responses``, blocking and streaming.

    One long-lived httpx client is reused across calls so connections stay warm;
    a search turn can run for minutes on the server side, hence the timeout well
    above what a plain completion needs.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ):
        self._api_key = api_key
        self._base_url = (base_url or settings.base_url).rstrip("/")
        self._timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    # -- plumbing ---------------------------------------------------------

    def _key(self) -> str:
        # Read lazily: constructing a client must not fail on a missing K.dat,
        # so that /settings and --help still work without a key on disk.
        if self._api_key is None:
            self._api_key = load_api_key()
        return self._api_key

    def _http(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout, connect=15.0)
            )
        return self._client

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self._key()}",
            "Content-Type": "application/json",
        }

    @property
    def url(self) -> str:
        return f"{self._base_url}/responses"

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # -- calls ------------------------------------------------------------

    async def create(self, **body: Any) -> Dict[str, Any]:
        """One blocking request. Returns the parsed response object."""
        body.pop("stream", None)
        r = await self._http().post(self.url, headers=self._headers(), json=body)
        if r.status_code != 200:
            raise DeepSeekError.from_response(r.status_code, r.text)
        return r.json()

    async def stream(self, **body: Any) -> AsyncIterator[Dict[str, Any]]:
        """Yield SSE events as dicts.

        Every event carries a ``type``; the terminal ones are
        ``response.completed`` / ``.incomplete`` / ``.failed``, each of which
        embeds the full response object under ``response`` — including ``usage``
        and the finished ``output`` list. Callers should take their
        authoritative result from there and treat the deltas purely as something
        to render, which keeps reassembly bugs impossible.
        """
        body["stream"] = True
        async with self._http().stream(
            "POST", self.url, headers=self._headers(), json=body
        ) as r:
            if r.status_code != 200:
                raise DeepSeekError.from_response(
                    r.status_code, (await r.aread()).decode("utf-8", "replace")
                )
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    event = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    yield event


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def reasoning_param(
    enabled: Optional[bool] = None, effort: Optional[str] = None
) -> Dict[str, str]:
    """The ``reasoning`` block for a request.

    ``/responses`` has no equivalent of the old chat-completions
    ``thinking: {"type": "disabled"}`` — that field is accepted and silently
    ignored, and the model reasons anyway. ``effort: "none"`` is what actually
    turns thinking off, so that is what "thinking disabled" maps to here.
    """
    if enabled is None:
        enabled = settings.thinking_enabled
    if not enabled:
        return {"effort": "none"}
    return {"effort": effort or settings.reasoning_effort}


def output_text(response: Dict[str, Any]) -> str:
    """Concatenate the assistant's *final* text from a response.

    Messages come tagged with a ``phase``: ``commentary`` is the model narrating
    mid-flight ("let me check the docs page"), ``final_answer`` is the answer.
    The answer wins wherever there is one.

    A run can legitimately complete with commentary and nothing else, though. The
    callers here are all single-shot subagents with no tools to narrate towards,
    so for them that narration *is* the reply — falling back to it beats handing
    back "" and having the caller report the model as silent. It still returns ""
    when the model genuinely said nothing.
    """
    answer: list[str] = []
    commentary: list[str] = []
    for item in response.get("output") or []:
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        bucket = answer if item.get("phase") in (None, "final_answer") else commentary
        for part in item.get("content") or []:
            if part.get("type") == "output_text" and part.get("text"):
                bucket.append(part["text"])
    return "".join(answer) or "".join(commentary)


def incomplete_reason(response: Dict[str, Any]) -> str:
    """Why the model stopped early, or "" if it finished normally.

    Worth checking wherever a response is reduced to its text: high reasoning
    effort can burn the entire ``max_output_tokens`` budget before a single
    output token is emitted, leaving a lone truncated ``reasoning`` item and no
    message. :func:`output_text` then legitimately returns "", which is
    indistinguishable from "the model had nothing to say" unless you look here.
    """
    if response.get("status") != "incomplete":
        return ""
    return (response.get("incomplete_details") or {}).get("reason") or "incomplete"


def reasoning_text(response: Dict[str, Any]) -> str:
    """Concatenate the reasoning items of a response, for verbose display."""
    parts = []
    for item in response.get("output") or []:
        if item.get("type") != "reasoning":
            continue
        for part in item.get("content") or []:
            if part.get("type") == "reasoning_text" and part.get("text"):
                parts.append(part["text"])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Drift detection
# ---------------------------------------------------------------------------
#
# Nothing here validates the response, on purpose — an unknown field must still
# reach the model without a client release. The cost of that is that a *renamed*
# field fails silently: ``output_text`` returns "", the model looks like it said
# nothing, and no exception is raised anywhere. These two checks are the cheapest
# thing that turns that silence into a visible signal. They only ever report;
# they never change what is sent or how a response is interpreted.

# Item types this codebase knows how to read. Anything else is echoed back
# verbatim and skipped by the parse loop — harmless, but worth noticing, because
# it is the earliest visible sign that the API grew something new.
KNOWN_ITEM_TYPES: frozenset[str] = frozenset(
    {
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "web_search_call",
    }
)


def unknown_item_types(response: Dict[str, Any]) -> set[str]:
    """Item ``type`` values in this response that this client does not read."""
    return {
        str(item.get("type"))
        for item in response.get("output") or []
        if isinstance(item, dict) and item.get("type") not in KNOWN_ITEM_TYPES
    }


def silent_degradation(response: Dict[str, Any]) -> Optional[str]:
    """A description of a response that looks empty for no stated reason.

    An ``output`` list with assistant messages in it, no text recovered from
    them, and no ``incomplete`` status is what a renamed content field looks
    like from out here. It is *also* what a model that genuinely said nothing
    looks like, so this is a signal and not a diagnosis — which is exactly why it
    is worth surfacing rather than acting on.

    Returns ``None`` when nothing is off.
    """
    items = [i for i in response.get("output") or [] if isinstance(i, dict)]
    if not items:
        return None
    if incomplete_reason(response):
        return None  # the model stopped early and said so — not a silent failure
    if output_text(response):
        return None

    messages = [i for i in items if i.get("type") == "message"]
    if not messages:
        return None
    empty = [
        m
        for m in messages
        if not any(
            (p or {}).get("type") == "output_text" and (p or {}).get("text")
            for p in m.get("content") or []
        )
    ]
    if not empty:
        return None
    return (
        f"{len(empty)} assistant message item(s) carried no readable output_text, "
        "and the response did not report stopping early. Either the model said "
        "nothing, or the content field has been renamed."
    )


_client: Optional[ResponsesClient] = None


def get_client() -> ResponsesClient:
    """Process-wide client, shared by the orchestrator and every subagent."""
    global _client
    if _client is None:
        _client = ResponsesClient()
    return _client
