"""
Subagent Router — intercepts heavy tools and delegates to isolated subagents.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Optional

from ..api import (
    DeepSeekClient,
    get_client,
    incomplete_reason,
    output_text,
    reasoning_param,
    reasoning_text,
)
from ..cache import get_cache
from ..cost_policy import should_vault_tool_output
from ..effort import effort_to_wire, parse_effort_value
from ..config import MODEL, get_project_root, settings
from ..tools.schemas import TOOL_SCHEMAS
from ..usage import get_usage
from .file_reader import run_file_reader
from .searcher import run_searcher
from .vision import run_vision

# Nested Agent loop: depth 1 (no Agent tool, so no further spawn).
AGENT_READONLY_TOOLS = ("ReadFile", "SearchContent", "ListDir")
AGENT_TASK_TOOLS = (
    "ReadFile",
    "SearchContent",
    "ListDir",
    "Glob",
    "Edit",
    "WriteFile",
    "Bash",
)
AGENT_MAX_STEPS = 8
AGENT_RESULT_MAX_CHARS = 12_000
AGENT_LIST_CAP = 40
_KIND_ALIASES = {"explore": "research", "plan": "planning"}

AGENT_EXPLORE_SYSTEM = (
    "You are a Research Subagent. Inspect the real project with ReadFile, "
    "SearchContent, and ListDir only. You cannot write files, run a shell, "
    "save learn guides, or spawn another Agent. SearchContent can read "
    ".g023/bbs/ for prior-session findings; do not dump the board. Call those "
    "tools, then return STRICT JSON: "
    '{"kind":"research","files":["..."],"symbols":["..."],"findings":["..."],'
    '"research":"..."}. '
    "files and symbols must come from tool results, not from guessing. "
    "research is a compact relay of what you learned for the next worker."
)
AGENT_PLAN_SYSTEM = (
    "You are a Planning Subagent. Inspect the real project with ReadFile, "
    "SearchContent, and ListDir only. You cannot write files, run a shell, "
    "save learn guides, or spawn another Agent. Use any prior research relay "
    "in the user message. Call those tools, then return STRICT JSON: "
    '{"kind":"planning","files":["..."],"symbols":["..."],"findings":["..."],'
    '"plan_steps":["..."],"open_checks":["..."],"plan":"..."}. '
    "files and symbols must come from tool results, not from guessing. "
    "plan is a compact relay for the todos worker."
)
AGENT_TODOS_SYSTEM = (
    "You are a Todos Subagent. Produce a JSON todo list for the objective. "
    "Use any prior plan/research relay in the user message. "
    "You cannot implement the todos yourself. Return STRICT JSON: "
    '{"kind":"todos","todos":[{"id":"1","title":"..."}]}. '
    "Keep titles short and actionable."
)
AGENT_TASK_SYSTEM = (
    "You are a Task Subagent. Implement ONE todo. If the previous task "
    "surfaced issues, fix those as well as implementing your todo. "
    "You may append durable findings to .g023/bbs/ (topic-slug dir, slug.txt) "
    "and must not leave a surfaced issue unrecorded. "
    "Return STRICT JSON: "
    '{"kind":"task","result":"...","issues":["..."]}. '
    "issues is a list of problems you hit or left for the next task; "
    "use an empty list if none. Do not return a nested transcript."
)


def _as_int(value: Any) -> Optional[int]:
    """Models sometimes send line numbers as strings — take either, or nothing."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def agent_readonly_schemas() -> list:
    """Schemas offered to the nested Agent call. Not added to the parent list."""
    return [s for s in TOOL_SCHEMAS if s.get("name") in AGENT_READONLY_TOOLS]


def agent_task_schemas() -> list:
    """Schemas offered to a task worker. Not added to the parent list."""
    return [s for s in TOOL_SCHEMAS if s.get("name") in AGENT_TASK_TOOLS]


def _normalize_todos(tasks: Any) -> list[dict]:
    if not tasks:
        return []
    if isinstance(tasks, str):
        tasks = [tasks]
    if not isinstance(tasks, list):
        return []
    out: list[dict] = []
    for i, item in enumerate(tasks, start=1):
        if isinstance(item, dict):
            title = item.get("title") or item.get("id") or str(item)
            out.append({"id": str(item.get("id") or i), "title": str(title)})
        else:
            out.append({"id": str(i), "title": str(item)})
    return out


