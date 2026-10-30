"""Null checks: completely-null columns, high null ratio, NOT NULL contract violations, empty tables."""

from __future__ import annotations

from agentic_devops.models import Finding, Severity
from agentic_devops.validation.rules.base import Rule, TableContext


class NullColumnsRule(Rule):
    rule_id = "nulls"
    requires_data = True

    def evaluate(self, ctx: TableContext) -> list[Finding]:
        table = ctx.display_name
        profile = ctx.profile
        rows = profile.row_count
        findings: list[Finding] = []

        if rows < ctx.contract.min_rows:
            findings.append(
                Finding(
                    "table.min_rows",
                    Severity.ERROR,
                    f"Table has {rows:,} row(s); the contract requires at least {ctx.contract.min_rows:,}.",
                    table=table,
                    details={"row_count": rows, "min_rows": ctx.contract.min_rows},
                )
            )
        if rows == 0:
            findings.append(
                Finding(
                    "table.empty",
                    Severity.WARNING,
                    "Table is empty"
                    + (" for the configured filter" if ctx.contract.filter else "")
                    + "; null-column checks were skipped.",
                    table=table,
                )
            )
            return findings

        pk_lower = {k.lower() for k in ctx.contract.primary_key}
        global_allow = set(ctx.config.allow_fully_null_columns)
        threshold = ctx.config.null_ratio_warn_threshold
        blank_note = " (blank strings counted as NULL)" if ctx.config.treat_blank_as_null else ""

        for name in ctx.column_names:
            nulls = profile.null_counts.get(name, 0)
            ratio = nulls / rows
            spec = ctx.contract.column(name)
            if nulls == rows:
                allowed = (spec is not None and spec.allow_fully_null) or name.lower() in global_allow
                findings.append(
                    Finding(
                        "nulls.fully_null_column",
                        Severity.INFO if allowed else Severity.ERROR,
                        f"Column is NULL in all {rows:,} row(s){blank_note}"
                        + (" (allowed by contract)." if allowed else "."),
                        table=table,
                        column=name,
                        details={"null_count": nulls, "row_count": rows},
                        suggestion=None
                        if allowed
                        else "Check the source mapping/join for this column. If it is intentionally "
                        "empty for now, set `allow_fully_null: true` on the column in the contract; "
                        "otherwise drop it.",
                    )
                )
                continue
            if spec is not None and not spec.nullable and nulls and name.lower() not in pk_lower:
                findings.append(
                    Finding(
                        "nulls.not_nullable_violation",
                        Severity.ERROR,
                        f"{nulls:,} NULL value(s){blank_note} in a column declared `nullable: false`.",
                        table=table,
                        column=name,
                        details={"null_count": nulls, "row_count": rows, "null_ratio": round(ratio, 6)},
                        suggestion="Add a default/COALESCE in the transformation or relax the contract.",
                    )
                )
            elif ratio >= threshold:
                findings.append(
                    Finding(
                        "nulls.high_null_ratio",
                        Severity.WARNING,
                        f"{ratio:.1%} of values are NULL{blank_note} (threshold {threshold:.0%}).",
                        table=table,
                        column=name,
                        details={"null_count": nulls, "row_count": rows, "null_ratio": round(ratio, 6)},
                    )
                )
        return findings
