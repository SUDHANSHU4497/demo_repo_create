"""LangChain advisor: the language tasks of the agents, on whichever Foundry model each task is routed to.

Advisory only (ADR 0001): outputs are text for humans. Any failure - model error, refusal, timeout,
invalid structured output after all fallbacks - returns None and the agent uses its template.
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel

from agentic_devops.config import LlmConfig
from agentic_devops.llm.base import Advisor, NullAdvisor, PrText, ReleaseAssessment, RemediationContext
from agentic_devops.llm.prompts import (
    PR_TASK,
    RELEASE_TASK,
    REMEDIATION_INVESTIGATE,
    REMEDIATION_TASK,
    code,
    prose,
    task_messages,
)
from agentic_devops.llm.registry import ModelRegistry, tracing_callbacks
from agentic_devops.llm.remediation_agent import (
    build_investigation_graph,
    build_remediation_tools,
    investigation_notes,
)
from agentic_devops.llm.schemas import PullRequestDraft, ReleaseRisk, RemediationPlan
from agentic_devops.models import Severity, ValidationReport

log = logging.getLogger(__name__)


class LangChainAdvisor:
    def __init__(self, registry: ModelRegistry, config: LlmConfig, callbacks: list[Any] | None = None):
        self.registry = registry
        self.config = config
        self.callbacks = callbacks or []

    def _run_config(self, task: str, **extra: Any) -> dict[str, Any]:
        return {
            "run_name": f"agentic-devops.{task}",
            "tags": ["agentic-devops", task],
            "metadata": {"task": task, **self.registry.describe().get(task, {})},
            "callbacks": self.callbacks,
            **extra,
        }

    def _structured(self, task: str, schema: type[BaseModel], messages: list) -> BaseModel | None:
        runnable = self.registry.structured(task, schema)
        if runnable is None:
            return None
        try:
            out = runnable.invoke(messages, config=self._run_config(task))
        except Exception as exc:  # noqa: BLE001 - advisory: degrade to template
            log.warning("LLM task %s failed on all models: %s: %s", task, type(exc).__name__, str(exc)[:200])
            return None
        if isinstance(out, dict):
            out = schema.model_validate(out)
        return out if isinstance(out, schema) else None

    # ------------------------------------------------------------------ remediation
    def remediation(self, report: ValidationReport, context: RemediationContext | None = None) -> str | None:
        task = self.registry.task("remediation")
        findings = [f for f in report.all_findings if f.severity in (Severity.ERROR, Severity.WARNING)]
        if task is None or not findings:
            return None
        payload = {
            "environment": report.environment,
            "findings": [self._finding_payload(f.to_dict()) for f in findings[:60]],
            "omitted_findings": max(0, len(findings) - 60),
        }
        notes = (
            self._investigate(report, context, payload, task.max_tool_rounds)
            if task.use_tools and context
            else None
        )
        plan = self._structured(
            "remediation", RemediationPlan, task_messages(REMEDIATION_TASK, payload, notes)
        )
        if plan is None:
            return None
        lines = [prose(plan.summary, 600), ""]
        for i, a in enumerate(plan.actions[:5], 1):
            lines.append(f"{i}. **{prose(a.title, 150)}** — {prose(a.detail, 800)}")
            if a.language in ("python", "sql") and a.code.strip():
                snippet = [f"   {ln}" for ln in code(a.code).splitlines()]
                lines += ["", f"   ```{a.language}", *snippet, "   ```"]
        return "\n".join(lines).strip() or None

    def _investigate(self, report, context, payload, max_rounds) -> str | None:
        tools = build_remediation_tools(
            report, context, self.config.share_sample_values, self.config.share_source_code
        )
        model = self.registry.with_tools("remediation", tools)
        if model is None:
            return None
        graph = build_investigation_graph(model, tools, max_rounds)
        try:
            state = graph.invoke(
                {"messages": task_messages(REMEDIATION_INVESTIGATE, payload), "rounds": 0},
                config=self._run_config("remediation", recursion_limit=2 * max_rounds + 6),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("Remediation investigation failed: %s", type(exc).__name__)
            return None
        return investigation_notes(state["messages"][2:]) or None

    def _finding_payload(self, f: dict[str, Any]) -> dict[str, Any]:
        details = dict(f.get("details") or {})
        if not self.config.share_sample_values:
            details.pop("sample_duplicates", None)
        return {k: v for k, v in {**f, "details": details}.items() if v not in (None, {}, [])}

    # ------------------------------------------------------------------ PR text
    def pr_text(self, context: dict[str, Any]) -> PrText | None:
        draft = self._structured("pr_description", PullRequestDraft, task_messages(PR_TASK, context))
        if draft is None or not draft.title.strip():
            return None
        body = [
            prose(draft.summary, 1200),
            "",
            "### Changes",
            *[f"- {prose(c, 300)}" for c in draft.changes[:8]],
        ]
        if draft.review_focus:
            body += ["", "### Review focus", *[f"- {prose(c, 300)}" for c in draft.review_focus[:5]]]
        return PrText(title=prose(draft.title, 120).replace("\n", " "), description="\n".join(body))

    # ------------------------------------------------------------------ release
    def release_assessment(self, context: dict[str, Any]) -> ReleaseAssessment | None:
        risk = self._structured("release_assessment", ReleaseRisk, task_messages(RELEASE_TASK, context))
        if risk is None:
            return None
        notes: list[str] = []
        for items, title in (
            (risk.highlights, "Highlights"),
            (risk.data_changes, "Data changes"),
            (risk.rollback_considerations, "Rollback considerations"),
        ):
            if items:
                notes += [f"**{title}**", *[f"- {prose(x, 300)}" for x in items[:8]], ""]
        return ReleaseAssessment(
            risk_level=risk.risk_level,
            risk_reasons=[prose(r, 300) for r in risk.risk_reasons[:6]],
            notes_markdown="\n".join(notes).strip(),
        )


def build_advisor(config: LlmConfig) -> Advisor:
    if not config.enabled or not config.tasks:
        return NullAdvisor()
    try:
        return LangChainAdvisor(ModelRegistry(config), config, tracing_callbacks(config))
    except Exception:  # noqa: BLE001 - missing packages/credentials must not break the pipeline
        log.exception("Could not initialise the LLM advisor; continuing without it")
        return NullAdvisor()
