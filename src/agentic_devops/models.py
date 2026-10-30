"""Shared data models: validation findings, reports and agent results."""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    ERROR = "error"  # blocks the gate
    WARNING = "warning"  # reported, blocks only when fail_on_warnings is set
    INFO = "info"  # informational

    @property
    def rank(self) -> int:
        return {"error": 3, "warning": 2, "info": 1}[self.value]


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: Severity
    message: str
    table: str | None = None
    column: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    suggestion: str | None = None

    def with_severity(self, severity: Severity) -> Finding:
        return dataclasses.replace(self, severity=severity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity.value,
            "message": self.message,
            "table": self.table,
            "column": self.column,
            "details": self.details,
            "suggestion": self.suggestion,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Finding:
        return cls(
            rule_id=data["rule_id"],
            severity=Severity(data["severity"]),
            message=data["message"],
            table=data.get("table"),
            column=data.get("column"),
            details=data.get("details") or {},
            suggestion=data.get("suggestion"),
        )


@dataclass
class TableResult:
    table: str
    row_count: int | None
    findings: list[Finding] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.findings if f.severity is Severity.WARNING)

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "row_count": self.row_count,
            "findings": [f.to_dict() for f in self.findings],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TableResult:
        return cls(
            table=data["table"],
            row_count=data.get("row_count"),
            findings=[Finding.from_dict(f) for f in data.get("findings", [])],
        )


@dataclass
class ValidationReport:
    run_id: str
    environment: str
    catalog: str
    started_at: str
    finished_at: str | None = None
    tables: list[TableResult] = field(default_factory=list)
    cross_table_findings: list[Finding] = field(default_factory=list)
    fail_on_warnings: bool = False
    profile: str = "full"

    @property
    def all_findings(self) -> list[Finding]:
        out: list[Finding] = []
        for t in self.tables:
            out.extend(t.findings)
        out.extend(self.cross_table_findings)
        return out

    @property
    def error_count(self) -> int:
        return sum(1 for f in self.all_findings if f.severity is Severity.ERROR)

    @property
    def warning_count(self) -> int:
        return sum(1 for f in self.all_findings if f.severity is Severity.WARNING)

    @property
    def passed(self) -> bool:
        if self.error_count:
            return False
        return not (self.fail_on_warnings and self.warning_count)

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "environment": self.environment,
            "catalog": self.catalog,
            "profile": self.profile,
            "passed": self.passed,
            "tables": len(self.tables),
            "errors": self.error_count,
            "warnings": self.warning_count,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "environment": self.environment,
            "catalog": self.catalog,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "fail_on_warnings": self.fail_on_warnings,
            "profile": self.profile,
            "passed": self.passed,
            "tables": [t.to_dict() for t in self.tables],
            "cross_table_findings": [f.to_dict() for f in self.cross_table_findings],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, default=str)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ValidationReport:
        return cls(
            run_id=data["run_id"],
            environment=data["environment"],
            catalog=data["catalog"],
            started_at=data["started_at"],
            finished_at=data.get("finished_at"),
            tables=[TableResult.from_dict(t) for t in data.get("tables", [])],
            cross_table_findings=[Finding.from_dict(f) for f in data.get("cross_table_findings", [])],
            fail_on_warnings=bool(data.get("fail_on_warnings", False)),
            profile=data.get("profile", "full"),
        )


class AgentStatus(str, Enum):
    SUCCEEDED = "succeeded"  # goal reached
    WAITING = "waiting"  # progressing, waiting on humans/policies (not a failure)
    SKIPPED = "skipped"  # nothing to do
    BLOCKED = "blocked"  # a guardrail refused the action (e.g. outside change window)
    FAILED = "failed"  # the action failed or validation failed

    @property
    def exit_code(self) -> int:
        return 0 if self in (AgentStatus.SUCCEEDED, AgentStatus.WAITING, AgentStatus.SKIPPED) else 1


@dataclass
class AgentResult:
    agent: str
    action: str
    status: AgentStatus
    message: str
    outputs: dict[str, Any] = field(default_factory=dict)
    planned_actions: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        return self.status.exit_code

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "action": self.action,
            "status": self.status.value,
            "message": self.message,
            "outputs": self.outputs,
            "planned_actions": self.planned_actions,
        }
