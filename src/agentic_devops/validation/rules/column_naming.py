"""Column-name consistency checks.

Within a table:
  naming.convention               not snake_case (whitespace, special chars, camelCase, ...)
  naming.max_length               longer than the standard allows
  naming.reserved_word            SQL reserved word (forces back-ticks everywhere)
  naming.case_insensitive_duplicate  ``CustomerId`` and ``customerid`` in the same table
  naming.normalized_duplicate     ``customer_id`` and ``customerid`` in the same table
  naming.non_standard_abbreviation  ``cust_id`` where the glossary says ``customer_id``
  naming.type_convention          e.g. boolean not prefixed ``is_``/``has_``

Across tables (this run + every contract in the repo):
  naming.cross_table_inconsistent   same concept, different spelling (``cust_id`` vs ``customer_id``)
  naming.cross_table_type_mismatch  same column name, different data types
"""

from __future__ import annotations

from collections import defaultdict

from agentic_devops.models import Finding, Severity
from agentic_devops.validation.contracts import DataContract
from agentic_devops.validation.naming import (
    SNAKE_CASE,
    NamingStandards,
    describe_case_problems,
    normalize_type,
    to_snake_case,
    type_family,
)
from agentic_devops.validation.rules.base import CrossTableRule, Rule, TableContext


class ColumnNamingRule(Rule):
    rule_id = "naming"
    requires_data = False

    def evaluate(self, ctx: TableContext) -> list[Finding]:
        table = ctx.display_name
        std = ctx.naming
        findings: list[Finding] = []

        for col in ctx.schema:
            name = col.name
            if std.case_style == "snake_case" and not SNAKE_CASE.match(name):
                suggestion = std.canonical_name(name)
                findings.append(
                    Finding(
                        "naming.convention",
                        Severity.ERROR,
                        f"Column name `{name}` is not snake_case: {'; '.join(describe_case_problems(name))}.",
                        table=table,
                        column=name,
                        details={"suggested_name": suggestion},
                        suggestion=f"Rename to `{suggestion}`.",
                    )
                )
            if len(name) > std.max_length:
                findings.append(
                    Finding(
                        "naming.max_length",
                        Severity.WARNING,
                        f"Column name is {len(name)} characters (max {std.max_length}).",
                        table=table,
                        column=name,
                    )
                )
            if name.lower() in std.reserved_words:
                findings.append(
                    Finding(
                        "naming.reserved_word",
                        Severity.WARNING,
                        f"`{name}` is a SQL reserved word and must be back-ticked in every query.",
                        table=table,
                        column=name,
                        suggestion=f"Rename to something descriptive, e.g. `{name.lower()}_value` or "
                        f"`<entity>_{name.lower()}`.",
                    )
                )
            abbrevs = std.abbreviation_tokens(name)
            if abbrevs and SNAKE_CASE.match(name):
                canonical = std.canonical_name(name)
                findings.append(
                    Finding(
                        "naming.non_standard_abbreviation",
                        Severity.WARNING,
                        f"`{name}` uses non-standard abbreviation(s) {', '.join(abbrevs)}.",
                        table=table,
                        column=name,
                        details={"abbreviations": abbrevs, "suggested_name": canonical},
                        suggestion=f"Use the glossary name `{canonical}`.",
                    )
                )
            family = type_family(col.data_type)
            for conv in std.type_conventions:
                if conv.data_type == family and not conv.matches(name):
                    expected = [p + "*" for p in conv.prefixes] + ["*" + s for s in conv.suffixes]
                    findings.append(
                        Finding(
                            "naming.type_convention",
                            conv.severity,
                            f"{family} column `{name}` should match {' or '.join(expected)}.",
                            table=table,
                            column=name,
                            details={"data_type": col.data_type, "expected_patterns": expected},
                        )
                    )

        findings.extend(_duplicates(table, ctx.column_names))
        return findings


