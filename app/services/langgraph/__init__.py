"""TeleMon LangGraph — lightweight graph runtime (Phase 1 foundation).

This is a self-contained, dependency-free implementation of the core LangGraph
mental model (StateGraph with typed state, nodes, conditional edges, and
interruptible execution). It intentionally avoids adding ``langgraph`` as a
production dependency: TeleMon's deployment is a single Docker image and we want
the AI orchestration layer to be available even when the optional Graphiti/LLM
extras are disabled.

The graph primitives here are the foundation that the MCP Gateway and the AI
Operations Center build on top of in later phases (agentic tool-calling,
human-in-the-loop approval, retries).

Design notes
------------
- ``StateGraph`` compiles to a ``CompiledGraph`` that runs nodes in topological
  order driven by conditional edges.
- Node output is merged into shared ``GraphState`` (last-write-wins by key).
- ``END`` is a sentinel; ``START`` is the implicit entry point.
- Conditional edges receive the current state and return the name of the next
  node, enabling supervisor/worker routing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Optional

logger_name = __name__

try:  # pragma: no cover - optional structured logger
    from app.core.logging import get_logger

    _log = get_logger(logger_name)
except Exception:  # pragma: no cover
    import logging as _logging

    _log = _logging.getLogger(logger_name)


# Sentinel routing targets.
START = "__start__"
END = "__end__"


# ─── State ─────────────────────────────────────────────────────────────────


@dataclass
class GraphState:
    """Shared, mutable state threaded through every node in a graph run.

    ``data`` holds arbitrary typed payloads (inputs, intermediate results,
    tool outputs). ``metadata`` carries control-plane info (run id, errors,
    the chosen route). ``next`` is set by conditional edges and read by the
    runner to decide routing.
    """

    data: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    next: Optional[str] = None
    error: Optional[str] = None

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)

    def fail(self, message: str) -> None:
        self.error = message
        self.metadata["failed"] = True


# ─── Node / Graph primitives ────────────────────────────────────────────────

NodeFn = Callable[[GraphState], Any]
ConditionFn = Callable[[GraphState], str]


@dataclass
class _Node:
    name: str
    fn: NodeFn


@dataclass
class _Edge:
    # An edge goes from ``src`` to ``dst``. When ``condition`` is set, ``dst`` is
    # ignored and the condition's returned name selects the next node.
    src: str
    dst: Optional[str] = None
    condition: Optional[ConditionFn] = None


class StateGraph:
    """Builder for a node/edge graph. Compile to a runnable graph."""

    def __init__(self, state_schema: type = GraphState) -> None:
        self._state_schema = state_schema
        self._nodes: dict[str, _Node] = {}
        self._edges: list[_Edge] = []
        self._entry: Optional[str] = None
        # Map of src node -> list of outgoing edges (for conditional routing).
        self._outgoing: dict[str, list[_Edge]] = {}

    def add_node(self, name: str, fn: NodeFn) -> "StateGraph":
        if name in (START, END):
            raise ValueError(f"node name '{name}' is reserved")
        self._nodes[name] = _Node(name=name, fn=fn)
        self._outgoing.setdefault(name, [])
        if self._entry is None:
            self._entry = name
        return self

    def add_edge(self, src: str, dst: str) -> "StateGraph":
        edge = _Edge(src=src, dst=dst)
        self._edges.append(edge)
        self._outgoing.setdefault(src, []).append(edge)
        return self

    def add_conditional_edges(
        self, src: str, condition: ConditionFn, *, path_map: Optional[dict[str, str]] = None
    ) -> "StateGraph":
        """Route from ``src`` using ``condition(state) -> key``.

        If ``path_map`` is provided, the returned key is mapped to a node name
        (allowing semantic route names like "worker:telegram" -> "telegram_node").
        """
        edge = _Edge(src=src, condition=condition)
        edge._path_map = path_map  # type: ignore[attr-defined]
        self._edges.append(edge)
        self._outgoing.setdefault(src, []).append(edge)
        return self

    def _resolve_entry(self) -> str:
        if self._entry is None:
            raise ValueError("graph has no nodes")
        return self._entry

    def compile(self) -> "CompiledGraph":
        # Validate that every referenced dst node exists (END is a valid target).
        for edge in self._edges:
            if edge.condition is not None:
                continue
            if edge.dst == END:
                continue
            if edge.dst not in self._nodes:
                raise ValueError(f"edge references unknown node '{edge.dst}'")
        return CompiledGraph(
            nodes=dict(self._nodes),
            outgoing=dict(self._outgoing),
            entry=self._resolve_entry(),
            state_schema=self._state_schema,
        )


class CompiledGraph:
    """Runnable graph. Execute via ``ainvoke`` (async) or ``invoke`` (sync)."""

    def __init__(
        self,
        nodes: dict[str, _Node],
        outgoing: dict[str, list[_Edge]],
        entry: str,
        state_schema: type,
    ) -> None:
        self._nodes = nodes
        self._outgoing = outgoing
        self._entry = entry
        self._state_schema = state_schema

    def _pick_next(self, current: str, state: GraphState) -> str:
        edges = self._outgoing.get(current, [])
        if not edges:
            return END
        # Prefer a conditional edge if present.
        for edge in edges:
            if edge.condition is not None:
                key = edge.condition(state)
                path_map = getattr(edge, "_path_map", None)
                if path_map and key in path_map:
                    key = path_map[key]
                if key == END:
                    return END
                if key not in self._nodes:
                    raise ValueError(f"conditional route '{key}' has no matching node")
                return key
        # Otherwise take the first unconditional edge.
        dst = edges[0].dst
        return END if dst == END else dst

    async def ainvoke(self, state: Optional[GraphState] = None, *, max_steps: int = 25) -> GraphState:
        if state is None:
            state = self._state_schema()
        current = self._entry
        steps = 0
        while current != END:
            steps += 1
            if steps > max_steps:
                state.fail("graph exceeded max_steps (possible cycle)")
                break
            node = self._nodes.get(current)
            if node is None:
                state.fail(f"unknown node '{current}'")
                break
            try:
                _log.debug("langgraph_node_enter", node=current)
                result = node.fn(state)
                if hasattr(result, "__await__"):
                    result = await result
                if isinstance(result, dict):
                    state.data.update(result)
            except Exception as exc:  # noqa: BLE001 — isolate node failures
                state.fail(f"node '{current}' raised: {exc}")
                _log.error("langgraph_node_error", node=current, error=str(exc))
                break
            if state.error:
                break
            current = self._pick_next(current, state)
        return state

    def invoke(self, state: Optional[GraphState] = None, *, max_steps: int = 25) -> GraphState:
        # Synchronous graphs only (no awaited nodes).
        if state is None:
            state = self._state_schema()
        current = self._entry
        steps = 0
        while current != END:
            steps += 1
            if steps > max_steps:
                state.fail("graph exceeded max_steps (possible cycle)")
                break
            node = self._nodes.get(current)
            if node is None:
                state.fail(f"unknown node '{current}'")
                break
            try:
                result = node.fn(state)
                if hasattr(result, "__await__"):
                    raise RuntimeError("use ainvoke for async graphs")
                if isinstance(result, dict):
                    state.data.update(result)
            except Exception as exc:  # noqa: BLE001
                state.fail(f"node '{current}' raised: {exc}")
                _log.error("langgraph_node_error", node=current, error=str(exc))
                break
            if state.error:
                break
            current = self._pick_next(current, state)
        return state
