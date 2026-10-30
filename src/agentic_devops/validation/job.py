"""Entry point of the ``agentic_data_validation`` Databricks job (python_wheel_task).

The Data Validation Agent (running in an Azure DevOps pipeline) uploads an input document to a
Unity Catalog volume, triggers this job, waits, and reads the output document back:

    input  : {run_id, environment, catalog, profile, contracts[], reference_contracts[],
              validation_config{}, naming_standards{}, results_table}
    output : ValidationReport.to_dict()

Results are also appended to a Delta table so data owners can trend quality over time.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agentic_devops.config import ValidationConfig
from agentic_devops.models import ValidationReport
from agentic_devops.validation.backends import SparkBackend, TableBackend
from agentic_devops.validation.contracts import contract_from_dict
from agentic_devops.validation.engine import ValidationEngine
from agentic_devops.validation.naming import NamingStandards

log = logging.getLogger(__name__)


def run_from_input(payload: dict[str, Any], backend: TableBackend) -> ValidationReport:
    contracts = [contract_from_dict(c) for c in payload["contracts"]]
    reference = [contract_from_dict(c) for c in payload.get("reference_contracts") or payload["contracts"]]
    engine = ValidationEngine(
        backend=backend,
        config=ValidationConfig.from_dict(payload.get("validation_config") or {}),
        naming=NamingStandards.from_dict(payload.get("naming_standards") or {}),
    )
    return engine.validate(
        contracts,
        catalog=payload["catalog"],
        environment=payload["environment"],
        run_id=payload["run_id"],
        reference_contracts=reference,
        profile=payload.get("profile", "full"),
    )


def write_results_table(spark: Any, table: str, report: ValidationReport, context: dict[str, Any]) -> None:
    created_at = datetime.now(timezone.utc)
    pr_id = str(context["pr_id"]) if context.get("pr_id") is not None else None
    commit = context.get("commit")
    rows = [
        (
            report.run_id,
            report.environment,
            pr_id,
            commit,
            f.table,
            f.rule_id,
            f.severity.value,
            f.column,
            f.message,
            json.dumps(f.details, default=str),
            report.passed,
            created_at,
        )
        for f in report.all_findings
    ] or [
        (
            report.run_id,
            report.environment,
            pr_id,
            commit,
            None,
            "run.passed",
            "info",
            None,
            "No findings",
            "{}",
            report.passed,
            created_at,
        )
    ]
    schema = (
        "run_id string, environment string, pr_id string, commit string, table_name string, rule_id string, "
        "severity string, column_name string, message string, details string, run_passed boolean, "
        "created_at timestamp"
    )
    spark.createDataFrame(rows, schema).write.mode("append").saveAsTable(table)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="Agentic DevOps data validation job")
    parser.add_argument("--input-path", required=True)
    parser.add_argument("--output-path", required=True)
    args = parser.parse_args(argv)

    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    payload = json.loads(Path(args.input_path).read_text(encoding="utf-8"))
    report = run_from_input(payload, SparkBackend(spark))

    out = Path(args.output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report.to_json(), encoding="utf-8")

    if payload.get("results_table"):
        try:
            write_results_table(spark, payload["results_table"], report, payload.get("context") or {})
        except Exception:  # noqa: BLE001 - history table is best effort; the report file is the contract
            log.exception("Could not append to %s", payload["results_table"])

    log.info("Validation %s: %s", report.run_id, report.summary())
    # The job itself succeeds whenever validation ran; pass/fail is read from the report so the
    # agent can tell "data is bad" apart from "validation could not run".
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
