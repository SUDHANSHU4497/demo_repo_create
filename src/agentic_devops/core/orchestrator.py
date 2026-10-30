"""Supervisor graph: routes pipeline commands and Azure DevOps service-hook events to the agents.

    START -> route --+--> data_validation        (validate / PR created, PR updated)
                     +--> qa_promotion           (promote-qa / push to feature branch, if enabled)
                     +--> qa_certification       (certify-qa)
                     +--> release_preparation    (release-prepare)
                     +--> release_finalization   (release-finalize)
                     +--> rollback_planning      (rollback-target)
                     +--> END                    (duplicate / unroutable event)

Routing is deterministic. Each agent node runs that agent's own LangGraph flow (a subgraph).
Agents are idempotent, so a duplicated or replayed event is harmless; ``seen`` only avoids
wasted work within one process (the hosted mode adds Service Bus duplicate detection).
"""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agentic_devops.agents.data_validation_agent import DataValidationAgent, ValidationRequest
from agentic_devops.agents.qa_promotion_agent import QaPromotionAgent
from agentic_devops.agents.release_agent import ReleaseAgent
from agentic_devops.core.agent import AgentContext
from agentic_devops.integrations.azure_devops import short_ref
from agentic_devops.models import AgentResult, AgentStatus

log = logging.getLogger(__name__)

COMMANDS = {
    "validate": "data_validation",
    "promote-qa": "qa_promotion",
    "certify-qa": "qa_certification",
    "release-prepare": "release_preparation",
    "release-finalize": "release_finalization",
    "rollback-target": "rollback_planning",
}


class SupervisorState(TypedDict, total=False):
    command: str
    args: dict[str, Any]
    event: dict[str, Any]
    result: AgentResult


class Orchestrator:
    name = "orchestrator"

    def __init__(
        self, ctx: AgentContext, validation: DataValidationAgent, qa: QaPromotionAgent, release: ReleaseAgent
    ):
        self.ctx = ctx
        self.validation = validation
        self.qa = qa
        self.release = release
        self.seen: set[str] = set()
        self.graph = self._build()

    def _build(self):
        g = StateGraph(SupervisorState)
        g.add_node("route", self._route)
        g.add_node(
            "data_validation", lambda s: {"result": self.validation.run(ValidationRequest(**s["args"]))}
        )
        g.add_node("qa_promotion", lambda s: {"result": self.qa.promote(**s["args"])})
        g.add_node("qa_certification", lambda s: {"result": self.qa.certify(**s["args"])})
        g.add_node("release_preparation", lambda s: {"result": self.release.prepare(**s["args"])})
        g.add_node("release_finalization", lambda s: {"result": self.release.finalize(**s["args"])})
        g.add_node("rollback_planning", lambda s: {"result": self.release.rollback_target(**s["args"])})
        g.add_edge(START, "route")
        targets = list(COMMANDS.values())
        g.add_conditional_edges(
            "route", lambda s: END if s.get("result") is not None else COMMANDS[s["command"]], [*targets, END]
        )
        for node in targets:
            g.add_edge(node, END)
        return g.compile(name="agentic-devops-supervisor")

    # ------------------------------------------------------------------ public API
    def run_command(self, command: str, **args: Any) -> AgentResult:
        if command not in COMMANDS:
            raise ValueError(f"Unknown command {command!r}. Known: {sorted(COMMANDS)}")
        return self._invoke({"command": command, "args": args})

    def handle_event(self, event: dict[str, Any]) -> AgentResult:
        return self._invoke({"event": event})

    def _invoke(self, state: SupervisorState) -> AgentResult:
        return self.graph.invoke(state, config={"run_name": self.graph.name, "recursion_limit": 25})["result"]

    # ------------------------------------------------------------------ routing
    def _skip(self, message: str) -> dict[str, Any]:
        return {"result": AgentResult(self.name, "route", AgentStatus.SKIPPED, message)}

    def _route(self, state: SupervisorState) -> dict[str, Any]:
        if state.get("command"):
            return {}
        event = state.get("event") or {}
        event_id = str(event.get("id") or "")
        if event_id and event_id in self.seen:
            return self._skip(f"Duplicate event {event_id}")
        if event_id:
            self.seen.add(event_id)

        etype = event.get("eventType", "")
        res = event.get("resource") or {}
        cfg = self.ctx.config

        if etype in ("git.pullrequest.created", "git.pullrequest.updated"):
            if res.get("status") != "active":
                return self._skip(f"PR {res.get('pullRequestId')} is {res.get('status')}")
            target = short_ref(res.get("targetRefName"))
            if target not in cfg.validation.gate_environments:
                return self._skip(f"No validation gate for target branch '{target}'")
            return {"command": "validate", "args": {"pr_id": int(res["pullRequestId"])}}

        if etype == "git.push" and cfg.qa_promotion.auto_promote_on_push:
            for update in res.get("refUpdates") or []:
                branch = short_ref(update.get("name"))
                if re.match(cfg.branches.feature_pattern, branch) and update.get("newObjectId", "").strip(
                    "0"
                ):
                    return {"command": "promote-qa", "args": {"source_branch": branch}}
            return self._skip("Push did not touch a promotable branch")

        return self._skip(f"No route for event type '{etype}'")

    def graphs(self) -> dict[str, Any]:
        """Every compiled graph in the system (for `agentic-devops graph`)."""
        return {
            "supervisor": self.graph,
            "data-validation": self.validation.graph,
            "qa-promotion": self.qa.promote_graph,
            "qa-certification": self.qa.certify_graph,
            "release-preparation": self.release.prepare_graph,
            "release-finalization": self.release.finalize_graph,
        }
