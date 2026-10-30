# ADR 0003 — Branching, merge strategies and policy enforcement

**Status:** Accepted · **Date:** 2026-10-30

## Decision
* `feature/*` → **`main`** (QA workspace) with **squash** merges: one commit per PR, Conventional Commit subject, linked work item.
* Only **QA-certified** commits (tag `qa-certified/*` created after a successful QA deploy + validation) can be released.
* The Release Agent creates **`rc/vX.Y.Z` pinned to the certified commit** and merges it into **`release`** (production) with a **merge commit** (`noFastForward`), so production history shows each release and `main` can keep moving during approval.
* `hotfix/*` branches go through QA certification as well (cd-qa triggers on `hotfix/*`), then the normal release path; a back-merge PR `release → main` is opened automatically after the production release.
* Enforcement lives in **branch policies**: required reviewers, build validation, required PR statuses (`agentic-devops/data-validation`, `qa-promotion`, `qa-certified`), comment resolution, linked work items. Agents only publish statuses and enable auto-complete; their identity is denied "bypass policies" and "force push".
* Production deploys additionally wait on the `databricks-prod` **environment** checks (approvers, business hours, exclusive lock).

## Consequences
+ Two independent human approvals for production (release PR + environment).
+ What was tested in QA is exactly what is released (pinned commit, certification tag).
− Release candidates that miss the change window wait for the next window (by design).
