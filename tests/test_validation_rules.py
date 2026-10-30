from __future__ import annotations

import pandas as pd

from agentic_devops.config import ValidationConfig
from agentic_devops.models import Severity
from agentic_devops.validation.backends import PandasBackend
from agentic_devops.validation.contracts import contract_from_dict
from agentic_devops.validation.naming import load_naming_standards, to_snake_case
from agentic_devops.validation.rules import (
    ColumnNamingRule,
    CrossTableNamingRule,
    NullColumnsRule,
    PrimaryKeyRule,
    SchemaContractRule,
    TableContext,
)
from tests.conftest import REPO_ROOT

NAMING = load_naming_standards(REPO_ROOT / "config" / "naming_standards.yml")


def ctx_for(
    df: pd.DataFrame, contract: dict, config: ValidationConfig | None = None, schema=None
) -> TableContext:
    c = contract_from_dict({"table": "s.t", **contract})
    backend = PandasBackend({"cat.s.t": df}, schemas={"cat.s.t": schema} if schema else None)
    return TableContext(c, "cat.s.t", backend, config or ValidationConfig(), NAMING)


def ids(findings, severity=None):
    return sorted(f.rule_id for f in findings if severity is None or f.severity is severity)


# ----------------------------------------------------------------------------- primary key
def test_pk_unique_and_not_null_passes():
    df = pd.DataFrame({"id": [1, 2, 3], "v": ["a", "b", "c"]})
    assert PrimaryKeyRule().evaluate(ctx_for(df, {"primary_key": ["id"]})) == []


def test_pk_duplicates_reported_with_samples():
    df = pd.DataFrame({"id": [1, 1, 1, 2, 3, 3], "updated_at": pd.date_range("2026-01-01", periods=6)})
    findings = PrimaryKeyRule().evaluate(ctx_for(df, {"primary_key": ["id"]}))
    assert ids(findings) == ["pk.unique"]
    f = findings[0]
    assert f.severity is Severity.ERROR
    assert f.details["duplicate_key_groups"] == 2
    assert f.details["duplicate_rows"] == 5
    assert f.details["distinct_keys"] == 3
    assert f.details["sample_duplicates"][0] == {"key": {"id": 1}, "count": 3}
    assert "ORDER BY updated_at" in f.suggestion


def test_pk_nulls_and_composite_key():
    df = pd.DataFrame({"order_id": [1, 1, None, 2], "line": [1, 2, 1, 1]})
    findings = PrimaryKeyRule().evaluate(ctx_for(df, {"primary_key": ["order_id", "line"]}))
    assert ids(findings) == ["pk.not_null"]
    assert findings[0].details["null_key_rows"] == 1


def test_pk_missing_column_and_not_declared():
    df = pd.DataFrame({"a": [1]})
    assert ids(PrimaryKeyRule().evaluate(ctx_for(df, {"primary_key": ["id"]}))) == ["pk.columns_exist"]
    assert ids(PrimaryKeyRule().evaluate(ctx_for(df, {}))) == ["pk.declared"]
    relaxed = ValidationConfig(require_primary_key=False)
    assert PrimaryKeyRule().evaluate(ctx_for(df, {}, relaxed)) == []


def test_pk_column_lookup_is_case_insensitive():
    df = pd.DataFrame({"ID": [1, 2]})
    assert PrimaryKeyRule().evaluate(ctx_for(df, {"primary_key": ["id"]})) == []


# ----------------------------------------------------------------------------- null columns
def test_fully_null_column_is_error_unless_allowed():
    df = pd.DataFrame({"id": [1, 2], "empty": [None, None], "ok_empty": [None, None], "blank": ["", "  "]})
    contract = {"primary_key": ["id"], "columns": [{"name": "ok_empty", "allow_fully_null": True}]}
    findings = NullColumnsRule().evaluate(ctx_for(df, contract))
    by_col = {f.column: f for f in findings}
    assert by_col["empty"].severity is Severity.ERROR
    assert by_col["blank"].severity is Severity.ERROR  # blank strings count as NULL by default
    assert by_col["ok_empty"].severity is Severity.INFO
    assert "id" not in by_col


def test_blank_strings_not_null_when_disabled():
    df = pd.DataFrame({"id": [1, 2], "blank": ["", " "]})
    cfg = ValidationConfig(treat_blank_as_null=False)
    assert NullColumnsRule().evaluate(ctx_for(df, {"primary_key": ["id"]}, cfg)) == []


def test_high_null_ratio_and_not_nullable_violation():
    df = pd.DataFrame({"id": range(20), "sparse": [1] + [None] * 19, "must": [None] + list(range(19))})
    contract = {"primary_key": ["id"], "columns": [{"name": "must", "nullable": False}]}
    findings = NullColumnsRule().evaluate(ctx_for(df, contract))
    assert ids(findings) == ["nulls.high_null_ratio", "nulls.not_nullable_violation"]


