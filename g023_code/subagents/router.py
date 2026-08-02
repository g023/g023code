"""
Subagent Router — intercepts heavy tools and delegates to isolated subagents.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openai import AsyncOpenAI

from ..config import load_api_key, settings
from .file_reader import run_file_reader
from .searcher import run_searcher


class SubagentRouter:
    def __init__(self, client: Optional[AsyncOpenAI] = None):
        self.client = client

    def _get_client(self) -> AsyncOpenAI:
        if self.client is None:
            self.client = AsyncOpenAI(api_key=load_api_key(), base_url=settings.base_url)
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
            kind = arguments.get("kind", "explore")
            objective = arguments.get("objective", "")
            return await self._run_agent(kind, objective)

        return json.dumps({"error": f"Unknown subagent tool: {tool_name}"})

    async def _run_vision(self, path_or_url: str, question: str) -> str:
        # Vision is external. For now return a clear message; user can extend.
        backend = settings.vision_backend
        if backend == "none":
            return json.dumps(
                {
                    "error": "Vision backend not configured.",
                    "hint": "Set vision_backend in settings or use /vision backend <name>. "
                            "DeepSeek V4 is text-only; an external VLM (GLM-4V, GPT-4V, local LLaVA) is required.",
                    "path_or_url": path_or_url,
                    "question": question,
                }
            )
        # Placeholder for real integration
        return json.dumps(
            {
                "backend": backend,
                "path_or_url": path_or_url,
                "question": question,
                "summary": "(Vision backend stub — integrate your preferred VLM here)",
            }
        )

    async def _run_agent(self, kind: str, objective: str) -> str:
        """Lightweight Explore / Plan subagent using Flash with thinking."""
        client = self._get_client()
        if kind == "plan":
            system = (
                "You are a Plan Subagent. Produce a clear, step-by-step implementation plan "
                "for the given objective. Be concrete about files to touch, order of operations, "
                "and verification steps. Output markdown."
            )
            effort = "max"
        else:
            system = (
                "You are an Explore Subagent. Investigate the codebase related to the objective. "
                "You have no tools in this isolated turn — reason from the objective alone and "
                "suggest which files or searches the orchestrator should perform next. "
                "Be concise."
            )
            effort = "high"

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"Objective: {objective}\n\nProject root context is available to the orchestrator; focus on high-level strategy."},
        ]

        try:
            resp = await client.chat.completions.create(
                model=settings.subagent_model,
                messages=messages,
                max_tokens=2048,
                temperature=0.3,
                reasoning_effort=effort,
                extra_body={"thinking": {"type": "enabled"}},
            )
            content = resp.choices[0].message.content or ""
            reasoning = getattr(resp.choices[0].message, "reasoning_content", None)
            return json.dumps(
                {
                    "kind": kind,
                    "objective": objective,
                    "plan_or_exploration": content,
                    "reasoning_excerpt": (reasoning[:800] + "…") if reasoning else None,
                },
                ensure_ascii=False,
            )
        except Exception as e:
            return json.dumps({"error": str(e), "kind": kind})


_router: Optional[SubagentRouter] = None


def get_router() -> SubagentRouter:
    global _router
    if _router is None:
        _router = SubagentRouter()
    return _router
