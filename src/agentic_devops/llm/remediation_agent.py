"""Remediation sub-agent: a LangGraph tool-calling loop with read-only tools.

    START -> investigate --(tool calls & rounds left)--> tools -> investigate ... -> END

The model can read data contracts, findings, the naming glossary and the files changed in the PR,
so its fix suggestions point at the actual PySpark/SQL that wrote the table. Tools cannot write
anything, rounds are capped, and the result is advisory text only.
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool, tool
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode

from agentic_devops.llm.base import RemediationContext
from agentic_devops.models import Severity, ValidationReport

MAX_FILE_CHARS = 12_000


class InvestigationState(MessagesState):
    rounds: int


def _norm(path: str) -> str:
    return path.replace("\\", "/").lstrip("/")


def build_remediation_tools(
    report: ValidationReport, ctx: RemediationContext, share_samples: bool, share_code: bool
) -> list[BaseTool]:
    contracts = {c.table.lower(): c for c in ctx.contracts}
    findings: dict[str, list[dict[str, Any]]] = {}
    for f in report.all_findings:
        if f.severity is Severity.INFO:
            continue
        d = f.to_dict()
        if not share_samples:
            d["details"] = {k: v for k, v in d["details"].items() if k != "sample_duplicates"}
        findings.setdefault((f.table or "(cross-table)").lower(), []).append(d)

    @tool
    def get_data_contract(table: str) -> str:
        """Return the data contract (primary key, columns, types, nullability, source paths) of a table, e.g. 'sales.orders'."""
        c = contracts.get(table.strip().lower())
        return (
            json.dumps(c.to_dict(), default=str)
            if c
            else f"No data contract for '{table}'. Known: {sorted(contracts)}"
        )

    @tool
    def get_findings(table: str) -> str:
        """Return the validation findings (rule id, column, message, details) for one table."""
        items = findings.get(table.strip().lower())
        return json.dumps(items, default=str)[:8000] if items else f"No findings for '{table}'."

    @tool
    def suggest_column_name(column: str) -> str:
        """Return the glossary-standard snake_case name for a column and any non-standard abbreviations it uses."""
        if ctx.naming is None:
            return "No naming standard configured."
        return json.dumps(
            {
                "column": column,
                "standard_name": ctx.naming.canonical_name(column),
                "abbreviations": ctx.naming.abbreviation_tokens(column),
            }
        )

    tools: list[BaseTool] = [get_data_contract, get_findings, suggest_column_name]

    if share_code and ctx.read_file and ctx.changed_files:
        allowed = {_norm(p): p for p in ctx.changed_files}

        @tool
        def list_changed_files() -> str:
            """List the files changed in this pull request."""
            return "\n".join(sorted(allowed))[:8000]

        @tool
        def read_changed_file(path: str) -> str:
            """Read one file changed in this pull request (first 12,000 characters)."""
            original = allowed.get(_norm(path))
            if original is None:
                return "Only files changed in this pull request can be read. Call list_changed_files first."
            try:
                text = ctx.read_file(original)
            except Exception as exc:  # noqa: BLE001
                return f"Could not read {path}: {type(exc).__name__}"
            return text[:MAX_FILE_CHARS] + ("\n…(truncated)" if len(text) > MAX_FILE_CHARS else "")

        tools += [list_changed_files, read_changed_file]
    return tools


def build_investigation_graph(model_with_tools: Runnable, tools: list[BaseTool], max_rounds: int):
    def investigate(state: InvestigationState) -> dict[str, Any]:
        return {
            "messages": [model_with_tools.invoke(state["messages"])],
            "rounds": state.get("rounds", 0) + 1,
        }

    def route(state: InvestigationState) -> str:
        last = state["messages"][-1]
        if isinstance(last, AIMessage) and last.tool_calls and state.get("rounds", 0) <= max_rounds:
            return "tools"
        return END

    graph = StateGraph(InvestigationState)
    graph.add_node("investigate", investigate)
    graph.add_node("tools", ToolNode(tools))
    graph.add_edge(START, "investigate")
    graph.add_conditional_edges("investigate", route, ["tools", END])
    graph.add_edge("tools", "investigate")
    return graph.compile(name="remediation-investigation")


def investigation_notes(messages: list[BaseMessage], limit: int = 20_000) -> str:
    """Condense the tool transcript into plain notes, so the final answer can come from any model."""
    parts = []
    for m in messages:
        if isinstance(m, ToolMessage):
            parts.append(f"[tool {m.name}]\n{str(m.content)[:4000]}")
        elif isinstance(m, AIMessage) and m.text.strip():
            parts.append(f"[analysis]\n{m.text.strip()[:4000]}")
    return "\n\n".join(parts)[:limit]
