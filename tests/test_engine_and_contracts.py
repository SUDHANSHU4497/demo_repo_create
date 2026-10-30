from __future__ import annotations

import json

import pandas as pd

from agentic_devops.config import ValidationConfig
from agentic_devops.models import Severity, ValidationReport
from agentic_devops.validation.backends import PandasBackend
from agentic_devops.validation.contracts import contract_from_dict, load_contracts, resolve_impacted_contracts
from agentic_devops.validation.engine import ValidationEngine
from agentic_devops.validation.job import run_from_input
from agentic_devops.validation.naming import load_naming_standards
from agentic_devops.validation.report import COMMENT_MARKER, render_markdown
from agentic_devops.validation.rules import Rule
from tests.conftest import REPO_ROOT, good_tables, write_contracts

NAMING = load_naming_standards(REPO_ROOT / "config" / "naming_standards.yml")


def test_engine_passes_on_good_data(tmp_path):
    contracts = load_contracts(write_contracts(tmp_path))
    engine = ValidationEngine(PandasBackend(good_tables("dev")), ValidationConfig(), NAMING)
    report = engine.validate(contracts, "dev", "dev", run_id="r1")
    assert report.passed, [f.to_dict() for f in report.all_findings]
    assert {t.table: t.row_count for t in report.tables} == {"sales.customers": 2, "sales.orders": 3}


def test_engine_reports_missing_table_and_fails(tmp_path):
    contracts = load_contracts(write_contracts(tmp_path))
    engine = ValidationEngine(PandasBackend({}), ValidationConfig(), NAMING)
    report = engine.validate(contracts, "dev", "dev")
    assert not report.passed
    assert {f.rule_id for f in report.all_findings} == {"schema.table_not_found"}


def test_engine_fails_closed_when_rule_crashes(tmp_path):
    class Boom(Rule):
        rule_id = "boom"

        def evaluate(self, ctx):
            raise RuntimeError("kaput")

    contracts = load_contracts(write_contracts(tmp_path))
    engine = ValidationEngine(PandasBackend(good_tables()), ValidationConfig(), NAMING, rules=[Boom()])
    report = engine.validate(contracts, "dev", "dev")
    assert not report.passed
    assert all(f.rule_id == "boom.execution_error" for f in report.all_findings)


def test_severity_overrides_and_fail_on_warnings():
    df = pd.DataFrame({"id": [1, 2], "cust_id": [1, 2]})
    contract = contract_from_dict({"table": "s.t", "primary_key": ["id"]})
    backend = PandasBackend({"c.s.t": df})

    report = ValidationEngine(backend, ValidationConfig(), NAMING).validate([contract], "c", "dev")
    assert report.passed and report.warning_count == 1

    strict = ValidationConfig(fail_on_warnings=True)
    assert not ValidationEngine(backend, strict, NAMING).validate([contract], "c", "dev").passed

    off = ValidationConfig(severity_overrides={"naming.*": "off"})
    assert ValidationEngine(backend, off, NAMING).validate([contract], "c", "dev").warning_count == 0

    up = ValidationConfig(severity_overrides={"naming.non_standard_abbreviation": "error"})
    assert ValidationEngine(backend, up, NAMING).validate([contract], "c", "dev").error_count == 1


def test_schema_only_profile_skips_data_scans():
    df = pd.DataFrame({"id": [1, 1]})  # duplicate key, but schema_only must not scan rows
    contract = contract_from_dict({"table": "s.t", "primary_key": ["id"]})
    report = ValidationEngine(PandasBackend({"c.s.t": df}), ValidationConfig(), NAMING).validate(
        [contract], "c", "prod", profile="schema_only"
    )
    assert report.passed and report.tables[0].row_count is None


def test_report_round_trip_and_markdown():
    df = pd.DataFrame({"id": [1, 1], "Bad Name": [None, None]})
    contract = contract_from_dict({"table": "s.t", "primary_key": ["id"]})
    report = ValidationEngine(PandasBackend({"c.s.t": df}), ValidationConfig(), NAMING).validate(
        [contract], "c", "dev", run_id="abc"
    )
    clone = ValidationReport.from_dict(json.loads(report.to_json()))
    assert clone.to_dict() == report.to_dict()
    md = render_markdown(report, advisor_markdown="1. **Dedupe**", commit="f" * 40)
    assert md.startswith(COMMENT_MARKER)
    assert "❌ FAILED" in md and "pk.unique" in md and "nulls.fully_null_column" in md
    assert "(id=1) ×2" in md and "Advisor notes" in md and "ffffffffff" in md


def test_job_entry_point_round_trip():
    contract = contract_from_dict({"table": "s.t", "primary_key": ["id"]})
    payload = {
        "run_id": "r9",
        "environment": "qa",
        "catalog": "qa",
        "contracts": [contract.to_dict()],
        "validation_config": ValidationConfig().to_dict(),
        "naming_standards": NAMING.to_dict(),
    }
    report = run_from_input(payload, PandasBackend({"qa.s.t": pd.DataFrame({"id": [1, 2]})}))
    assert report.passed and report.run_id == "r9"


def test_impact_analysis(tmp_path):
    contracts = load_contracts(write_contracts(tmp_path))
    rel = contracts[1].path  # whatever path the orders contract was loaded from

    def tables(files, global_paths=None):
        return sorted(c.table for c in resolve_impacted_contracts(contracts, files, global_paths or []))

    assert tables(["/src/pipelines/sales/orders/build.py"]) == ["sales.orders"]
    assert tables(["src/pipelines/hr/x.py", "README.md"]) == []
    assert tables(["/src/common/utils.py"], ["src/common/**"]) == ["sales.customers", "sales.orders"]
    assert tables([rel]) == [contracts[1].table]


def test_repo_contracts_and_config_are_valid():
    from agentic_devops.config import load_config

    contracts = load_contracts(REPO_ROOT / "contracts")
    assert {c.table for c in contracts} == {"sales.orders", "sales.customers", "sales.order_items"}
    cfg = load_config(REPO_ROOT / "config" / "agentic.yml")
    assert cfg.branches.qa == "main" and cfg.branches.prod == "release"
    assert cfg.validation.gate_environments == {"main": "dev", "release": "qa"}
    assert any(c.data_type == "boolean" for c in NAMING.type_conventions)
    assert NAMING.abbreviations["no"] == "number"
    assert Severity.ERROR.rank > Severity.WARNING.rank
