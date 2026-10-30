# Databricks notebook source
# MAGIC %md
# MAGIC # One-time setup: operational tables for the agentic CI/CD system
# MAGIC Run once per workspace (dev, qa, prod) as a platform admin. The schema and volume themselves are
# MAGIC created by the bundle (`resources/ops_schema.yml`).

# COMMAND ----------

dbutils.widgets.text("catalog", "qa", "Catalog")
catalog = dbutils.widgets.get("catalog")

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {catalog}.ops.validation_results (
  run_id       STRING    COMMENT 'Agent run id',
  environment  STRING,
  pr_id        STRING,
  commit       STRING,
  table_name   STRING    COMMENT 'Contract table (schema.table)',
  rule_id      STRING    COMMENT 'e.g. pk.unique, nulls.fully_null_column, naming.convention',
  severity     STRING    COMMENT 'error | warning | info',
  column_name  STRING,
  message      STRING,
  details      STRING    COMMENT 'JSON',
  run_passed   BOOLEAN,
  created_at   TIMESTAMP
) CLUSTER BY (table_name, created_at)
COMMENT 'Findings of every agentic data validation run'
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {catalog}.ops.audit_events (
  event_ts     TIMESTAMP,
  run_id       STRING,
  agent        STRING    COMMENT 'data-validation-agent | qa-promotion-agent | release-agent',
  action       STRING,
  status       STRING    COMMENT 'succeeded | waiting | skipped | blocked | failed',
  message      STRING,
  pr_id        INT,
  commit_sha   STRING,
  environment  STRING,
  dry_run      BOOLEAN,
  details      STRING    COMMENT 'JSON: outputs, actions taken, pipeline run link'
) CLUSTER BY (agent, event_ts)
COMMENT 'Audit trail of every agent decision'
""")

# COMMAND ----------

# Least privilege: the validation service principal reads data and writes only these tables.
# spark.sql(f"GRANT USE CATALOG, USE SCHEMA, SELECT ON CATALOG {catalog} TO `sp-agentic-validation-{catalog}`")
# spark.sql(f"GRANT MODIFY ON TABLE {catalog}.ops.validation_results TO `sp-agentic-validation-{catalog}`")
# spark.sql(f"GRANT READ VOLUME, WRITE VOLUME ON VOLUME {catalog}.ops.agentic_validation TO `sp-agentic-validation-{catalog}`")
