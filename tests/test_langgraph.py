"""Tests for the LangGraph foundation (StateGraph + supervisor/worker)."""

import pytest

from app.services.langgraph import END, START, CompiledGraph, GraphState, StateGraph
from app.services.langgraph.supervisor import SupervisorGraph


def test_state_graph_runs_linear_nodes():
    g = StateGraph()

    def a(state: GraphState) -> dict:
        return {"x": 1}

    def b(state: GraphState) -> dict:
        return {"y": state.get("x", 0) + 10}

    g.add_node("a", a)
    g.add_node("b", b)
    g.add_edge(START, "a")
    g.add_edge("a", "b")
    g.add_edge("b", END)

    compiled: CompiledGraph = g.compile()
    state = compiled.invoke(GraphState(data={"seed": 0}))
    assert state.get("x") == 1
    assert state.get("y") == 11
    assert state.error is None


def test_state_graph_conditional_routing():
    g = StateGraph()

    def decide(state: GraphState) -> dict:
        return {}

    def high(state: GraphState) -> dict:
        return {"route": "high"}

    def low(state: GraphState) -> dict:
        return {"route": "low"}

    g.add_node("decide", decide)
    g.add_node("high", high)
    g.add_node("low", low)

    def router(state: GraphState) -> str:
        return "high" if state.get("value", 0) > 5 else "low"

    g.add_edge(START, "decide")
    g.add_conditional_edges("decide", router)
    g.add_edge("high", END)
    g.add_edge("low", END)

    compiled = g.compile()
    assert compiled.invoke(GraphState(data={"value": 10})).get("route") == "high"
    assert compiled.invoke(GraphState(data={"value": 1})).get("route") == "low"


def test_state_graph_isolates_node_failure():
    g = StateGraph()

    def boom(state: GraphState) -> dict:
        raise RuntimeError("kaboom")

    g.add_node("boom", boom)
    g.add_edge(START, "boom")
    g.add_edge("boom", END)

    compiled = g.compile()
    state = compiled.invoke(GraphState())
    assert state.error is not None
    assert "kaboom" in state.error


@pytest.mark.asyncio
async def test_supervisor_routes_to_grafana_worker():
    sg = SupervisorGraph()

    captured = {}

    def grafana_worker(state: GraphState) -> dict:
        captured["hit"] = True
        return {"grafana": "ok"}

    sg.register_worker("grafana", grafana_worker)
    graph = sg.compile()

    state = await graph.ainvoke(GraphState(data={"task": "show grafana metrics"}))
    assert captured.get("hit") is True
    assert state.get("worker_results", {}).get("grafana") == {"grafana": "ok"}


@pytest.mark.asyncio
async def test_supervisor_noop_when_unmatched():
    sg = SupervisorGraph()

    def telegram_worker(state: GraphState) -> dict:
        return {"telegram": "ok"}

    sg.register_worker("telegram", telegram_worker)
    graph = sg.compile()

    state = await graph.ainvoke(GraphState(data={"task": "do something unrelated"}))
    assert state.get("worker_results", {}).get("noop", {}).get("status") == "no_worker_matched"
