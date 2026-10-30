"""Where validation runs.

* DatabricksJobExecutor (default): the agent runs on an Azure DevOps agent; the scan runs on
  Databricks compute in the target workspace, next to the data. Nothing but the report leaves it.
* LocalExecutor: runs the engine in-process (inside a Databricks notebook, or tests with pandas).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Protocol

from agentic_devops.config import EnvironmentConfig, ValidationConfig
from agentic_devops.models import ValidationReport
from agentic_devops.validation.backends import TableBackend
from agentic_devops.validation.contracts import DataContract
from agentic_devops.validation.engine import ValidationEngine
from agentic_devops.validation.naming import NamingStandards

log = logging.getLogger(__name__)


class ValidationExecutor(Protocol):
    def execute(
        self,
        contracts: list[DataContract],
        reference_contracts: list[DataContract],
        environment: EnvironmentConfig,
        run_id: str,
        profile: str,
        context: dict[str, Any],
    ) -> tuple[ValidationReport, str | None]: ...


class LocalExecutor:
    def __init__(
        self,
        backend_factory: Callable[[str], TableBackend],
        config: ValidationConfig,
        naming: NamingStandards,
    ):
        self.backend_factory = backend_factory
        self.config = config
        self.naming = naming

    def execute(self, contracts, reference_contracts, environment, run_id, profile, context):
        engine = ValidationEngine(self.backend_factory(environment.name), self.config, self.naming)
        report = engine.validate(
            contracts, environment.catalog, environment.name, run_id, reference_contracts, profile=profile
        )
        return report, None


class DatabricksJobExecutor:
    def __init__(
        self, databricks_factory: Callable[[str], Any], config: ValidationConfig, naming: NamingStandards
    ):
        self.databricks_factory = databricks_factory
        self.config = config
        self.naming = naming

    def execute(self, contracts, reference_contracts, environment, run_id, profile, context):
        if not environment.validation_job_id or not environment.workspace_url:
            raise RuntimeError(
                f"environments.{environment.name}.workspace_url and validation_job_id must be set "
                "for execution_mode=databricks_job"
            )
        client = self.databricks_factory(environment.name)
        base = f"{environment.volume_path}/runs/{run_id}"
        input_path, output_path = f"{base}/input.json", f"{base}/output.json"
        payload = {
            "run_id": run_id,
            "environment": environment.name,
            "catalog": environment.catalog,
            "profile": profile,
            "contracts": [c.to_dict() for c in contracts],
            "reference_contracts": [c.to_dict() for c in reference_contracts],
            "validation_config": self.config.to_dict(),
            "naming_standards": self.naming.to_dict(),
            "results_table": environment.results_table,
            "context": context,
        }
        client.upload_file(input_path, json.dumps(payload, default=str).encode("utf-8"))
        db_run_id = client.run_job(
            environment.validation_job_id,
            {"input_path": input_path, "output_path": output_path},
            idempotency_token=f"agentic-{run_id}-{profile}",
        )
        log.info("Started Databricks run %s in %s", db_run_id, environment.workspace_url)
        run = client.wait_for_run(
            db_run_id, timeout_s=self.config.timeout_minutes * 60, poll_s=self.config.poll_interval_seconds
        )
        report = ValidationReport.from_dict(json.loads(client.download_file(output_path)))
        return report, run.get("run_page_url")
