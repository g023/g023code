"""
Responses-shaped items  <->  DeepSeek Anthropic-format ``/v1/messages``.

The orchestrator, subagents, session log and compaction all speak the
Responses item vocabulary (``message`` / ``reasoning`` / ``function_call`` /
``function_call_output`` / ``web_search_call``). DeepSeek now prefers its
Anthropic-format endpoint, which is also where its server-side ``web_search``
tool lives. Rather than rewrite 20k lines of item handling, this module is a
*deterministic* wire codec:

* ``to_messages_body`` turns a Responses request body into a Messages body.
  It is a pure function of its input, so a byte-identical item prefix encodes
  to a byte-identical message prefix — the property DeepSeek's KV prefix
  cache needs to keep hitting.
* ``from_message`` turns a Messages response back into a Responses-shaped
  response (``output`` items, ``status``, ``usage``).
* ``StreamTranslator`` turns Messages SSE events into the handful of
  Responses stream events the orchestrator renders, then emits a terminal
  ``response.completed`` built by ``from_message`` — so streamed and blocking
  calls produce the same items.

Round-trip rules that matter for correctness (DeepSeek Anthropic guide):

* Thinking blocks must be replayed with their ``signature`` when tool calls
  continue, so a ``reasoning`` item keeps the signature under ``signature``.
  Reasoning without a signature (e.g. a legacy /responses session) is dropped
  on encode — the service recovers by tool-call id, and an unsigned thinking
  block is riskier than none.
* ``server_tool_use`` + ``web_search_tool_result`` are kept verbatim under
  ``_anthropic`` on a ``web_search_call`` item and replayed unchanged
  (``encrypted_content`` must not be altered).
* Every ``tool_use`` needs a ``tool_result`` in the very next user message,
  placed before any text there.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Tuple

# Effort aliases DeepSeek accepts in ``output_config.effort`` (others map
# server-side: minimal->low, medium->high, xhigh->high, ultra->max).
_THINKING_OFF = {"none", "disabled", "off"}

WEB_SEARCH_TOOL_TYPE = "web_search_20250305"
DEFAULT_WEB_SEARCH_MAX_USES = 5

_TOOL_BLOCK_TYPES = ("tool_use", "server_tool_use")


# ---------------------------------------------------------------------------
# Request: Responses body -> Messages body
# ---------------------------------------------------------------------------


def _text_part_value(part: Dict[str, Any]) -> Optional[str]:
    if part.get("type") in ("input_text", "output_text", "text"):
        return part.get("text") or ""
    return None


def _image_block(part: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``input_image`` (data URI or http URL) -> Anthropic ``image`` block."""
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    if not isinstance(url, str) or not url:
        return None
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        media = header[5:].split(";", 1)[0] or "image/png"
        return {
            "type": "image",
            "source": {"type": "base64", "media_type": media, "data": data},
        }
    return {"type": "image", "source": {"type": "url", "url": url}}


def _user_content_blocks(content: Any) -> List[Dict[str, Any]]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    blocks: List[Dict[str, Any]] = []
    for part in content or []:
        if not isinstance(part, dict):
            continue
        text = _text_part_value(part)
        if text is not None:
            if text:
                blocks.append({"type": "text", "text": text})
            continue
        if part.get("type") in ("input_image", "image"):
            if part.get("type") == "image" and part.get("source"):
                blocks.append({"type": "image", "source": part["source"]})
                continue
            img = _image_block(part)
            if img:
                blocks.append(img)
    return blocks


def _tool_result_content(output: Any) -> Any:
    if isinstance(output, str):
        return output if output else "(empty)"
    if isinstance(output, list):
        blocks = _user_content_blocks(output)
        return blocks or "(empty)"
    return json.dumps(output, ensure_ascii=False, default=str)