def _duplicates(table: str, names: list[str]) -> list[Finding]:
    findings: list[Finding] = []
    by_lower: dict[str, list[str]] = defaultdict(list)
    for n in names:
        by_lower[n.lower()].append(n)
    for group in by_lower.values():
        if len(group) > 1:
            findings.append(
                Finding(
                    "naming.case_insensitive_duplicate",
                    Severity.ERROR,
                    f"Columns {', '.join(f'`{g}`' for g in group)} differ only by case; Delta/Spark treat "
                    "them as the same column.",
                    table=table,
                    column=",".join(group),
                    suggestion="Keep one column and drop or rename the others.",
                )
            )

    by_compact: dict[str, set[str]] = defaultdict(set)
    for n in names:
        by_compact[to_snake_case(n).replace("_", "")].add(n.lower())
    for group in by_compact.values():
        if len(group) > 1:
            ordered = sorted(group)
            findings.append(
                Finding(
                    "naming.normalized_duplicate",
                    Severity.WARNING,
                    f"Columns {', '.join(f'`{g}`' for g in ordered)} look like the same field spelled "
                    "differently.",
                    table=table,
                    column=",".join(ordered),
                    suggestion="Confirm they are different fields; otherwise consolidate into one column.",
                )
            )
    return findings


class CrossTableNamingRule(CrossTableRule):
    """Compares columns of validated tables against every other table we know about.

    Known tables = the tables validated in this run (physical schema) plus all contracts in the repo
    (declared columns), so a PR touching one table is still checked against the rest of the model.
    """

    rule_id = "naming.cross_table"

    def evaluate(
        self, contexts: list[TableContext], reference_contracts: list[DataContract], naming: NamingStandards
    ) -> list[Finding]:
        # (table, column, data_type, is_physical)
        entries: list[tuple[str, str, str | None, bool]] = []
        validated = set()
        for ctx in contexts:
            validated.add(ctx.display_name.lower())
            for col in ctx.schema:
                entries.append((ctx.display_name, col.name, col.data_type, True))
        for c in reference_contracts:
            if c.table.lower() in validated:
                continue
            for col in c.columns:
                entries.append((c.table, col.name, col.type, False))

        findings: list[Finding] = []

        by_concept: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for table, col, _type, _phys in entries:
            by_concept[naming.canonical_name(col)].append((table, col))
        for concept, uses in sorted(by_concept.items()):
            spellings = sorted({c.lower() for _, c in uses})
            if len(spellings) < 2:
                continue
            for table, col in uses:
                if table.lower() not in validated or col.lower() == concept:
                    continue
                others = sorted({f"{t}.{c}" for t, c in uses if c.lower() != col.lower()})
                findings.append(
                    Finding(
                        "naming.cross_table_inconsistent",
                        Severity.WARNING,
                        f"`{col}` refers to the same concept as {', '.join(f'`{o}`' for o in others)}; "
                        f"use one name (`{concept}`) across tables.",
                        table=table,
                        column=col,
                        details={"canonical_name": concept, "other_usages": others},
                        suggestion=f"Rename to `{concept}`.",
                    )
                )

        by_name: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for table, col, dtype, _phys in entries:
            norm = normalize_type(dtype)
            if norm:
                by_name[col.lower()].append((table, col, norm))
        for _col, uses in sorted(by_name.items()):
            types = {t for _, _, t in uses}
            if len(types) < 2:
                continue
            for table, col, dtype in uses:
                if table.lower() not in validated:
                    continue
                others = sorted({f"{t}.{c} ({ty})" for t, c, ty in uses if ty != dtype})
                if not others:
                    continue
                findings.append(
                    Finding(
                        "naming.cross_table_type_mismatch",
                        Severity.WARNING,
                        f"`{col}` is `{dtype}` here but {', '.join(others)} elsewhere; joins on it will "
                        "need casts.",
                        table=table,
                        column=col,
                        details={"data_type": dtype, "other_usages": others},
                    )
                )
        return findings
