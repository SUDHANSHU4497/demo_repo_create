# ADR 0004 — Data validation runs in Databricks, driven by data contracts in git

**Status:** Accepted · **Date:** 2026-10-30

## Context
Primary-key, null and naming checks need to read production-sized Delta tables governed by Unity Catalog. Pulling data to a build agent is slow and moves data outside governance.

## Decision
* The agent (on the build agent) uploads a small JSON input to a UC **volume**, triggers the bundle-deployed **`agentic_data_validation`** serverless job via Jobs `run-now`, and reads back a JSON report. Only aggregates and ≤ 5 sample keys per finding leave the workspace.
* Rules run against a `TableBackend` interface: `SparkBackend` (one aggregation for all NULL counts, one shuffle per PK) in Databricks, `PandasBackend` in tests. The same engine powers the developer notebook, so results are identical before and after the PR.
* **Data contracts** (`contracts/**.yml`) declare primary key, columns, types, nullability and the `sources` globs of code that writes each table. They are reviewed with the code and drive impact analysis.
* PR into `main` validates **dev** tables (the feature's output); PR into `release` and certification validate **QA** tables; post-deploy production verification uses the `schema_only` profile (no scans).

## Consequences
+ Data stays governed; validation scales with Spark; one rule implementation everywhere.
− Requires each table to have a contract (the agent reports `pk.declared` until it does).
− Dev data may be unrepresentative — mitigated by the second validation on QA data before release.
