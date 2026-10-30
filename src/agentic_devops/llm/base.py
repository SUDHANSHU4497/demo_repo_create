"""Advisor interface used by the agents. No LangChain imports here, so tests and the deterministic
parts of the system work without any model configured."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentic_devops.models import ValidationReport
from agentic_devops.validation.contracts import DataContract
from agentic_devops.validation.naming import NamingStandards


@dataclass
class PrText:
    title: str
    description: str


@dataclass
class ReleaseAssessment:
    risk_level: str
    risk_reasons: list[str]
    notes_markdown: str


@dataclass
class RemediationContext:
    """What the remediation sub-agent may look at (all read-only)."""

    contracts: list[DataContract] = field(default_factory=list)
    naming: NamingStandards | None = None
    changed_files: list[str] = field(default_factory=list)
    read_file: Callable[[str], str] | None = None  # reads a changed file at the PR's source commit


class Advisor(Protocol):
    def remediation(
        self, report: ValidationReport, context: RemediationContext | None = None
    ) -> str | None: ...

    def pr_text(self, context: dict[str, Any]) -> PrText | None: ...

    def release_assessment(self, context: dict[str, Any]) -> ReleaseAssessment | None: ...


class NullAdvisor:
    """Used when the LLM is disabled: every agent has a deterministic template fallback."""

    def remediation(self, report: ValidationReport, context: RemediationContext | None = None) -> str | None:
        return None

    def pr_text(self, context: dict[str, Any]) -> PrText | None:
        return None

    def release_assessment(self, context: dict[str, Any]) -> ReleaseAssessment | None:
        return None
