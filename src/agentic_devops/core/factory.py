"""Wires real clients into an AgentContext and builds the agents."""

from __future__ import annotations

from dataclasses import dataclass

from agentic_devops.agents.data_validation_agent import DataValidationAgent
from agentic_devops.agents.qa_promotion_agent import QaPromotionAgent
from agentic_devops.agents.release_agent import ReleaseAgent
from agentic_devops.config import AppConfig
from agentic_devops.core.agent import AgentContext
from agentic_devops.core.audit import (
    AuditSink,
    CompositeAuditSink,
    DatabricksSqlAuditSink,
    JsonlAuditSink,
    MemoryAuditSink,
)
from agentic_devops.core.orchestrator import Orchestrator
from agentic_devops.core.runtime import pipeline_run_url
from agentic_devops.integrations.auth import ado_auth_from_env, databricks_auth_from_env
from agentic_devops.integrations.azure_devops import AzureDevOpsClient
from agentic_devops.integrations.databricks import DatabricksClient
from agentic_devops.integrations.http import HttpClient
from agentic_devops.integrations.notifications import NullNotifier, TeamsNotifier
from agentic_devops.llm.advisor import build_advisor


@dataclass
class Agents:
    validation: DataValidationAgent
    qa: QaPromotionAgent
    release: ReleaseAgent
    orchestrator: Orchestrator


def build_context(config: AppConfig, dry_run: bool = False, backend_factory=None) -> AgentContext:
    a = config.azure_devops
    ado = AzureDevOpsClient(
        a.organization_url, a.project, a.repository, HttpClient(ado_auth_from_env()), a.api_version
    )

    clients: dict[str, DatabricksClient] = {}

    def databricks_factory(env_name: str) -> DatabricksClient:
        if env_name not in clients:
            env = config.environment(env_name)
            if not env.workspace_url:
                raise RuntimeError(f"environments.{env_name}.workspace_url is not set")
            clients[env_name] = DatabricksClient(
                env.workspace_url, HttpClient(databricks_auth_from_env(env_name))
            )
        return clients[env_name]

    sinks: list[AuditSink] = []
    if config.audit.sink != "none":
        sinks.append(JsonlAuditSink(config.audit.jsonl_path))
    if config.audit.sink == "databricks_sql" and config.audit.warehouse_id:
        sinks.append(
            DatabricksSqlAuditSink(
                databricks_factory(config.audit.environment), config.audit.warehouse_id, config.audit.table
            )
        )
    audit = CompositeAuditSink(sinks) if sinks else MemoryAuditSink()

    url = config.notifications.teams_webhook_url
    return AgentContext(
        config=config,
        ado=ado,
        advisor=build_advisor(config.llm),
        audit=audit,
        notifier=TeamsNotifier(url) if url else NullNotifier(),
        databricks_factory=databricks_factory,
        backend_factory=backend_factory,
        dry_run=dry_run,
        pipeline_run_url=pipeline_run_url(),
    )


def build_agents(ctx: AgentContext) -> Agents:
    validation = DataValidationAgent(ctx)
    qa = QaPromotionAgent(ctx, validation)
    release = ReleaseAgent(ctx, validation)
    return Agents(validation, qa, release, Orchestrator(ctx, validation, qa, release))
