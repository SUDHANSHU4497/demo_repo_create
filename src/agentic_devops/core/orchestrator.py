"""Event router for the event-driven deployment option (Azure DevOps service hooks).

In the default deployment the pipelines call the agents directly through the CLI. When service
hooks are wired to a webhook (Azure Function / Container App -> Service Bus), each event is passed
to ``Orchestrator.handle_event`` which routes it to the right agent. Agents are idempotent, so a
duplicated or replayed event is harmless; ``seen`` only avoids wasted work within one process.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from agentic_devops.agents.data_validation_agent import DataValidationAgent, ValidationRequest
from agentic_devops.agents.qa_promotion_agent import QaPromotionAgent
from agentic_devops.agents.release_agent import ReleaseAgent
from agentic_devops.core.agent import AgentContext
from agentic_devops.integrations.azure_devops import short_ref
from agentic_devops.models import AgentResult, AgentStatus

log = logging.getLogger(__name__)


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

    def _skip(self, message: str) -> AgentResult:
        return AgentResult(self.name, "route", AgentStatus.SKIPPED, message)

    def handle_event(self, event: dict[str, Any]) -> AgentResult:
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
            return self.validation.run(ValidationRequest(pr_id=int(res["pullRequestId"])))

        if etype == "git.push" and cfg.qa_promotion.auto_promote_on_push:
            for update in res.get("refUpdates") or []:
                branch = short_ref(update.get("name"))
                if re.match(cfg.branches.feature_pattern, branch) and update.get("newObjectId", "").strip(
                    "0"
                ):
                    return self.qa.promote(branch)
            return self._skip("Push did not touch a promotable branch")

        return self._skip(f"No route for event type '{etype}'")
