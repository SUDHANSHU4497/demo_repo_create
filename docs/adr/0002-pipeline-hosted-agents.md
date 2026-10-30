# ADR 0002 — Host agents in Azure Pipelines first; event-driven host later

**Status:** Accepted · **Date:** 2026-10-30

## Options
1. **Azure Pipelines** runs `agentic-devops <command>` in YAML pipelines.
2. **Event-driven service**: service hooks → Azure Function → Service Bus → Container Apps job.
3. **Long-running agent service** (AKS / App Service) polling Azure DevOps.

## Decision
Option 1 for phase 1. The package is host-agnostic (`Orchestrator.handle_event` already routes service-hook payloads), so option 2 can be added without changing the agents.

## Rationale
* No new infrastructure, identities or network paths; reuses service connections (workload identity federation), environments with approvals, logs, artifacts and retention that the organisation already governs.
* Pipeline YAML for gates is taken from the protected branch, so a feature branch cannot alter its own gate.
* Agents are stateless reconcilers (state = PRs, statuses, tags in Azure DevOps), which fits short-lived pipeline jobs and makes re-runs safe.

## Consequences
+ Fast to adopt, easy to audit.
− Reaction time includes pipeline queue time (typically < 1 min on hosted agents).
− Long human waits (release approval) are not polled; instead Azure DevOps auto-complete performs the merge and the next pipeline (cd-prod) continues the flow.
