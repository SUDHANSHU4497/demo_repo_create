# Databricks notebook source
# MAGIC %md
# MAGIC # Validate my tables (before raising a PR)
# MAGIC
# MAGIC Runs the **same rules** as the Data Validation Agent on the PR gate, interactively, against your
# MAGIC tables in the dev catalog: primary keys, fully-null columns, column-name consistency and the
# MAGIC schema contract. Fix findings here and the PR gate will be green the first time.
# MAGIC
# MAGIC Open this notebook from your **Databricks Git folder** (the repo checkout) on your feature branch.

# COMMAND ----------

dbutils.widgets.text("tables", "", "Contract tables (comma separated; blank = all)")
dbutils.widgets.text("catalog", "dev", "Catalog")
dbutils.widgets.dropdown("profile", "full", ["full", "schema_only"], "Profile")

# COMMAND ----------

import os
import sys

repo_root = os.path.abspath(os.path.join(os.getcwd(), ".."))
sys.path.insert(0, os.path.join(repo_root, "src"))

from agentic_devops.config import load_config
from agentic_devops.validation.backends import SparkBackend
from agentic_devops.validation.contracts import load_contracts
from agentic_devops.validation.engine import ValidationEngine
from agentic_devops.validation.naming import load_naming_standards

cfg = load_config(os.path.join(repo_root, "config", "agentic.yml"))
all_contracts = load_contracts(os.path.join(repo_root, cfg.validation.contracts_dir))
wanted = {t.strip().lower() for t in dbutils.widgets.get("tables").split(",") if t.strip()}
selected = [c for c in all_contracts if not wanted or c.table.lower() in wanted]
print(f"Validating {len(selected)} table(s): {[c.table for c in selected]}")

# COMMAND ----------

engine = ValidationEngine(
    SparkBackend(spark),
    cfg.validation,
    load_naming_standards(os.path.join(repo_root, cfg.validation.naming_standards_path)),
)
report = engine.validate(
    selected,
    catalog=dbutils.widgets.get("catalog"),
    environment="interactive",
    reference_contracts=all_contracts,
    profile=dbutils.widgets.get("profile"),
)
print(report.summary())

# COMMAND ----------

rows = [
    (f.severity.value, f.table, f.column, f.rule_id, f.message, f.suggestion)
    for f in sorted(report.all_findings, key=lambda f: -f.severity.rank)
]
display(spark.createDataFrame(rows or [("info", None, None, "run.passed", "No findings", None)],
                              "severity string, table string, column string, rule string, message string, fix string"))

# COMMAND ----------

assert report.passed, f"{report.error_count} error(s) - the PR gate would fail. See the table above."