def _parse_arguments(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {"_malformed_arguments": str(raw)}
    return value if isinstance(value, dict) else {"_value": value}


def items_to_messages(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Encode Responses items as alternating Anthropic messages.

    Consecutive same-role blocks merge into one message. In a user message,
    ``tool_result`` blocks are moved ahead of text (API requirement) while
    keeping their relative order.
    """
    seq: List[Tuple[str, Dict[str, Any]]] = []  # (role, block)
    for item in items:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "reasoning":
            sig = item.get("signature")
            if not sig:
                continue
            text = "".join(
                (p.get("text") or "")
                for p in item.get("content") or []
                if isinstance(p, dict)
            )
            seq.append(("assistant", {"type": "thinking", "thinking": text, "signature": sig}))
        elif kind == "function_call":
            seq.append(
                (
                    "assistant",
                    {
                        "type": "tool_use",
                        "id": item.get("call_id") or item.get("id") or "",
                        "name": item.get("name") or "",
                        "input": _parse_arguments(item.get("arguments")),
                    },
                )
            )
        elif kind == "function_call_output":
            seq.append(
                (
                    "user",
                    {
                        "type": "tool_result",
                        "tool_use_id": item.get("call_id") or "",
                        "content": _tool_result_content(item.get("output")),
                    },
                )
            )
        elif kind == "web_search_call":
            raw = item.get("_anthropic")
            if isinstance(raw, list) and raw:
                for block in raw:
                    seq.append(("assistant", block))
            # A legacy /responses search item carries no replayable blocks;
            # its results already live in the following text, so skip it.
        else:
            role = item.get("role") or "assistant"
            if role in ("system", "developer"):
                role = "user"
            content = item.get("content")
            if role == "assistant":
                if isinstance(content, str):
                    blocks = [{"type": "text", "text": content}] if content else []
                else:
                    blocks = [
                        {"type": "text", "text": t}
                        for t in (
                            _text_part_value(p) for p in content or [] if isinstance(p, dict)
                        )
                        if t
                    ]
            else:
                blocks = _user_content_blocks(content)
            for b in blocks:
                seq.append((role, b))

    messages: List[Dict[str, Any]] = []
    for role, block in seq:
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].append(block)
        else:
            messages.append({"role": role, "content": [block]})

    out: List[Dict[str, Any]] = []
    for msg in messages:
        blocks = msg["content"]
        if msg["role"] == "user":
            results = [b for b in blocks if b.get("type") == "tool_result"]
            if results:
                rest = [b for b in blocks if b.get("type") != "tool_result"]
                blocks = results + rest
        else:
            # An assistant turn of nothing but thinking is not a valid turn.
            if all(b.get("type") == "thinking" for b in blocks):
                continue
        if out and out[-1]["role"] == msg["role"]:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": msg["role"], "content": blocks})

    if out and out[0]["role"] != "user":
        out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(continue)"}]})
    return out


def tools_to_anthropic(
    tools: Optional[Iterable[Dict[str, Any]]], web_search_max_uses: int = DEFAULT_WEB_SEARCH_MAX_USES
) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        ttype = tool.get("type")
        if ttype == "web_search" or (isinstance(ttype, str) and ttype.startswith("web_search_")):
            ws: Dict[str, Any] = {
                "type": ttype if ttype and ttype != "web_search" else WEB_SEARCH_TOOL_TYPE,
                "name": "web_search",
            }
            uses = tool.get("max_uses", web_search_max_uses)
            if uses:
                ws["max_uses"] = int(uses)
            out.append(ws)
            continue
        fn = tool.get("function") if ttype == "function" and "function" in tool else tool
        name = fn.get("name")
        if not name:
            continue
        entry: Dict[str, Any] = {
            "name": name,
            "input_schema": fn.get("parameters") or fn.get("input_schema") or {"type": "object", "properties": {}},
        }
        if fn.get("description"):
            entry["description"] = fn["description"]
        out.append(entry)
    return out


def thinking_params(reasoning: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """``reasoning: {effort}`` -> ``thinking`` (+ ``output_config.effort``)."""
    effort = str((reasoning or {}).get("effort") or "").strip().lower()
    if not effort:
        return {}
    if effort in _THINKING_OFF:
        return {"thinking": {"type": "disabled"}}
    return {"thinking": {"type": "enabled"}, "output_config": {"effort": effort}}


def to_messages_body(
    body: Dict[str, Any], *, web_search_max_uses: int = DEFAULT_WEB_SEARCH_MAX_USES
) -> Dict[str, Any]:
    """Translate a Responses request body. Key order is fixed for determinism."""
    out: Dict[str, Any] = {"model": body.get("model")}
    if body.get("instructions"):
        out["system"] = body["instructions"]
    tools = tools_to_anthropic(body.get("tools"), web_search_max_uses)
    if tools:
        out["tools"] = tools
    params = thinking_params(body.get("reasoning"))
    out.update(params)
    if body.get("temperature") is not None and (params.get("thinking") or {}).get("type") != "enabled":
        # Thinking mode pins temperature at 1.0; sending it there is noise.
        out["temperature"] = body["temperature"]
    if body.get("max_output_tokens"):
        out["max_tokens"] = int(body["max_output_tokens"])
    if body.get("stop"):
        out["stop_sequences"] = list(body["stop"]) if isinstance(body["stop"], list) else [body["stop"]]
    input_items = body.get("input")
    if isinstance(input_items, str):
        input_items = [{"role": "user", "content": input_items}]
    out["messages"] = items_to_messages(input_items or [])
    if body.get("stream"):
        out["stream"] = True
    return out


# ---------------------------------------------------------------------------
# Response: Messages -> Responses
# ---------------------------------------------------------------------------


def _search_queries(block: Dict[str, Any]) -> List[str]:
    inp = block.get("input") or {}
    q = inp.get("query") if isinstance(inp, dict) else None
    if isinstance(q, list):
        return [str(x) for x in q if x]
    return [str(q)] if q else []


def _search_sources(result_block: Dict[str, Any]) -> List[Dict[str, str]]:
    content = result_block.get("content")
    if not isinstance(content, list):
        return []
    out = []
    for r in content:
        if isinstance(r, dict) and r.get("url"):
            out.append({"type": "url", "url": r["url"], "title": r.get("title") or ""})
    return out


def convert_usage(usage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    usage = usage or {}
    miss = int(usage.get("input_tokens") or 0)
    hit = int(usage.get("cache_read_input_tokens") or 0)
    out_tok = int(usage.get("output_tokens") or 0)
    res: Dict[str, Any] = {
        "input_tokens": miss + hit,
        "input_tokens_details": {"cached_tokens": hit},
        "output_tokens": out_tok,
        "total_tokens": miss + hit + out_tok,
    }
    stu = usage.get("server_tool_use")
    if isinstance(stu, dict):
        res["server_tool_use"] = dict(stu)
    return res


def from_message(msg: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic message -> Responses-shaped response dict."""
    mid = msg.get("id") or "msg"
    blocks = [b for b in msg.get("content") or [] if isinstance(b, dict)]
    stop = msg.get("stop_reason") or "end_turn"
    last_tool = max(
        (i for i, b in enumerate(blocks) if b.get("type") in _TOOL_BLOCK_TYPES),
        default=-1,
    )
    output: List[Dict[str, Any]] = []
    searches: Dict[str, Dict[str, Any]] = {}
    for i, b in enumerate(blocks):
        t = b.get("type")
        if t == "thinking":
            item: Dict[str, Any] = {
                "type": "reasoning",
                "id": f"rs_{mid}_{i}",
                "content": [{"type": "reasoning_text", "text": b.get("thinking") or ""}],
            }
            if b.get("signature"):
                item["signature"] = b["signature"]
            output.append(item)
        elif t == "text":
            phase = "commentary" if (i < last_tool or stop == "tool_use") else "final_answer"
            output.append(
                {
                    "type": "message",
                    "id": f"msg_{mid}_{i}",
                    "role": "assistant",
                    "status": "completed",
                    "phase": phase,
                    "content": [{"type": "output_text", "text": b.get("text") or "", "annotations": []}],
                }
            )
        elif t == "tool_use":
            output.append(
                {
                    "type": "function_call",
                    "id": f"fc_{b.get('id')}",
                    "call_id": b.get("id") or "",
                    "name": b.get("name") or "",
                    "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                    "status": "completed",
                }
            )
        elif t == "server_tool_use":
            item = {
                "type": "web_search_call",
                "id": b.get("id") or f"ws_{mid}_{i}",
                "status": "completed",
                "action": {"type": "search", "queries": _search_queries(b)},
                "_anthropic": [b],
            }
            searches[b.get("id") or ""] = item
            output.append(item)
        elif t == "web_search_tool_result":
            item = searches.get(b.get("tool_use_id") or "")
            if item is None:
                # Orphan result: keep it replayable on its own.
                item = {
                    "type": "web_search_call",
                    "id": b.get("tool_use_id") or f"ws_{mid}_{i}",
                    "status": "completed",
                    "action": {"type": "search", "queries": []},
                    "_anthropic": [],
                }
                output.append(item)
            item["_anthropic"].append(b)
            content = b.get("content")
            if isinstance(content, dict) and content.get("type", "").endswith("error"):
                item["status"] = "failed"
            item["action"]["sources"] = _search_sources(b)
        # Unknown block types are dropped: they cannot be replayed safely.

    resp: Dict[str, Any] = {
        "id": mid,
        "object": "response",
        "model": msg.get("model"),
        "status": "completed",
        "output": output,
        "usage": convert_usage(msg.get("usage")),
        "stop_reason": stop,
    }
    if stop == "max_tokens":
        resp["status"] = "incomplete"
        resp["incomplete_details"] = {"reason": "max_output_tokens"}
    return resp


# ---------------------------------------------------------------------------
# Streaming: Messages SSE -> Responses-style events
# ---------------------------------------------------------------------------


class StreamError(RuntimeError):
    pass


class StreamTranslator:
    """Feed parsed SSE ``data`` dicts; get back Responses-style events.

    Only the events the orchestrator renders are synthesised; the terminal
    ``response.completed`` / ``response.incomplete`` carries the authoritative
    response built by :func:`from_message`.
    """

    def __init__(self) -> None:
        self.message: Dict[str, Any] = {"content": []}
        self.blocks: Dict[int, Dict[str, Any]] = {}
        self._json_buf: Dict[int, List[str]] = {}
        self._pending_text: List[int] = []  # text block indexes awaiting a phase
        self.done = False
        self.usage: Dict[str, Any] = {}

    def _msg_item(self, idx: int, phase: str) -> Dict[str, Any]:
        b = self.blocks.get(idx) or {}
        return {
            "type": "message",
            "id": f"msg_{self.message.get('id') or 'msg'}_{idx}",
            "role": "assistant",
            "status": "completed",
            "phase": phase,
            "content": [{"type": "output_text", "text": b.get("text") or ""}],
        }

    def _flush_text(self, phase: str) -> List[Dict[str, Any]]:
        out = [
            {"type": "response.output_item.done", "item": self._msg_item(i, phase)}
            for i in self._pending_text
        ]
        self._pending_text = []
        return out

    def feed(self, ev: Dict[str, Any]) -> List[Dict[str, Any]]:
        t = ev.get("type")
        if t is None and "error" in ev:
            err = ev.get("error") or {}
            raise StreamError(err.get("message") if isinstance(err, dict) else str(err))
        if t == "error":
            err = ev.get("error") or {}
            raise StreamError(err.get("message") if isinstance(err, dict) else str(err))
        if t == "message_start":
            m = ev.get("message") or {}
            self.message = {k: v for k, v in m.items() if k != "content"}
            self.message["content"] = []
            self.usage = dict(m.get("usage") or {})
            return []
        if t == "content_block_start":
            idx = int(ev.get("index") or 0)
            block = dict(ev.get("content_block") or {})
            out: List[Dict[str, Any]] = []
            if block.get("type") == "tool_use":
                block["input"] = {}
                self._json_buf[idx] = []
            if block.get("type") in _TOOL_BLOCK_TYPES:
                out.extend(self._flush_text("commentary"))
            if block.get("type") == "server_tool_use":
                self._json_buf[idx] = []
                out.append(
                    {
                        "type": "response.output_item.added",
                        "item": {"type": "web_search_call", "id": block.get("id")},
                    }
                )
            if block.get("type") == "text":
                block.setdefault("text", "")
            if block.get("type") == "thinking":
                block.setdefault("thinking", "")
            self.blocks[idx] = block
            return out
        if t == "content_block_delta":
            idx = int(ev.get("index") or 0)
            d = ev.get("delta") or {}
            b = self.blocks.setdefault(idx, {"type": "text", "text": ""})
            dt = d.get("type")
            if dt == "text_delta":
                piece = d.get("text") or ""
                b["text"] = (b.get("text") or "") + piece
                return [
                    {
                        "type": "response.output_text.delta",
                        "item_id": f"msg_{self.message.get('id') or 'msg'}_{idx}",
                        "delta": piece,
                    }
                ]
            if dt == "thinking_delta":
                piece = d.get("thinking") or ""
                b["thinking"] = (b.get("thinking") or "") + piece
                return [{"type": "response.reasoning_text.delta", "delta": piece}]
            if dt == "signature_delta":
                b["signature"] = (b.get("signature") or "") + (d.get("signature") or "")
            elif dt == "input_json_delta":
                self._json_buf.setdefault(idx, []).append(d.get("partial_json") or "")
            elif dt == "citations_delta":
                pass
            return []
        if t == "content_block_stop":
            idx = int(ev.get("index") or 0)
            b = self.blocks.get(idx) or {}
            if idx in self._json_buf:
                raw = "".join(self._json_buf.pop(idx))
                if raw.strip():
                    b["input"] = _parse_arguments(raw)
                elif "input" not in b:
                    b["input"] = {}
            if b.get("type") == "text":
                self._pending_text.append(idx)
            if b.get("type") == "web_search_tool_result":
                partial = from_message(
                    {"id": self.message.get("id"), "content": self._ordered_blocks()}
                )
                for item in partial["output"]:
                    if item.get("type") == "web_search_call" and item.get("id") == b.get("tool_use_id"):
                        return [{"type": "response.output_item.done", "item": item}]
            return []
        if t == "message_delta":
            d = ev.get("delta") or {}
            if "stop_reason" in d:
                self.message["stop_reason"] = d.get("stop_reason")
            if "stop_sequence" in d:
                self.message["stop_sequence"] = d.get("stop_sequence")
            u = ev.get("usage") or {}
            # Final message_delta usage is cumulative; overlay it.
            self.usage.update({k: v for k, v in u.items() if v is not None})
            return []
        if t == "message_stop":
            self.done = True
            phase = "commentary" if self.message.get("stop_reason") == "tool_use" else "final_answer"
            out = self._flush_text(phase)
            resp = self.response()
            out.append(
                {
                    "type": "response.incomplete" if resp["status"] == "incomplete" else "response.completed",
                    "response": resp,
                }
            )
            return out
        return []  # ping and anything new

    def _ordered_blocks(self) -> List[Dict[str, Any]]:
        return [self.blocks[i] for i in sorted(self.blocks)]

    def response(self) -> Dict[str, Any]:
        msg = dict(self.message)
        msg["content"] = self._ordered_blocks()
        msg["usage"] = self.usage
        return from_message(msg)