def test_empty_table_and_min_rows():
    df = pd.DataFrame({"id": pd.Series([], dtype="int64")})
    findings = NullColumnsRule().evaluate(ctx_for(df, {"primary_key": ["id"], "validation": {"min_rows": 5}}))
    assert ids(findings) == ["table.empty", "table.min_rows"]


# ----------------------------------------------------------------------------- naming
def test_snake_case_helper():
    assert to_snake_case("CustomerID") == "customer_id"
    assert to_snake_case("Order Amount ") == "order_amount"
    assert to_snake_case("order-date") == "order_date"
    assert to_snake_case("2nd_address") == "c_2nd_address"
    assert NAMING.canonical_name("CustNo") == "customer_number"


def test_naming_convention_violations_with_suggestions():
    df = pd.DataFrame(columns=["order_id", "CustomerID", "Order Amount", "ship-date", "select"])
    findings = ColumnNamingRule().evaluate(ctx_for(df, {}))
    conv = {f.column: f for f in findings if f.rule_id == "naming.convention"}
    assert set(conv) == {"CustomerID", "Order Amount", "ship-date"}
    assert conv["CustomerID"].details["suggested_name"] == "customer_id"
    assert "whitespace" in conv["Order Amount"].message
    assert "special characters '-'" in conv["ship-date"].message
    assert any(f.rule_id == "naming.reserved_word" and f.column == "select" for f in findings)


def test_abbreviation_and_duplicate_spellings():
    df = pd.DataFrame(columns=["cust_id", "order_amt", "customerid", "customer_id", "Customer_Id"])
    findings = ColumnNamingRule().evaluate(ctx_for(df, {}))
    abbr = {
        f.column: f.details["suggested_name"]
        for f in findings
        if f.rule_id == "naming.non_standard_abbreviation"
    }
    assert abbr == {"cust_id": "customer_id", "order_amt": "order_amount"}
    assert any(
        f.rule_id == "naming.case_insensitive_duplicate" and f.severity is Severity.ERROR for f in findings
    )
    assert any(f.rule_id == "naming.normalized_duplicate" for f in findings)


def test_type_conventions():
    df = pd.DataFrame({"active": [True], "is_deleted": [False], "load_time": pd.to_datetime(["2026-01-01"])})
    findings = ColumnNamingRule().evaluate(ctx_for(df, {}))
    tc = {f.column: f.severity for f in findings if f.rule_id == "naming.type_convention"}
    assert tc == {"active": Severity.WARNING, "load_time": Severity.INFO}


def test_cross_table_inconsistency_and_type_mismatch():
    orders = ctx_for(pd.DataFrame({"order_id": [1], "cust_id": [1]}), {}, schema={"cust_id": "string"})
    reference = [
        contract_from_dict({"table": "s.customers", "columns": [{"name": "customer_id", "type": "bigint"}]}),
        contract_from_dict({"table": "s.payments", "columns": [{"name": "cust_id", "type": "bigint"}]}),
    ]
    findings = CrossTableNamingRule().evaluate([orders], reference, NAMING)
    incon = [f for f in findings if f.rule_id == "naming.cross_table_inconsistent"]
    assert len(incon) == 1 and incon[0].column == "cust_id"
    assert "s.customers.customer_id" in incon[0].details["other_usages"]
    mism = [f for f in findings if f.rule_id == "naming.cross_table_type_mismatch"]
    assert len(mism) == 1 and "s.payments.cust_id (bigint)" in mism[0].message


# ----------------------------------------------------------------------------- schema contract
def test_schema_contract_missing_renamed_extra_and_type():
    df = pd.DataFrame({"order_id": [1], "customerid": [2], "amount": [1.0], "extra": ["x"]})
    contract = {
        "columns": [
            {"name": "order_id", "type": "bigint"},
            {"name": "customer_id", "type": "bigint"},
            {"name": "amount", "type": "decimal(18,2)"},
        ],
        "validation": {"allow_extra_columns": False},
    }
    findings = SchemaContractRule().evaluate(ctx_for(df, contract))
    got = {(f.rule_id, f.column): f for f in findings}
    assert got[("schema.missing_column", "customer_id")].details["possible_rename_of"] == "customerid"
    assert got[("schema.type_mismatch", "amount")].details == {
        "expected": "decimal(18,2)",
        "actual": "double",
    }
    assert got[("schema.unexpected_column", "extra")].severity is Severity.ERROR
    assert got[("schema.unexpected_column", "customerid")].severity is Severity.ERROR


def test_type_aliases_are_equivalent():
    df = pd.DataFrame({"id": [1]})
    contract = {"columns": [{"name": "id", "type": "long"}]}
    assert SchemaContractRule().evaluate(ctx_for(df, contract)) == []
