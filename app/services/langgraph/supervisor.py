"""LangGraph supervisor→worker orchestrator (TeleMon AI Platform Phase 1).

A reusable graph that routes an incoming "task" to one or more worker nodes
selected by a supervisor. Workers are registered by name; the supervisor returns
a list of worker keys (or "noop"). This is the foundation the MCP Gateway uses
to decide which MCP server(s) should handle a tool call.

In Phase 1 the supervisor is a simple keyword/intent router (no LLM required),
so it works even when DeepSeek is unconfigured. Later phases can swap the
supervisor for an LLM-driven planner without changing the worker contract.
"""

from __future__ import annotations

from typing import Callable

from app.services.langgraph import END, START, CompiledGraph, GraphState, StateGraph

# A worker is an async or sync function (GraphState) -> dict | None.
WorkerFn = Callable[[GraphState], object]

# Keyword → worker routing hints used by the default supervisor.
_SUPERVISOR_KEYWORDS: dict[str, str] = {
    "telegram": "telegram",
    "텔레그램": "telegram",
    "send": "telegram",
    "message": "telegram",
    "채팅": "telegram",
    "broadcast": "telegram",
    "grafana": "grafana",
    "대시보드": "grafana",
    "메트릭": "grafana",
    "metric": "grafana",
    "prometheus": "grafana",
    "log": "grafana",
    "로그": "grafana",
}


class SupervisorGraph:
    """Builds and caches a supervisor→worker StateGraph.

    Usage::
        sg = SupervisorGraph()
        sg.register_worker("telegram", telegram_worker)
        sg.register_worker("grafana", grafana_worker)
        graph = sg.compile()
        state = await graph.ainvoke(GraphState(data={"task": "send telegram"}))
    """

    def __init__(self) -> None:
        self._workers: dict[str, WorkerFn] = {}
        self._compiled: CompiledGraph | None = None

    def register_worker(self, name: str, fn: WorkerFn) -> None:
        self._workers[name] = fn
        self._compiled = None  # invalidate cache

    async def _supervisor(self, state: GraphState) -> dict:
        task = str(state.get("task", "")).lower()
        selected: list[str] = []
        for keyword, worker in _SUPERVISOR_KEYWORDS.items():
            if keyword in task and worker in self._workers:
                if worker not in selected:
                    selected.append(worker)
        if not selected:
            selected = ["noop"]
        state.set("selected_workers", selected)
        state.metadata["supervisor"] = {
            "task": state.get("task", ""),
            "selected": selected,
        }
        return {"selected_workers": selected}

    def _supervisor_router(self, state: GraphState) -> str:
        selected = state.get("selected_workers", []) or []
        if not selected or selected == ["noop"]:
            return "noop"
        return "dispatcher"

    async def _dispatcher(self, state: GraphState) -> dict:
        """Run every selected worker and collect their outputs keyed by name."""
        selected = state.get("selected_workers", []) or []
        results: dict[str, object] = {}
        for name in selected:
            fn = self._workers.get(name)
            if fn is None:
                results[name] = {"error": "unknown worker"}
                continue
            try:
                out = fn(state)
                if hasattr(out, "__await__"):
                    out = await out
                results[name] = out if out is not None else {}
            except Exception as exc:  # noqa: BLE001
                results[name] = {"error": str(exc)}
        state.set("worker_results", results)
        return {"worker_results": results}

    async def _noop(self, state: GraphState) -> dict:
        state.set("worker_results", {"noop": {"status": "no_worker_matched"}})
        return {"worker_results": {"noop": {"status": "no_worker_matched"}}}

    def compile(self) -> CompiledGraph:
        if self._compiled is not None:
            return self._compiled
        g = StateGraph()
        g.add_node("supervisor", self._supervisor)
        g.add_node("dispatcher", self._dispatcher)
        g.add_node("noop", self._noop)
        g.add_edge(START, "supervisor")
        g.add_conditional_edges("supervisor", self._supervisor_router)
        g.add_edge("dispatcher", END)
        g.add_edge("noop", END)
        self._compiled = g.compile()
        return self._compiled
