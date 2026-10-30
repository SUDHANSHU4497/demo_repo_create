"""Physical schema vs. data contract: missing, unexpected and re-typed columns."""

from __future__ import annotations

from agentic_devops.models import Finding, Severity
from agentic_devops.validation.naming import normalize_type, to_snake_case
from agentic_devops.validation.rules.base import Rule, TableContext


class SchemaContractRule(Rule):
    rule_id = "schema"
    requires_data = False

    def evaluate(self, ctx: TableContext) -> list[Finding]:
        contract = ctx.contract
        if not contract.columns:
            return []
        table = ctx.display_name
        physical = {c.name.lower(): c for c in ctx.schema}
        declared = {c.name.lower(): c for c in contract.columns}
        findings: list[Finding] = []

        for key, spec in declared.items():
            actual = physical.get(key)
            if actual is None:
                near = _near_match(spec.name, physical)
                findings.append(
                    Finding(
                        "schema.missing_column",
                        Severity.ERROR,
                        f"Contract column `{spec.name}` is missing from the table"
                        + (f" (found `{near}` - renamed?)." if near else "."),
                        table=table,
                        column=spec.name,
                        details={"possible_rename_of": near} if near else {},
                        suggestion=(
                            f"Rename `{near}` back to `{spec.name}` or update the contract."
                            if near
                            else "Restore the column or remove it from the contract (breaking change)."
                        ),
                    )
                )
                continue
            want, got = normalize_type(spec.type), normalize_type(actual.data_type)
            if want and got and want != got:
                findings.append(
                    Finding(
                        "schema.type_mismatch",
                        Severity.ERROR,
                        f"Column `{actual.name}` is `{got}` but the contract declares `{want}`.",
                        table=table,
                        column=actual.name,
                        details={"expected": want, "actual": got},
                        suggestion=f"Cast to `{want}` in the transformation or update the contract.",
                    )
                )

        for key, actual in physical.items():
            if key in declared:
                continue
            findings.append(
                Finding(
                    "schema.unexpected_column",
                    Severity.INFO if contract.allow_extra_columns else Severity.ERROR,
                    f"Column `{actual.name}` exists in the table but not in the contract.",
                    table=table,
                    column=actual.name,
                    suggestion="Add it to the contract (with type and nullability) so it is governed.",
                )
            )
        return findings


def _near_match(name: str, physical: dict) -> str | None:
    target = to_snake_case(name).replace("_", "")
    for key, col in physical.items():
        if to_snake_case(col.name).replace("_", "") == target and key != name.lower():
            return col.name
    return None
