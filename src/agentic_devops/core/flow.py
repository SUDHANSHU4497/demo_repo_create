"""Builds the agents' LangGraph state graphs.

Each agent workflow is a list of steps (graph nodes). A node returns a partial state update. Any
node can finish the workflow by putting an ``AgentResult`` in ``state["result"]`` (a guardrail that
blocks, a skip, a failure, or the final outcome); the edge after every node checks for it. Steps
can also branch with a ``route`` function. Routing is plain Python - never an LLM decision.

    START -> step1 -> step2 -> ... -> stepN -> END
               \\________\\___________\\_____-> END   (when a step sets "result")
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import END, START, StateGraph


@dataclass
class Step:
    name: str
    fn: Callable[[Any], dict[str, Any]]
    route: Callable[[Any], str] | None = None  # returns the next step's name
    targets: Sequence[str] = field(default_factory=tuple)


def build_flow(state_schema: type, steps: list[Step], name: str):
    graph = StateGraph(state_schema)
    for step in steps:
        graph.add_node(step.name, step.fn)
    graph.add_edge(START, steps[0].name)
    names = [s.name for s in steps]
    for i, step in enumerate(steps):
        nxt = names[i + 1] if i + 1 < len(names) else END
        if step.route is not None:
            graph.add_conditional_edges(step.name, _guard(step.route), [*step.targets, END])
        elif nxt == END:
            graph.add_edge(step.name, END)
        else:
            graph.add_conditional_edges(step.name, _guard(lambda _s, n=nxt: n), [nxt, END])
    return graph.compile(name=name)


def _guard(route: Callable[[Any], str]) -> Callable[[Any], str]:
    def _next(state: Any) -> str:
        return END if state.get("result") is not None else route(state)

    return _next


def run_flow(graph: Any, initial: dict[str, Any]) -> Any:
    """Invoke a flow and return its AgentResult."""
    final = graph.invoke(initial, config={"run_name": graph.name, "recursion_limit": 100})
    result = final.get("result")
    if result is None:  # every flow must end with a result
        raise RuntimeError(f"Flow {graph.name} ended without a result")
    return result


def to_mermaid(graph: Any) -> str:
    """Mermaid for a compiled graph, without LangGraph's front matter/styling (renders in ADO wiki too)."""
    out = []
    in_front_matter = False
    for line in graph.get_graph().draw_mermaid().splitlines():
        if line.strip() == "---":
            in_front_matter = not in_front_matter
            continue
        if in_front_matter or line.strip().startswith("classDef"):
            continue
        line = line.replace("([<p>__start__</p>]):::first", "([start])").replace(
            "([<p>__end__</p>]):::last", "([end])"
        )
        out.append(line.replace("\t", "    ").rstrip(";"))
    return "\n".join(out).strip()
