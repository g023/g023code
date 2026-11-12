"""
DeepSeek LLM client — one model, one wire.

Every call goes to DeepSeek V4.1 Flash (``config.MODEL``) on DeepSeek's
Anthropic-format endpoint (``POST {base}/anthropic/v1/messages``). Web access is
two client tools on DeepSeek's Search/Fetch APIs (:mod:`g023_code.web`); the
server-side ``web_search`` tool is no longer sent, and ``web_search_call`` items from
older sessions are only replayed. Callers build and read a small,
stable item vocabulary (``message`` / ``reasoning`` / ``function_call`` /
``function_call_output`` / ``web_search_call`` — the program's internal
representation, also what session logs store on disk);
:mod:`g023_code.anthropic_codec` translates it to Messages and back,
deterministically, so the KV prefix cache keeps hitting.

This is deliberately a thin wrapper over httpx rather than an SDK: the request
body stays open (a field DeepSeek adds tomorrow needs no client release), and
nothing here interprets the payload — item semantics live in
:mod:`g023_code.orchestrator`.
"""

from __future__ import annotations

import asyncio
import json
import random
from typing import Any, AsyncIterator, Dict, Optional

import httpx

from . import anthropic_codec as codec
from .config import MESSAGES_PATH, MODEL, load_api_key, settings
from .effort import effort_to_wire

# The API is strict about tool-call bookkeeping in both directions: a
# ``function_call`` with no matching ``function_call_output`` is a 400, and so is
# an output whose ``call_id`` was never called. Anything that edits history has
# to preserve that pairing — see ``Orchestrator._rollback_to_last_complete``.
# A server-side search turn can run for minutes, hence the generous timeout.
DEFAULT_TIMEOUT = 300.0


class DeepSeekError(RuntimeError):
    """A non-200 from the API, carrying the server's own message where it has one."""

    def __init__(self, status: int, message: str, body: str = "", retry_after: Optional[str] = None):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body
        self.retry_after = retry_after

    @classmethod
    def from_response(cls, status: int, text: str, retry_after: Optional[str] = None) -> "DeepSeekError":
        message = text[:400]
        try:
            message = json.loads(text)["error"]["message"]
        except Exception:
            pass
        return cls(status, message, text, retry_after)


# Transient failures worth a retry. A request that never produced a response
# has no side effect beyond (at most) billing, so a bounded retry is safe. A
# ReadTimeout is not retried: it means a long generation already ran for the
# full timeout, and repeating it would triple the wait.
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504, 529})
RETRY_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)
MAX_ATTEMPTS = 3
RETRY_BASE_S = 1.0
RETRY_CAP_S = 30.0


def retry_delay(attempt: int, retry_after: Optional[str] = None) -> float:
    """Seconds before retry ``attempt`` (1-based): Retry-After if given, else jittered backoff."""
    if retry_after:
        try:
            return max(0.0, min(RETRY_CAP_S, float(retry_after)))
        except ValueError:
            pass
    base = min(RETRY_CAP_S, RETRY_BASE_S * (2 ** (attempt - 1)))
    return base * (0.5 + random.random() / 2)


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, DeepSeekError):
        return exc.status in RETRY_STATUSES
    return isinstance(exc, RETRY_EXCEPTIONS)


class _RetryBeforeFirstEvent(Exception):
    """Internal: a stream attempt failed before anything was yielded."""

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause


