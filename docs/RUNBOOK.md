# Runbook

Every agent result names what happened and why; the PR comment, the pipeline log and the `agentic-*` artifact (JSONL audit + validation report) carry the details.

## Developers

| Symptom | Meaning | What to do |
|---|---|---|
| `data-validation` **failed** with `pk.unique` | duplicate keys in your dev output | Deduplicate (comment shows sample keys and a `ROW_NUMBER()` snippet) or switch append → `MERGE`. Re-run your job, push. |
| `pk.not_null` / `nulls.not_nullable_violation` | NULL keys / NOT NULL columns | Fix the join/mapping or `COALESCE`; quarantine bad rows. |
| `nulls.fully_null_column` | a column is NULL in every row | Usually a broken mapping or join. If intentional (feature not live), set `allow_fully_null: true` on the column in the contract — reviewers will see it. |
| `naming.convention` / `non_standard_abbreviation` | name breaks the standard | Rename to the suggested name; update the contract. |
| `naming.cross_table_inconsistent` | same concept named differently elsewhere | Align to the canonical name shown. |
| `schema.missing_column` with `possible_rename_of` | column renamed without updating the contract | Rename back or update the contract (breaking change ⇒ raises release risk). |
| `schema.table_not_found` | table doesn't exist in dev or SP can't read it | Run your pipeline in dev first; check grants. |
| `data-validation` **error** | validation could not run | Look at the pipeline log / Databricks run link; re-queue the build. The gate stays closed until it runs. |
| `qa-promotion` failed: conflicts | branch conflicts with `main` | In the Git folder: pull `main`, merge into your branch, resolve, push, re-run promote. |
| promote **blocked**: no work item | none found | Name branch `feature/<id>-…`, mention `AB#<id>`, or pass a work item id. |

Validate before raising a PR: `notebooks/validate_my_tables.py` runs the same rules.

## Platform / release managers

| Situation | Action |
|---|---|
| `release-prepare` **blocked**: change window / freeze | Wait for the window. Emergency: hotfix process (still certified in QA); changing the window is a reviewed config change. |
| blocked: "already in flight" | Complete or abandon the open `rc/*` PR first. |
| blocked: "rc branch exists at another commit" | A previous attempt left `rc/vX.Y.Z`; delete it if its PR was abandoned, re-run. |
| blocked: fresh QA validation failed | QA data regressed since certification. Investigate, fix forward via `main`. |
| Release PR shows HIGH risk | Release Managers were added as required reviewers. Review risk factors and the advisor notes. |
| `cd-prod` Verify failed → Rollback ran | Previous tag redeployed. Check jobs that ran on the bad version; restore affected tables with Delta time travel: `RESTORE TABLE prod.sales.orders TO TIMESTAMP AS OF '<deploy time>'`. Open a bug; fix forward through `main`. |
| Rollback found no previous tag | First release — disable the affected jobs in the prod workspace and fix forward. |
| Pipeline re-run after partial failure | Safe: agents are idempotent (reuse PR, comment, rc branch, tags). |
| Validation job slow | Add `validation.filter` to large contracts (bounded window), check serverless quotas, raise `timeout_minutes`. |
| LLM down / disabled | Nothing to do; templates are used. |

## Hotfix

1. `hotfix/<id>-<desc>` from `release`; fix; push.
2. `cd-qa` deploys the hotfix branch to QA and certifies it (QA temporarily runs the hotfix).
3. Run `release-prepare --commit <hotfix sha> --bump patch`.
4. Approve PR + `databricks-prod` environment.
5. `release-finalize` opens the back-merge PR `release → main`; merge it so `main` (and QA on next deploy) has the fix.

## Useful queries

```sql
-- Validation pass rate per week
SELECT date_trunc('week', created_at) AS wk, avg(cast(run_passed AS INT)) AS pass_rate
FROM qa.ops.validation_results GROUP BY 1 ORDER BY 1;

-- Most frequent failing rules by table
SELECT table_name, rule_id, count(*) AS n FROM dev.ops.validation_results
WHERE severity = 'error' AND created_at > now() - INTERVAL 30 DAYS GROUP BY ALL ORDER BY n DESC;

-- Agent decisions for one release
SELECT event_ts, agent, action, status, message FROM qa.ops.audit_events
WHERE details LIKE '%"version": "1.4.0"%' ORDER BY event_ts;
```
