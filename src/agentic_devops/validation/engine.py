"""Runs validation rules over a set of contracts and produces a ValidationReport.

The engine fails closed: if a rule crashes, that becomes an ERROR finding rather than a silent pass.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from agentic_devops.config import ValidationConfig
from agentic_devops.models import Finding, Severity, TableResult, ValidationReport
from agentic_devops.validation.backends import TableBackend
from agentic_devops.validation.contracts import DataContract
from agentic_devops.validation.naming import NamingStandards
from agentic_devops.validation.rules import (
    CrossTableRule,
    Rule,
    TableContext,
    default_cross_table_rules,
    default_rules,
)

log = logging.getLogger(__name__)

PROFILES = ("full", "schema_only")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ValidationEngine:
    def __init__(
        self,
        backend: TableBackend,
        config: ValidationConfig,
        naming: NamingStandards,
        rules: list[Rule] | None = None,
        cross_table_rules: list[CrossTableRule] | None = None,
    ):
        self.backend = backend
        self.config = config
        self.naming = naming
        self.rules = rules if rules is not None else default_rules()
        self.cross_table_rules = (
            cross_table_rules if cross_table_rules is not None else default_cross_table_rules()
        )

    def validate(
        self,
        contracts: list[DataContract],
        catalog: str,
        environment: str,
        run_id: str | None = None,
        reference_contracts: list[DataContract] | None = None,
        profile: str = "full",
    ) -> ValidationReport:
        if profile not in PROFILES:
            raise ValueError(f"profile must be one of {PROFILES}")
        report = ValidationReport(
            run_id=run_id or uuid.uuid4().hex[:12],
            environment=environment,
            catalog=catalog,
            started_at=_now(),
            fail_on_warnings=self.config.fail_on_warnings,
            profile=profile,
        )
        rules = [r for r in self.rules if profile == "full" or not r.requires_data]
        contexts: list[TableContext] = []

        for contract in contracts:
            fq = contract.fq_name(catalog)
            log.info("Validating %s (%s)", fq, profile)
            try:
                exists = self.backend.table_exists(fq)
            except Exception as exc:  # noqa: BLE001 - fail closed on any backend error
                report.tables.append(
                    TableResult(contract.table, None, [_crash("table.lookup", contract, exc)])
                )
                continue
            if not exists:
                report.tables.append(
                    TableResult(
                        contract.table,
                        None,
                        [
                            Finding(
                                "schema.table_not_found",
                                Severity.ERROR,
                                f"Table `{fq}` does not exist (or the validation identity cannot read it).",
                                table=contract.table,
                                suggestion="Run the pipeline in this environment first, or grant SELECT to "
                                "the validation service principal.",
                            )
                        ],
                    )
                )
                continue

            ctx = TableContext(
                contract=contract, fq_name=fq, backend=self.backend, config=self.config, naming=self.naming
            )
            findings: list[Finding] = []
            for rule in rules:
                try:
                    findings.extend(rule.evaluate(ctx))
                except Exception as exc:  # noqa: BLE001
                    log.exception("Rule %s failed on %s", rule.rule_id, fq)
                    findings.append(_crash(rule.rule_id, contract, exc))
            row_count = ctx.profile.row_count if "profile" in ctx.__dict__ else None
            report.tables.append(TableResult(contract.table, row_count, self._apply_overrides(findings)))
            contexts.append(ctx)

        for xrule in self.cross_table_rules:
            try:
                report.cross_table_findings.extend(
                    self._apply_overrides(
                        xrule.evaluate(contexts, reference_contracts or contracts, self.naming)
                    )
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("Cross-table rule %s failed", xrule.rule_id)
                report.cross_table_findings.append(
                    Finding(f"{xrule.rule_id}.execution_error", Severity.ERROR, f"Rule crashed: {exc!r}")
                )

        report.finished_at = _now()
        return report

    def _apply_overrides(self, findings: list[Finding]) -> list[Finding]:
        overrides = self.config.severity_overrides
        if not overrides:
            return findings
        out = []
        for f in findings:
            level = overrides.get(f.rule_id) or overrides.get(f.rule_id.split(".")[0] + ".*")
            if level == "off":
                continue
            out.append(f.with_severity(Severity(level)) if level else f)
        return out


def _crash(rule_id: str, contract: DataContract, exc: Exception) -> Finding:
    return Finding(
        f"{rule_id}.execution_error",
        Severity.ERROR,
        f"Validation could not complete: {type(exc).__name__}: {exc}",
        table=contract.table,
        suggestion="Check that the table is readable by the validation identity and the contract filter is valid.",
    )