class DeepSeekClient:
    """Async client for the Anthropic-format Messages endpoint, blocking and streaming.

    One long-lived httpx client is reused across calls so connections stay warm.
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
        return f"{self._base_url}{MESSAGES_PATH}"

    def _wire_body(self, body: Dict[str, Any]) -> Dict[str, Any]:
        # The model is not the caller's choice: there is exactly one.
        body["model"] = MODEL
        return codec.to_messages_body(body)

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    # -- calls ------------------------------------------------------------

    async def create(self, *, timeout: Optional[float] = None, **body: Any) -> Dict[str, Any]:
        """One blocking request. Returns the decoded (item-shaped) response."""
        body.pop("stream", None)
        kwargs: Dict[str, Any] = {"headers": self._headers(), "json": self._wire_body(body)}
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout, connect=15.0)
        attempt = 0
        while True:
            attempt += 1
            try:
                r = await self._http().post(self.url, **kwargs)
                if r.status_code != 200:
                    raise DeepSeekError.from_response(
                        r.status_code, r.text, r.headers.get("retry-after")
                    )
                break
            except Exception as e:
                if attempt >= MAX_ATTEMPTS or not _retryable(e):
                    raise
                await asyncio.sleep(retry_delay(attempt, getattr(e, "retry_after", None)))
        data = r.json()
        if isinstance(data, dict) and data.get("type") == "error":
            err = data.get("error") or {}
            raise DeepSeekError(r.status_code, err.get("message") or "error", r.text)
        return codec.from_message(data)

    async def stream(self, **body: Any) -> AsyncIterator[Dict[str, Any]]:
        """Yield decoded stream events as dicts.

        Every event carries a ``type``; the terminal ones are
        ``response.completed`` / ``.incomplete``, each of which embeds the full
        decoded response under ``response`` — including ``usage`` and the
        finished ``output`` list. Callers take their authoritative result from
        there and treat the deltas purely as something to render, which keeps
        reassembly bugs impossible.
        """
        body["stream"] = True
        wire = self._wire_body(body)
        attempt = 0
        while True:
            attempt += 1
            try:
                async for out in self._stream_once(wire):
                    yield out
                return
            except _RetryBeforeFirstEvent as e:
                if attempt >= MAX_ATTEMPTS:
                    raise e.cause from None
                await asyncio.sleep(retry_delay(attempt, getattr(e.cause, "retry_after", None)))

    async def _stream_once(self, wire: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
        """One streaming attempt. Failures before the first event are retryable."""
        translator = codec.StreamTranslator()
        yielded = False
        try:
            async with self._http().stream(
                "POST", self.url, headers=self._headers(), json=wire
            ) as r:
                if r.status_code != 200:
                    raise DeepSeekError.from_response(
                        r.status_code,
                        (await r.aread()).decode("utf-8", "replace"),
                        r.headers.get("retry-after"),
                    )
                async for out in self._events(r, translator):
                    yielded = True
                    yield out
        except Exception as e:
            if not yielded and not isinstance(e, _RetryBeforeFirstEvent) and _retryable(e):
                raise _RetryBeforeFirstEvent(e) from e
            raise

    async def _events(self, r: httpx.Response, translator: Any) -> AsyncIterator[Dict[str, Any]]:
        async for line in r.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            try:
                translated = translator.feed(event)
            except codec.StreamError as e:
                raise DeepSeekError(0, f"stream error: {e}") from None
            for out in translated:
                yield out
        if not translator.done:
            # Closed before message_stop: whatever arrived is incomplete.
            resp = translator.response()
            resp["status"] = "incomplete"
            resp.setdefault("incomplete_details", {"reason": "stream_closed"})
            yield {"type": "response.incomplete", "response": resp}

    # -- vision ----------------------------------------------------------

    async def vision(
        self,
        prompt: str,
        data_uri: str,
        system: str = "",
        timeout: float = 180.0,
        max_output_tokens: int = 2048,
    ) -> Dict[str, Any]:
        """One isolated image question: image + prompt in a single user turn.

        ``data_uri`` must carry the image's real media type
        (``data:image/jpeg;base64,...``); the codec turns it into an Anthropic
        ``image`` block. Thinking is off — a transcription does not need it, and
        it would eat the output budget.
        """
        body = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": data_uri},
                        {"type": "input_text", "text": prompt},
                    ],
                }
            ],
            "reasoning": reasoning_param(enabled=False),
            "max_output_tokens": max_output_tokens,
        }
        if system:
            body["instructions"] = system
        return await self.create(timeout=timeout, **body)


# ---------------------------------------------------------------------------
# Request helpers
# ---------------------------------------------------------------------------


def reasoning_param(
    enabled: Optional[bool] = None, effort: Optional[Any] = None
) -> Dict[str, str]:
    """The ``reasoning`` block for a request (the codec turns it into
    ``thinking`` + ``output_config.effort``).

    Integer ``1``–``100`` is a user/model scale, not a wire value: the API
    rejects numbers. :func:`g023_code.effort.effort_to_wire` maps it onto
    ``none`` / ``low`` / ``high``, capped at ``MAX_EFFORT`` (75 → ``high``).
    This harness never sends ``max``.
    """
    if enabled is None:
        enabled = settings.thinking_enabled
    if effort is None:
        effort = settings.reasoning_effort
    return {"effort": effort_to_wire(effort, enabled=bool(enabled))}


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


_client: Optional[DeepSeekClient] = None


def get_client() -> DeepSeekClient:
    """Process-wide client, shared by the orchestrator and every subagent."""
    global _client
    if _client is None:
        _client = DeepSeekClient()
    return _client


async def close_client() -> None:
    """Close the shared client's connections (call on the loop that used it)."""
    if _client is not None:
        await _client.aclose()
