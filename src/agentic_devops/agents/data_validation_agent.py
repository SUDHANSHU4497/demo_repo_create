"""Data Validation Agent (LangGraph flow).

Goal: every PR into a protected branch carries an up-to-date ``agentic-devops/data-validation``
status, backed by checks on the tables the PR affects:

  * primary key declared, present, never NULL, unique
  * no completely-NULL columns (plus high-NULL-ratio and NOT NULL contract checks)
  * consistent column names (snake_case, glossary abbreviations, case/format duplicates,
    same concept spelled differently across tables, type drift) and schema vs. contract

    load_pr -> resolve_environment -> select_tables -> mark_pending -> run_checks
            -> advise (LLM, optional) -> publish

The status is what the branch policy requires, so a failing check blocks the merge.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, TypedDict

from agentic_devops.config import EnvironmentConfig
from agentic_devops.core.agent import AgentContext, BaseAgent
from agentic_devops.core.flow import Step, build_flow, run_flow
from agentic_devops.llm.base import RemediationContext
from agentic_devops.models import AgentResult, AgentStatus, Severity, ValidationReport
from agentic_devops.validation.contracts import DataContract, load_contracts, resolve_impacted_contracts
from agentic_devops.validation.executors import DatabricksJobExecutor, LocalExecutor, ValidationExecutor
from agentic_devops.validation.naming import load_naming_standards
from agentic_devops.validation.report import COMMENT_MARKER, render_markdown

log = logging.getLogger(__name__)

STATUS_NAME = "data-validation"


@dataclass
class ValidationRequest:
    pr_id: int | None = None
    environment: str | None = None  # default: from the PR target branch (validation.gate_environments)
    tables: list[str] | None = None  # explicit contract tables ("sales.orders")
    validate_all: bool = False
    profile: str = "full"  # full | schema_only
    commit: str | None = None
    post_to_pr: bool = True


class ValidationState(TypedDict, total=False):
    req: ValidationRequest
    pr: Any
    post: bool
    env: EnvironmentConfig
    commit: str | None
    iteration: int | None
    contracts: list[DataContract]
    selected: list[DataContract]
    changed_files: list[str]
    scope: str
    report: ValidationReport
    job_url: str | None
    advice: str | None
    result: AgentResult


class DataValidationAgent(BaseAgent):
    name = "data-validation-agent"

    def __init__(self, ctx: AgentContext, executor: ValidationExecutor | None = None):
        super().__init__(ctx)
        self.naming = load_naming_standards(self.cfg.validation.naming_standards_path)
        self.executor = executor or self._default_executor()
        self.graph = build_flow(
            ValidationState,
            [
                Step("load_pr", self._load_pr),
                Step("resolve_environment", self._resolve_environment),
                Step("select_tables", self._select_tables),
                Step("mark_pending", self._mark_pending),
                Step("run_checks", self._run_checks),
                Step("advise", self._advise),
                Step("publish", self._publish),
            ],
            name="data-validation",
        )

    def _default_executor(self) -> ValidationExecutor:
        v = self.cfg.validation
        if v.execution_mode == "local":
            if not self.ctx.backend_factory:
                raise RuntimeError("execution_mode=local needs a backend_factory (e.g. SparkBackend)")
            return LocalExecutor(self.ctx.backend_factory, v, self.naming)
        if not self.ctx.databricks_factory:
            raise RuntimeError("execution_mode=databricks_job needs a databricks_factory")
        return DatabricksJobExecutor(self.ctx.databricks_factory, v, self.naming)

    def run(self, req: ValidationRequest) -> AgentResult:
        return run_flow(self.graph, {"req": req})

    # ------------------------------------------------------------------ nodes
    def _done(self, state: ValidationState, status: AgentStatus, message: str, **outputs: Any) -> dict:
        pr = state.get("pr")
        env = state.get("env")
        return {
            "result": self.finish(
                "validate",
                status,
                message,
                pr_id=pr.id if pr else None,
                commit=state.get("commit"),
                environment=env.name if env else None,
                **outputs,
            )
        }

    def _load_pr(self, state: ValidationState) -> dict:
        req = state["req"]
        pr = self.ado.get_pr(req.pr_id) if req.pr_id else None
        if pr and pr.status != "active":
            return {"pr": pr, **self._done({"pr": pr}, AgentStatus.SKIPPED, f"PR !{pr.id} is {pr.status}")}
        return {"pr": pr, "post": bool(pr and req.post_to_pr)}

    def _resolve_environment(self, state: ValidationState) -> dict:
        req, pr = state["req"], state.get("pr")
        env_name = req.environment or (
            self.cfg.validation.gate_environments.get(pr.target_branch) if pr else None
        )
        if not env_name:
            if pr:
                return self._done(
                    state,
                    AgentStatus.SKIPPED,
                    f"Target branch '{pr.target_branch}' has no data validation gate",
                )
            return self._done(state, AgentStatus.FAILED, "An environment or a PR id is required")
        return {
            "env": self.cfg.environment(env_name),
            "commit": req.commit or (pr.last_merge_source_commit if pr else None),
            "iteration": self.ado.latest_iteration_id(pr.id) if state.get("post") else None,
        }

    def _select_tables(self, state: ValidationState) -> dict:
        req, pr = state["req"], state.get("pr")
        contracts = load_contracts(self.cfg.validation.contracts_dir)
        changed: list[str] = []
        if req.tables:
            wanted = {t.lower() for t in req.tables}
            selected, scope = [c for c in contracts if c.table.lower() in wanted], "explicit table list"
        elif req.validate_all or pr is None:
            selected, scope = list(contracts), "all contracts"
        else:
            changed = self.ado.get_pr_changed_files(pr.id)
            selected = resolve_impacted_contracts(contracts, changed, self.cfg.validation.global_impact_paths)
            scope = f"{len(changed)} changed file(s) in PR !{pr.id}"
        update = {"contracts": contracts, "selected": selected, "scope": scope, "changed_files": changed}
        if not selected:
            msg = f"No governed tables affected ({scope})."
            if state.get("post"):
                self._status(pr.id, "succeeded", msg, state.get("iteration"))
            return {**update, **self._done(state, AgentStatus.SKIPPED, msg, tables=[])}
        return update

    def _mark_pending(self, state: ValidationState) -> dict:
        if state.get("post"):
            n, env = len(state["selected"]), state["env"].name
            self._status(
                state["pr"].id, "pending", f"Validating {n} table(s) in {env}…", state.get("iteration")
            )
        return {}

    def _run_checks(self, state: ValidationState) -> dict:
        pr, tables = state.get("pr"), [c.table for c in state["selected"]]
        try:
            report, job_url = self.executor.execute(
                state["selected"],
                state["contracts"],
                state["env"],
                self.ctx.run_id,
                state["req"].profile,
                {"pr_id": pr.id if pr else None, "commit": state.get("commit")},
            )
        except Exception as exc:  # noqa: BLE001 - fail closed: "could not validate" never means "passed"
            log.exception("Validation could not run")
            msg = f"Validation could not run: {type(exc).__name__}: {exc}"
            if state.get("post"):
                self._status(pr.id, "error", msg, state.get("iteration"))
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
            return self._done(state, AgentStatus.FAILED, msg, tables=tables)
        return {"report": report, "job_url": job_url}

    def _advise(self, state: ValidationState) -> dict:
        report = state["report"]
        if not any(f.severity in (Severity.ERROR, Severity.WARNING) for f in report.all_findings):
            return {"advice": None}
        pr, commit, changed = state.get("pr"), state.get("commit"), state.get("changed_files") or []
        read_file = None
        if pr and commit and changed and hasattr(self.ado, "get_file_content"):

            def read_file(path: str) -> str:
                return self.ado.get_file_content(path, commit)

        context = RemediationContext(
            contracts=state["contracts"], naming=self.naming, changed_files=changed, read_file=read_file
        )
        return {"advice": self.ctx.advisor.remediation(report, context)}

    def _publish(self, state: ValidationState) -> dict:
        report, pr, env = state["report"], state.get("pr"), state["env"]
        tables = [c.table for c in state["selected"]]
        markdown = render_markdown(
            report,
            advisor_markdown=state.get("advice"),
            commit=state.get("commit"),
            run_url=self.ctx.pipeline_run_url or state.get("job_url"),
            max_findings=self.cfg.validation.max_findings_in_comment,
        )
        self.write_artifact(f"validation-report-{env.name}.json", report.to_json())
        self.write_artifact(f"validation-report-{env.name}.md", markdown)
        desc = (
            f"{report.error_count} error(s), {report.warning_count} warning(s) in "
            f"{len(tables)} table(s) [{env.name}]"
        )
        if state.get("post"):
            self.act(
                f"Upsert validation comment on PR !{pr.id}",
                self.ado.upsert_pr_comment,
                pr.id,
                COMMENT_MARKER,
                markdown,
                not report.passed,
            )
            self._status(pr.id, "succeeded" if report.passed else "failed", desc, state.get("iteration"))
        return self._done(
            state,
            AgentStatus.SUCCEEDED if report.passed else AgentStatus.FAILED,
            ("Passed: " if report.passed else "Failed: ") + desc,
            tables=tables,
            scope=state.get("scope"),
            summary=report.summary(),
            databricks_run_url=state.get("job_url"),
            advisor_used=bool(state.get("advice")),
        )

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