def clamp_ds_reasoning(kind: str, reasoning: Optional[str]) -> tuple[bool, str]:
    """Map the Agent schema's reasoning arg onto DeepSeek effort.

    Schema: none|low|high (``medium`` is still accepted from older prompts and
    maps to ``high``, as DeepSeek itself does). This harness never sends
    ``max`` (``MAX_EFFORT`` is 75 / ``high``). Unset default: ``high`` for every
    kind. (Todos used to default to ``medium``, which DeepSeek folds into
    ``high`` server-side — so ``high`` is what it always really ran at.)
    ``max`` often burns the entire ``max_output_tokens`` budget on thinking
    and returns empty JSON. Returns (enabled, effort) for
    :func:`reasoning_param`.
    """
    default = "high"
    if not reasoning:
        return True, default
    token = str(reasoning).strip().lower()
    if token in ("none", "off"):
        return False, "none"
    parsed = parse_effort_value(token)
    if parsed is None:
        return True, default
    if parsed <= 0:
        return False, "none"
    return True, effort_to_wire(parsed)


def _extract_json_object(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if not text:
        return None
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except (TypeError, ValueError):
            return None
    return None


def _harvest_from_tool(bucket: dict, name: str, raw: str) -> None:
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return
    if not isinstance(data, dict):
        return

    files: list = bucket["files"]
    symbols: list = bucket["symbols"]
    findings: list = bucket["findings"]

    def add_file(path: Any) -> None:
        if not path:
            return
        text = str(path).rstrip("/")
        if text and text not in files:
            files.append(text)

    if name == "ListDir":
        add_file(data.get("path"))
        for entry in data.get("entries") or []:
            if isinstance(entry, dict):
                add_file(entry.get("name"))
    elif name == "ReadFile":
        add_file(data.get("path"))
        meta = data.get("metadata") or {}
        syms = meta.get("symbols") or {}
        if isinstance(syms, dict):
            for symbol in syms:
                if symbol not in symbols:
                    symbols.append(symbol)
        summary = data.get("summary")
        if summary:
            findings.append(str(summary)[:400])
    elif name == "SearchContent":
        for path in data.get("files") or []:
            add_file(path)
        for match in data.get("matches") or data.get("samples") or []:
            if isinstance(match, dict):
                add_file(match.get("file"))


def _merge_agent_payload(kind: str, objective: str, text: str, harvested: dict) -> dict:
    payload: dict[str, Any] = {
        "kind": kind,
        "objective": objective,
        "files": list(harvested.get("files") or []),
        "symbols": list(harvested.get("symbols") or []),
        "findings": list(harvested.get("findings") or []),
    }
    if kind in ("plan", "planning"):
        payload["plan_steps"] = list(harvested.get("plan_steps") or [])
        payload["open_checks"] = list(harvested.get("open_checks") or [])
    if kind == "task":
        payload["issues"] = list(harvested.get("issues") or [])
        payload["result"] = harvested.get("result") or ""
    parsed = _extract_json_object(text)
    if isinstance(parsed, dict):
        for key in ("files", "symbols", "findings", "plan_steps", "open_checks", "issues", "todos"):
            extra = parsed.get(key)
            if not extra:
                continue
            payload.setdefault(key, [])
            items = extra if isinstance(extra, list) else [extra]
            for item in items:
                if item not in payload[key]:
                    payload[key].append(item)
        for key in ("research", "plan", "summary", "result"):
            extra = parsed.get(key)
            if extra:
                payload[key] = extra if isinstance(extra, str) else json.dumps(extra, ensure_ascii=False)
        exploration = parsed.get("plan_or_exploration") or parsed.get("summary")
        if exploration and not payload.get("findings"):
            payload["findings"] = [str(exploration)[:2000]]
    elif (text or "").strip():
        payload["findings"] = payload.get("findings") or [text.strip()[:2000]]
        if kind == "task" and not payload.get("result"):
            payload["result"] = text.strip()[:2000]
    for key in ("files", "symbols", "findings", "plan_steps", "open_checks", "issues", "todos"):
        if isinstance(payload.get(key), list):
            payload[key] = payload[key][:AGENT_LIST_CAP]
    return payload


def _cap_agent_json(payload: dict) -> str:
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) <= AGENT_RESULT_MAX_CHARS:
        return raw
    payload = dict(payload)
    payload["truncated"] = True
    findings = list(payload.get("findings") or [])
    while findings and len(json.dumps(payload, ensure_ascii=False)) > AGENT_RESULT_MAX_CHARS:
        findings.pop()
        payload["findings"] = findings
    raw = json.dumps(payload, ensure_ascii=False)
    if len(raw) > AGENT_RESULT_MAX_CHARS:
        payload["findings"] = []
        raw = json.dumps(payload, ensure_ascii=False)[:AGENT_RESULT_MAX_CHARS]
    return raw


