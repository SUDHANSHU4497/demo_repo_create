"""Structured outputs the models must return (validated by Pydantic, provider-independent)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RemediationAction(BaseModel):
    """One concrete fix for a group of related validation findings."""

    title: str = Field(description="One-line title of the fix")
    detail: str = Field(description="Likely root cause and what to change, 1-3 sentences")
    language: Literal["python", "sql", "none"] = Field(description="Language of the code snippet")
    code: str = Field(default="", description="Minimal PySpark or SQL snippet, empty if none helps")


class RemediationPlan(BaseModel):
    """Remediation advice for data validation findings."""

    summary: str = Field(description="One or two sentences summarising the problems")
    actions: list[RemediationAction] = Field(description="At most 5 fixes, most important first")


class PullRequestDraft(BaseModel):
    """Title and description for a pull request."""

    title: str = Field(description="Conventional Commit subject, e.g. 'feat(sales): add orders mart'")
    summary: str = Field(description="2-4 sentences: what changes and why")
    changes: list[str] = Field(description="Notable changes, at most 8")
    review_focus: list[str] = Field(description="What reviewers should check closely, at most 5")


class ReleaseRisk(BaseModel):
    """Risk opinion and notes for a production release."""

    risk_level: Literal["low", "medium", "high"]
    risk_reasons: list[str] = Field(description="Concrete reasons tied to the input")
    highlights: list[str] = Field(description="User-facing highlights, at most 8")
    data_changes: list[str] = Field(description="Data/schema changes, at most 8")
    rollback_considerations: list[str] = Field(description="E.g. tables needing Delta RESTORE, at most 5")
