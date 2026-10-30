"""Primary key checks: declared, columns exist, never null, unique."""

from __future__ import annotations

from agentic_devops.models import Finding, Severity
from agentic_devops.validation.rules.base import Rule, TableContext


class PrimaryKeyRule(Rule):
    rule_id = "pk"
    requires_data = True

    def evaluate(self, ctx: TableContext) -> list[Finding]:
        table = ctx.display_name
        pk = ctx.contract.primary_key
        if not pk:
            if ctx.config.require_primary_key:
                return [
                    Finding(
                        "pk.declared",
                        Severity.ERROR,
                        "No primary key declared in the data contract.",
                        table=table,
                        suggestion="Add `primary_key: [<column>, ...]` to the contract "
                        f"({ctx.contract.path or 'contracts/'}).",
                    )
                ]
            return []

        resolved = [ctx.actual_column(k) for k in pk]
        missing = [k for k, r in zip(pk, resolved, strict=True) if r is None]
        if missing:
            return [
                Finding(
                    "pk.columns_exist",
                    Severity.ERROR,
                    f"Primary key column(s) {', '.join(missing)} not found in the table.",
                    table=table,
                    column=",".join(missing),
                    details={"primary_key": pk, "missing": missing},
                    suggestion="Fix the contract's primary_key or restore the column(s) in the pipeline output.",
                )
            ]

        keys = [r for r in resolved if r]
        stats = ctx.key_stats(keys)
        findings: list[Finding] = []
        key_label = ", ".join(keys)
        if stats.null_key_rows:
            per_column = {k: ctx.profile.null_counts.get(k, 0) for k in keys}
            findings.append(
                Finding(
                    "pk.not_null",
                    Severity.ERROR,
                    f"{stats.null_key_rows:,} row(s) have a NULL in primary key ({key_label}).",
                    table=table,
                    column=key_label,
                    details={"null_key_rows": stats.null_key_rows, "null_counts_by_column": per_column},
                    suggestion="Filter or repair rows with missing keys before the write, "
                    f"e.g. `df.dropna(subset={keys!r})` or route them to a quarantine table.",
                )
            )
        if stats.duplicate_key_groups:
            order_hint = _order_hint(ctx)
            findings.append(
                Finding(
                    "pk.unique",
                    Severity.ERROR,
                    f"{stats.duplicate_key_groups:,} primary key value(s) are duplicated across "
                    f"{stats.duplicate_rows:,} row(s) ({key_label}).",
                    table=table,
                    column=key_label,
                    details={
                        "duplicate_key_groups": stats.duplicate_key_groups,
                        "duplicate_rows": stats.duplicate_rows,
                        "distinct_keys": stats.distinct_keys,
                        "row_count": stats.row_count,
                        "sample_duplicates": stats.sample_duplicates,
                    },
                    suggestion="Deduplicate before writing, e.g. keep the latest row per key: "
                    f"`ROW_NUMBER() OVER (PARTITION BY {key_label} ORDER BY {order_hint} DESC) = 1`, "
                    "or use `MERGE INTO` instead of append.",
                )
            )
        return findings


def _order_hint(ctx: TableContext) -> str:
    for candidate in (
        "updated_at",
        "update_ts",
        "modified_at",
        "load_ts",
        "ingested_at",
        "_commit_timestamp",
    ):
        col = ctx.actual_column(candidate)
        if col:
            return col
    return "<last_updated_column>"
