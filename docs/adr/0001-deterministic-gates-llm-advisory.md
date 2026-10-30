# ADR 0001 — Deterministic gates, LLM as advisor only

**Status:** Accepted · **Date:** 2026-10-30

## Context
The system is called "agentic" and uses LLMs (served from Azure AI Foundry, see ADR 0005), but its outputs gate production releases of data pipelines. Gate decisions must be reproducible, explainable to auditors and resistant to manipulation. PR titles, descriptions and commit messages are free text written by anyone with push access, so anything an LLM reads from them is a prompt-injection vector.

## Decision
* Every decision that changes state or lets code through — pass/fail of validation, merge, tag, deployment, rollback — is made by deterministic code and Azure DevOps branch policies.
* The LLM is used for **language tasks**: PR titles/descriptions, remediation suggestions, release notes, and a risk opinion.
* The LLM may only make a gate **stricter** (raise release risk ⇒ add required reviewers), never looser (`escalate_only`).
* LLM output is never executed; it is sanitised before it is posted and validated (e.g. PR titles must match the Conventional Commit pattern, otherwise the template title is used).
* The LLM is optional (`llm.enabled: false` by default); every task has a template fallback, and any LLM failure, refusal or malformed output falls back silently.
* Only metadata is sent; row values are excluded unless explicitly allowed.

## Consequences
+ Same inputs ⇒ same gate result; auditors can trace each decision to a rule id.
+ A prompt-injected PR cannot talk its way through a gate.
− Less "autonomy": the agents will not, for example, auto-fix data or decide that a failing check is acceptable. That remains a human decision (change the contract, set a severity override) and is visible in the diff.