class SubagentRouter:
    def __init__(self, client: Optional[DeepSeekClient] = None):
        self.client = client
        # Frozen parent prefix (instructions, tools, reasoning). When set,
        # nested Agent calls reuse it so they hit the standing cache units
        # instead of missing from token 0 on a different system prompt.
        self._shared_prefix: Optional[dict] = None

    def set_shared_prefix(
        self,
        instructions: str,
        tools: list,
        reasoning: Optional[dict] = None,
        temperature: Optional[float] = None,
    ) -> None:
        self._shared_prefix = {
            "instructions": instructions,
            "tools": list(tools),
            "reasoning": dict(reasoning) if reasoning else None,
            "temperature": temperature,
        }

    def clear_shared_prefix(self) -> None:
        self._shared_prefix = None

    def _get_client(self) -> DeepSeekClient:
        if self.client is None:
            self.client = get_client()
        return self.client

    async def delegate(self, tool_name: str, arguments: dict) -> str:
        """
        Route a heavy tool call to the appropriate subagent.
        Always returns a compact string (usually JSON) for the orchestrator.
        """
        if tool_name == "ReadFile":
            return await run_file_reader(
                path=arguments.get("path", ""),
                focus=arguments.get("focus"),
                raw=bool(arguments.get("raw", False)),
                start_line=_as_int(arguments.get("start_line")),
                end_line=_as_int(arguments.get("end_line")),
                client=self._get_client(),
            )

        if tool_name == "SearchContent":
            return await run_searcher(
                query=arguments.get("query", ""),
                path=arguments.get("path"),
                max_matches=int(arguments.get("max_matches", settings.max_search_matches)),
                file_glob=arguments.get("file_glob"),
            )

        if tool_name == "AnalyzeImage":
            return await self._run_vision(
                path_or_url=arguments.get("path_or_url", ""),
                question=arguments.get("question", "Describe this image focusing on any technical or code content."),
            )

        if tool_name == "Agent":
            if not getattr(settings, "subagents_enabled", False):
                return json.dumps(
                    {
                        "error": "subagents are disabled",
                        "note": "Do the work directly with your own tools. The user can "
                        "enable subagents with /subagents on.",
                    }
                )
            kind = arguments.get("kind", "explore")
            objective = arguments.get("objective", "")
            reasoning = arguments.get("reasoning")
            return await self._run_agent(
                kind,
                objective,
                reasoning,
                prior=arguments.get("prior"),
                issues=arguments.get("issues"),
                tasks=arguments.get("tasks"),
            )

        return json.dumps({"error": f"Unknown subagent tool: {tool_name}"})

    async def _run_vision(self, path_or_url: str, question: str) -> str:
        return await run_vision(path_or_url=path_or_url, question=question)

    async def _run_agent(
        self,
        kind: str,
        objective: str,
        reasoning: Optional[str] = None,
        prior: Any = None,
        issues: Any = None,
        tasks: Any = None,
    ) -> str:
        """
        Research / planning / todos / task subagent (explore/plan still alias).

        Isolated loop: function_call items live on a local list and are never
        appended to the parent orchestrator history. Workers relay structured
        payloads (research, plan, issues, summary) both ways via ``prior`` /
        ``issues`` so the parent never ingests a nested transcript.
        """
        resolved = _KIND_ALIASES.get(kind, kind)
        if resolved == "todos":
            return await self._run_todos(objective, reasoning, prior=prior, tasks=tasks)
        if resolved == "task":
            todo = {"id": "1", "title": objective}
            return await self._run_task(objective, todo, list(issues or []), reasoning, prior=prior)
        return await self._run_agent_ds(resolved, objective, reasoning, prior=prior)

    async def _exec_agent_tool(
        self,
        name: str,
        arguments: dict,
        allowed: tuple = AGENT_READONLY_TOOLS,
    ) -> str:
        """Run one nested-agent tool. Unknown names are refused."""
        if name not in allowed:
            return json.dumps(
                {"error": f"{name} is not available to this subagent"},
                ensure_ascii=False,
            )
        if name == "ReadFile":
            return await run_file_reader(
                path=arguments.get("path", ""),
                focus=arguments.get("focus"),
                raw=bool(arguments.get("raw", False)),
                start_line=_as_int(arguments.get("start_line")),
                end_line=_as_int(arguments.get("end_line")),
                client=self._get_client(),
            )
        if name == "SearchContent":
            return await run_searcher(
                query=arguments.get("query", ""),
                path=arguments.get("path"),
                max_matches=int(arguments.get("max_matches", settings.max_search_matches)),
                file_glob=arguments.get("file_glob"),
            )
        from ..tools.registry import get_registry

        registry = get_registry()
        if name == "ListDir":
            return await registry._exec_list_dir(
                path=arguments.get("path") or ".",
                recursive=bool(arguments.get("recursive", False)),
            )
        if name == "Glob":
            return await registry._exec_glob(
                pattern=arguments.get("pattern") or "*",
                path=arguments.get("path") or ".",
            )
        if name in ("WriteFile", "Edit", "Bash"):
            allowed_perm = await registry.check_permission(name, arguments)
            if not allowed_perm:
                return json.dumps({"error": "User denied permission", "tool": name})
            return await registry.execute(name, arguments, "subagent")
        return json.dumps({"error": f"{name} is not available to this subagent"})

    # -- nested DeepSeek loop ---------------------------------------------


    def _agent_system(self, kind: str, system_override: Optional[str] = None) -> str:
        if system_override:
            return system_override
        if kind in ("plan", "planning"):
            return AGENT_PLAN_SYSTEM
        if kind in ("todos", "todos_plan"):
            return AGENT_TODOS_SYSTEM
        if kind == "task":
            return AGENT_TASK_SYSTEM
        return AGENT_EXPLORE_SYSTEM

    def _agent_user_content(
        self,
        kind: str,
        objective: str,
        prior: Any = None,
        *,
        system_override: Optional[str] = None,
        allowed_tools: tuple = AGENT_READONLY_TOOLS,
        share_prefix: bool = False,
    ) -> str:
        parts: list[str] = []
        if share_prefix:
            parts.append(self._agent_system(kind, system_override))
            parts.append(
                "You are a nested worker on the parent's standing prefix. "
                f"Only call {', '.join(allowed_tools)} — any other tool fails closed. "
                "Do not spawn Agent. Do not WriteFile/Edit/Bash unless those names "
                "are in that list. Return the JSON object the brief asks for."
            )
        parts.append(f"Objective: {objective}")
        if prior:
            prior_text = prior if isinstance(prior, str) else json.dumps(prior, ensure_ascii=False)
            parts.append(f"Prior relay payload:\n{prior_text[:8000]}")
        if kind == "task":
            parts.append(
                "Implement this todo. Fix any issues listed in the prior relay. "
                "Return the JSON object."
            )
        elif kind in ("todos", "todos_plan"):
            parts.append("Return the JSON todo list. Do not implement the todos.")
        else:
            parts.append("Inspect the project with your tools, then return the JSON object.")
        return "\n\n".join(parts)

    def _write_branch_log(
        self,
        kind: str,
        objective: str,
        payload: dict,
        items: list,
    ) -> Optional[str]:
        """Scratch outline of an isolated Agent branch. Parent sees the path."""
        try:
            root = get_project_root() / ".g023" / "scratch" / "agent"
            root.mkdir(parents=True, exist_ok=True)
            stamp = hashlib.sha256(
                f"{kind}:{objective}:{time.time()}".encode("utf-8")
            ).hexdigest()[:12]
            path = root / f"{kind}-{stamp}.json"
            outline = []
            for it in items:
                row = {"type": it.get("type") or it.get("role")}
                if it.get("name"):
                    row["name"] = it.get("name")
                blob = it.get("output") or it.get("content") or ""
                if isinstance(blob, list):
                    blob = "".join(
                        (p.get("text") or "") for p in blob if isinstance(p, dict)
                    )
                row["chars"] = len(blob) if isinstance(blob, str) else 0
                outline.append(row)
            path.write_text(
                json.dumps(
                    {
                        "kind": kind,
                        "objective": (objective or "")[:2000],
                        "steps": len(items),
                        "payload": payload,
                        "transcript_outline": outline,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            try:
                return str(path.relative_to(get_project_root()))
            except ValueError:
                return str(path)
        except Exception:
            return None

    async def _run_agent_ds(
        self,
        kind: str,
        objective: str,
        reasoning: Optional[str] = None,
        prior: Any = None,
        system_override: Optional[str] = None,
        allowed_tools: tuple = AGENT_READONLY_TOOLS,
    ) -> str:
        """Isolated tool loop, depth 1. Transcript never lands on the parent.

        When the parent has published a shared prefix, this call reuses those
        instructions / tools / effort so the standing cache units hit. The
        worker brief moves into the first user item. Execution still refuses
        any tool not in ``allowed_tools``.
        """
        from ..orchestrator import parse_reply

        client = self._get_client()
        prefix = self._shared_prefix
        share = bool(prefix and prefix.get("instructions") and prefix.get("tools"))
        enabled, effort = clamp_ds_reasoning(kind, reasoning)
        if share:
            system = prefix["instructions"]
            schemas = prefix["tools"]
            reasoning_wire = prefix.get("reasoning") or reasoning_param(
                enabled=enabled, effort=effort
            )
            shared_temp = prefix.get("temperature")
            temperature = (
                float(shared_temp)
                if shared_temp is not None
                else settings.temperature
            )
        else:
            system = self._agent_system(kind, system_override)
            schemas = (
                agent_task_schemas()
                if allowed_tools == AGENT_TASK_TOOLS
                else agent_readonly_schemas()
            )
            reasoning_wire = reasoning_param(enabled=enabled, effort=effort)
            temperature = 0.3
        items: list[dict] = [
            {
                "role": "user",
                "content": self._agent_user_content(
                    kind,
                    objective,
                    prior,
                    system_override=system_override,
                    allowed_tools=allowed_tools,
                    share_prefix=share,
                ),
            }
        ]
        harvested: dict[str, list] = {
            "files": [],
            "symbols": [],
            "findings": [],
            "plan_steps": [],
            "open_checks": [],
            "issues": [],
        }
        last_text = ""
        last_reasoning = ""
        cut = ""

        try:
            for _step in range(AGENT_MAX_STEPS):
                create_kwargs: dict[str, Any] = {
                    "model": MODEL,
                    "instructions": system,
                    "input": list(items),
                    "tools": schemas,
                    "temperature": temperature,
                    "reasoning": reasoning_wire,
                }
                # Omitting max_output_tokens matches the parent body. A live
                # probe showed adding the field busts the standing prefix.
                if not share:
                    create_kwargs["max_output_tokens"] = 8192
                resp = await client.create(**create_kwargs)
                get_usage().record(MODEL, resp.get("usage"), scope="subagent")
                reply = parse_reply(resp)
                last_text = reply.content or reply.commentary or output_text(resp)
                last_reasoning = reply.reasoning or reasoning_text(resp)
                cut = reply.incomplete_reason or incomplete_reason(resp)
                # Isolated transcript: echo the server items here, never on the parent.
                items.extend(reply.output)
                if not reply.tool_calls:
                    break
                for tc in reply.tool_calls:
                    args = tc.parsed()
                    result = await self._exec_agent_tool(tc.name, args, allowed=allowed_tools)
                    _harvest_from_tool(harvested, tc.name, result)
                    stored = result
                    full_read = (
                        tc.name == "ReadFile"
                        and bool(args.get("raw"))
                        and not args.get("start_line")
                        and not args.get("end_line")
                    )
                    if should_vault_tool_output(tc.name, stored, raw_read=full_read):
                        try:
                            from .. import salience

                            intent = salience.call_intent(
                                salience.step_intent(last_reasoning or "", reply.commentary or "", objective),
                                args,
                            )
                            stored = get_cache().offload_tool_output(
                                stored, intent=intent, salient=settings.salience_excerpt
                            )
                        except Exception:
                            stored = result
                    items.append(
                        {
                            "type": "function_call_output",
                            "call_id": tc.id,
                            "output": stored,
                        }
                    )
            payload = _merge_agent_payload(kind, objective, last_text, harvested)
            payload["reasoning_excerpt"] = (last_reasoning[:800] + "…") if last_reasoning else None
            branch = self._write_branch_log(kind, objective, payload, items)
            if branch:
                payload["branch"] = branch
            if cut:
                payload["truncated"] = cut
                if not last_text and not payload.get("files") and not payload.get("findings"):
                    payload["error"] = (
                        f"Subagent produced no answer: output stopped early ({cut}), "
                        "with the whole budget spent on reasoning."
                    )
            return _cap_agent_json(payload)
        except Exception as e:
            return json.dumps({"error": str(e), "kind": kind})

    async def _run_task(
        self,
        objective: str,
        todo: dict,
        prior_issues: list,
        reasoning: Optional[str] = None,
        prior: Any = None,
    ) -> str:
        """One task worker: implement ``todo``, fix ``prior_issues``, relay issues."""
        relay = {
            "todo": todo,
            "issues": list(prior_issues or []),
        }
        if prior:
            relay["prior"] = prior
        return await self._run_agent_ds(
            "task",
            f"{objective} — todo {todo.get('id')}: {todo.get('title')}",
            reasoning,
            prior=json.dumps(relay, ensure_ascii=False),
            system_override=AGENT_TASK_SYSTEM,
            allowed_tools=AGENT_TASK_TOOLS,
        )

    def _todos_summary(self, objective: str, results: list[dict]) -> str:
        lines = [f"Objective: {objective}"]
        for row in results:
            todo = row.get("todo") or {}
            title = todo.get("title") or todo.get("id") or "?"
            result = row.get("result") or ""
            issues = row.get("issues") or []
            line = f"- {title}: {result}".rstrip()
            if issues:
                line += f" (issues: {'; '.join(str(i) for i in issues)})"
            lines.append(line)
        return "\n".join(lines)

    async def _run_todos(
        self,
        objective: str,
        reasoning: Optional[str] = None,
        prior: Any = None,
        tasks: Any = None,
    ) -> str:
        """Create a todo list, orchestrate task workers, relay a final summary.

        Each task worker receives issues surfaced by the previous task.
        The parent sees only the compact summary payload, not nested transcripts.
        """
        todos = _normalize_todos(tasks)
        if not todos:
            raw = await self._run_agent_ds(
                "todos_plan",
                objective,
                reasoning,
                prior=prior,
                system_override=AGENT_TODOS_SYSTEM,
            )
            parsed = _extract_json_object(raw) or {}
            todos = _normalize_todos(parsed.get("todos"))
            if not todos:
                todos = [{"id": "1", "title": objective}]

        prior_issues: list = []
        results: list[dict] = []
        for todo in todos:
            raw = await self._run_task(objective, todo, prior_issues, reasoning, prior=prior)
            payload = _extract_json_object(raw) or {}
            issues = payload.get("issues") or []
            if isinstance(issues, str):
                issues = [issues]
            if not isinstance(issues, list):
                issues = [str(issues)]
            result = payload.get("result") or ""
            if not result and payload.get("findings"):
                findings = payload["findings"]
                result = findings[0] if isinstance(findings, list) and findings else str(findings)
            results.append({"todo": todo, "result": result, "issues": issues})
            prior_issues = list(issues)

        summary = self._todos_summary(objective, results)
        return _cap_agent_json(
            {
                "kind": "todos",
                "objective": objective,
                "todos": todos,
                "summary": summary,
                "issues": prior_issues,
                "results": [
                    {
                        "id": r["todo"].get("id"),
                        "title": r["todo"].get("title"),
                        "result": r["result"],
                        "issues": r["issues"],
                    }
                    for r in results
                ],
            }
        )


_router: Optional[SubagentRouter] = None


def get_router() -> SubagentRouter:
    global _router
    if _router is None:
        _router = SubagentRouter()
    return _router
