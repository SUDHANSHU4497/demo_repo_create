"""Agent base class and the shared context (clients, advisor, audit, clock).

Every agent is a *reconciler*: it observes the current state in Azure DevOps (open PRs, statuses,
tags, branches), compares it with the goal, and performs only the missing steps. Running an
agent twice therefore never duplicates a PR, comment, tag or branch, and a pipeline retry is safe.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_devops.config import AppConfig
from agentic_devops.core.audit import AuditEvent, AuditSink
from agentic_devops.integrations.notifications import Notifier
from agentic_devops.llm.base import Advisor
from agentic_devops.models import AgentResult, AgentStatus

log = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class AgentContext:
    config: AppConfig
    ado: Any  # AzureDevOpsClient (or a test double with the same methods)
    advisor: Advisor
    audit: AuditSink
    notifier: Notifier
    databricks_factory: Callable[[str], Any] | None = None  # environment name -> DatabricksClient
    backend_factory: Callable[[str], Any] | None = None  # environment name -> TableBackend (local mode)
    clock: Callable[[], datetime] = _utcnow
    sleep: Callable[[float], None] = time.sleep
    dry_run: bool = False
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    artifacts_dir: Path = field(
        default_factory=lambda: (
            Path(os.environ.get("BUILD_ARTIFACTSTAGINGDIRECTORY", "agentic-artifacts")) / "agentic"
        )
    )
    pipeline_run_url: str | None = None


class BaseAgent:
    name = "agent"

    def __init__(self, ctx: AgentContext):
        self.ctx = ctx
        self.cfg = ctx.config
        self.ado = ctx.ado
        self._planned: list[str] = []

    @property
    def genre(self) -> str:
        return self.cfg.azure_devops.status_genre

    def act(self, description: str, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Perform a write action, or only record it when running with --dry-run."""
        if self.ctx.dry_run:
            log.info("[dry-run] %s", description)
            self._planned.append(description)
            return None
        log.info("%s", description)
        self._planned.append(description)
        return fn(*args, **kwargs)

    def write_artifact(self, name: str, content: str) -> Path | None:
        try:
            self.ctx.artifacts_dir.mkdir(parents=True, exist_ok=True)
            path = self.ctx.artifacts_dir / name
            path.write_text(content, encoding="utf-8")
            return path
        except OSError:
            log.exception("Could not write artifact %s", name)
            return None

    def finish(
        self,
        action: str,
        status: AgentStatus,
        message: str,
        *,
        pr_id: int | None = None,
        commit: str | None = None,
        environment: str | None = None,
        **outputs: Any,
    ) -> AgentResult:
        keys = {"pr_id": pr_id, "commit": commit, "environment": environment}
        outputs = {**{k: v for k, v in keys.items() if v is not None}, **outputs}
        result = AgentResult(self.name, action, status, message, outputs, planned_actions=list(self._planned))
        self._planned.clear()
        log.info("%s/%s -> %s: %s", self.name, action, status.value, message)
        self.ctx.audit.record(
            AuditEvent(
                timestamp=self.ctx.clock().isoformat(timespec="seconds"),
                run_id=self.ctx.run_id,
                agent=self.name,
                action=action,
                status=status.value,
                message=message,
                pr_id=pr_id,
                commit=commit,
                environment=environment,
                dry_run=self.ctx.dry_run,
                details={
                    "outputs": outputs,
                    "actions": result.planned_actions,
                    "pipeline_run": self.ctx.pipeline_run_url,
                },
            )
        )
        return result
