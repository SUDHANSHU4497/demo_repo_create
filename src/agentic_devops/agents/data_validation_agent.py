"""Data Validation Agent.

Goal: every PR into a protected branch carries an up-to-date ``agentic-devops/data-validation``
status, backed by checks on the tables the PR affects:

  * primary key declared, present, never NULL, unique
  * no completely-NULL columns (plus high-NULL-ratio and NOT NULL contract checks)
  * consistent column names (snake_case, glossary abbreviations, case/format duplicates,
    same concept spelled differently across tables, type drift) and schema vs. contract

The status is what the branch policy requires, so a failing check blocks the merge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from agentic_devops.core.agent import AgentContext, BaseAgent
from agentic_devops.models import AgentResult, AgentStatus, Severity
from agentic_devops.validation.contracts import DataContract, load_contracts, resolve_impacted_contracts
from agentic_devops.validation.executors import DatabricksJobExecutor, LocalExecutor, ValidationExecutor
from agentic_devops.validation.naming import load_naming_standards
from agentic_devops.validation.report import COMMENT_MARKER, render_markdown

log = logging.getLogger(__name__)

STATUS_NAME = "data-validation"


@dataclass
class ValidationRequest:
    pr_id: int | None = None
    environment: str | None = (
        None  # default: derived from the PR target branch (validation.gate_environments)
    )
    tables: list[str] | None = None  # explicit contract tables ("sales.orders")
    validate_all: bool = False
    profile: str = "full"  # full | schema_only
    commit: str | None = None
    post_to_pr: bool = True


class DataValidationAgent(BaseAgent):
    name = "data-validation-agent"

    def __init__(self, ctx: AgentContext, executor: ValidationExecutor | None = None):
        super().__init__(ctx)
        self.naming = load_naming_standards(self.cfg.validation.naming_standards_path)
        self.executor = executor or self._default_executor()

    def _default_executor(self) -> ValidationExecutor:
        v = self.cfg.validation
        if v.execution_mode == "local":
            if not self.ctx.backend_factory:
                raise RuntimeError("execution_mode=local needs a backend_factory (e.g. SparkBackend)")
            return LocalExecutor(self.ctx.backend_factory, v, self.naming)
        if not self.ctx.databricks_factory:
            raise RuntimeError("execution_mode=databricks_job needs a databricks_factory")
        return DatabricksJobExecutor(self.ctx.databricks_factory, v, self.naming)

    # ------------------------------------------------------------------ main entry
    def run(self, req: ValidationRequest) -> AgentResult:
        v = self.cfg.validation
        pr = self.ado.get_pr(req.pr_id) if req.pr_id else None
        if pr and pr.status != "active":
            return self.finish("validate", AgentStatus.SKIPPED, f"PR !{pr.id} is {pr.status}", pr_id=pr.id)

        env_name = req.environment or (v.gate_environments.get(pr.target_branch) if pr else None)
        if not env_name:
            if pr:
                return self.finish(
                    "validate",
                    AgentStatus.SKIPPED,
                    f"Target branch '{pr.target_branch}' has no data validation gate",
                    pr_id=pr.id,
                )
            return self.finish("validate", AgentStatus.FAILED, "An environment or a PR id is required")
        env = self.cfg.environment(env_name)
        commit = req.commit or (pr.last_merge_source_commit if pr else None)
        iteration = self.ado.latest_iteration_id(pr.id) if pr and req.post_to_pr else None

        all_contracts = load_contracts(v.contracts_dir)
        selected, scope = self._select(req, pr, all_contracts)
        if not selected:
            msg = f"No governed tables affected ({scope})."
            if pr and req.post_to_pr:
                self._status(pr.id, "succeeded", msg, iteration)
            return self.finish(
                "validate",
                AgentStatus.SKIPPED,
                msg,
                pr_id=pr.id if pr else None,
                commit=commit,
                environment=env_name,
                tables=[],
            )

        tables = [c.table for c in selected]
        if pr and req.post_to_pr:
            self._status(pr.id, "pending", f"Validating {len(tables)} table(s) in {env_name}…", iteration)

        try:
            report, job_url = self.executor.execute(
                selected,
                all_contracts,
                env,
                self.ctx.run_id,
                req.profile,
                {"pr_id": pr.id if pr else None, "commit": commit},
            )
        except Exception as exc:  # noqa: BLE001 - fail closed: "could not validate" never means "passed"
            log.exception("Validation could not run")
            msg = f"Validation could not run: {type(exc).__name__}: {exc}"
            if pr and req.post_to_pr:
                self._status(pr.id, "error", msg, iteration)
                self.act(
                    f"Comment on PR !{pr.id}: validation error",
                    self.ado.upsert_pr_comment,
                    pr.id,
                    COMMENT_MARKER,
                    f"{COMMENT_MARKER}\n## 🤖 Data Validation Agent — ⛔ ERROR\n{msg}\n\n"
                    "The gate stays closed until validation runs successfully. Re-queue the "
                    "`pr-validation` build once the cause is fixed.",
                    True,
                )
            return self.finish(
                "validate",
                AgentStatus.FAILED,
                msg,
                pr_id=pr.id if pr else None,
                commit=commit,
                environment=env_name,
                tables=tables,
            )

        advice = None
        if any(f.severity in (Severity.ERROR, Severity.WARNING) for f in report.all_findings):
            advice = self.ctx.advisor.remediation(report)
        markdown = render_markdown(
            report,
            advisor_markdown=advice,
            commit=commit,
            run_url=self.ctx.pipeline_run_url or job_url,
            max_findings=v.max_findings_in_comment,
        )
        self.write_artifact(f"validation-report-{env_name}.json", report.to_json())
        self.write_artifact(f"validation-report-{env_name}.md", markdown)

        summary = report.summary()
        desc = f"{report.error_count} error(s), {report.warning_count} warning(s) in {len(tables)} table(s) [{env_name}]"
        if pr and req.post_to_pr:
            self.act(
                f"Upsert validation comment on PR !{pr.id}",
                self.ado.upsert_pr_comment,
                pr.id,
                COMMENT_MARKER,
                markdown,
                not report.passed,
            )
            self._status(pr.id, "succeeded" if report.passed else "failed", desc, iteration)

        status = AgentStatus.SUCCEEDED if report.passed else AgentStatus.FAILED
        return self.finish(
            "validate",
            status,
            ("Passed: " if report.passed else "Failed: ") + desc,
            pr_id=pr.id if pr else None,
            commit=commit,
            environment=env_name,
            tables=tables,
            scope=scope,
            summary=summary,
            databricks_run_url=job_url,
        )

    # ------------------------------------------------------------------ helpers
    def _select(
        self, req: ValidationRequest, pr, contracts: list[DataContract]
    ) -> tuple[list[DataContract], str]:
        if req.tables:
            wanted = {t.lower() for t in req.tables}
            return [c for c in contracts if c.table.lower() in wanted], "explicit table list"
        if req.validate_all or pr is None:
            return list(contracts), "all contracts"
        changed = self.ado.get_pr_changed_files(pr.id)
        impacted = resolve_impacted_contracts(contracts, changed, self.cfg.validation.global_impact_paths)
        return impacted, f"{len(changed)} changed file(s) in PR !{pr.id}"

    def _status(self, pr_id: int, state: str, description: str, iteration: int | None) -> None:
        self.act(
            f"Set PR !{pr_id} status {self.genre}/{STATUS_NAME}={state}",
            self.ado.post_pr_status,
            pr_id,
            state,
            STATUS_NAME,
            self.genre,
            description,
            self.ctx.pipeline_run_url,
            iteration,
        )
