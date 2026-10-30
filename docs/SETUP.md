# Setup guide — Azure DevOps + Azure Databricks

One-time setup for platform admins. Order matters: identities → Databricks → Azure DevOps → policies.

## 1. Identities (Microsoft Entra ID)

| Service principal | Used by | Databricks workspace role |
|---|---|---|
| `sp-agentic-dev` | `pr-validation` (PR → main), `promote-to-qa` | dev workspace user; `SELECT` on `dev` catalog; write `dev.ops` |
| `sp-agentic-qa` | `cd-qa`, `pr-validation` (PR → release), `release-prepare` | qa workspace user + bundle deployer (`run_as` for qa jobs) |
| `sp-agentic-prod` | `cd-prod` only | prod workspace user + bundle deployer (`run_as` for prod jobs) |

Add each SP to its workspace (Account console → Workspaces → Permissions) and grant Unity Catalog privileges (see `notebooks/setup_ops_tables.py`).

## 2. Databricks (per workspace)

1. Deploy the bundle once by hand (or let the CD pipeline do it): `databricks bundle deploy -t qa --var service_principal_id=<app-id>`. This creates the `ops` schema, the `agentic_validation` volume and the `agentic-data-validation-<target>` job.
2. Run `notebooks/setup_ops_tables.py` with `catalog=<env>` to create `ops.validation_results` and `ops.audit_events`.
3. Note the job id: `databricks jobs list --output json | jq '.[] | select(.settings.name|startswith("agentic-data-validation")) | .job_id'`.
4. Developers: Git folder (Workspace → Create → Git folder) pointing at the Azure Repos repository; secret scope `agentic-devops` with key `ado-pat` if they use `notebooks/promote_to_qa.py`.

## 3. Azure DevOps

### 3.1 Service connections (Project settings → Service connections)
Create three **Azure Resource Manager** connections using **workload identity federation**: `sc-databricks-dev`, `sc-databricks-qa`, `sc-databricks-prod`, each bound to the matching service principal. Restrict *Pipeline permissions*: `sc-databricks-prod` → `cd-prod` only.

### 3.2 Variable groups (Pipelines → Library), Key Vault-linked where secret

| Group | Variables |
|---|---|
| `agentic-devops-common` | `ADO_ORG_URL`, `ADO_PROJECT`, `ADO_REPOSITORY`, `ADO_RELEASE_MANAGERS_GROUP_ID`, `TEAMS_WEBHOOK_URL` (secret), `AUDIT_SINK`, `DBX_AUDIT_WAREHOUSE_ID`, `DBX_QA_HOST`, and for the LLM layer: `LLM_ENABLED`, `AZURE_AI_PROJECT_ENDPOINT`, `FOUNDRY_CLAUDE_DEPLOYMENT`, `FOUNDRY_GPT_DEPLOYMENT`, `FOUNDRY_GPT_MINI_DEPLOYMENT`, `FOUNDRY_MISTRAL_DEPLOYMENT`, optional `LLM_REMEDIATION_MODEL` / `LLM_PR_MODEL` / `LLM_RELEASE_MODEL`, `LLM_TRACING`, `APPLICATIONINSIGHTS_CONNECTION_STRING` (secret) |
| `agentic-devops-dev` | `DBX_DEV_HOST`, `DBX_DEV_VALIDATION_JOB_ID`, `DBX_SP_APP_ID` |
| `agentic-devops-qa` | `DBX_QA_HOST`, `DBX_QA_VALIDATION_JOB_ID`, `DBX_SP_APP_ID` |
| `agentic-devops-prod` | `DBX_PROD_HOST`, `DBX_PROD_VALIDATION_JOB_ID`, `DBX_SP_APP_ID` |

### 3.3 Pipelines
Create five pipelines from existing YAML: `pipelines/pr-validation.yml`, `promote-to-qa.yml`, `cd-qa.yml`, `release-prepare.yml`, `cd-prod.yml`. Note the id of `promote-to-qa` for the Databricks notebook.

### 3.4 Environments (Pipelines → Environments)

| Environment | Checks |
|---|---|
| `databricks-qa` | none (or a single approver during rollout) |
| `databricks-prod` | Approvals: *Release Managers* (min 1, requester cannot approve) · Business hours (aligned with `release.change_window`) · Exclusive lock |

### 3.5 Repository permissions (Project settings → Repositories → *repo* → Security)
For **`<Project> Build Service (<Org>)`**:

| Permission | Value |
|---|---|
| Contribute to pull requests | Allow |
| Create branch | Allow (optionally restrict via branch security to `rc/*`) |
| Create tag | Allow |
| Contribute | Allow (needed for `rc/*` refs) |
| Bypass policies when completing pull requests | **Deny** |
| Bypass policies when pushing | **Deny** |
| Force push (rewrite history, delete branches and tags) | **Deny** |

Also enable *Project settings → Pipelines → Settings*: "Limit job authorization scope to current project" and grant the build service access to the repository.

### 3.6 Branch policies

**`main`**
* Require a minimum number of reviewers: 1 (reset votes on new pushes; requestor cannot approve).
* Check for linked work items: Required.
* Check for comment resolution: Required.
* Limit merge types: Squash merge only.
* Build validation: `pr-validation`, trigger automatic, *Required*, expire after 12 h.
* Status checks (Required, "Reset status whenever there are new changes"):
  * `agentic-devops/data-validation`
  * `agentic-devops/qa-promotion`

**`release`**
* Minimum reviewers: 2; automatically included reviewers: *Release Managers* (required).
* Limit merge types: Basic merge (no fast-forward) only.
* Build validation: `pr-validation`, Required.
* Status checks (Required): `agentic-devops/data-validation`, `agentic-devops/qa-certified`.
* Comment resolution: Required.
* Restrict who can push directly: nobody (PRs only).

### 3.7 Optional: service hooks (event-driven mode)
Project settings → Service hooks → Web Hooks: *Pull request created*, *Pull request updated*, *Code pushed* → your Function URL (see HLD §8). Set `qa_promotion.auto_promote_on_push: true` to open PRs on push.

## 4. Azure AI Foundry models (LangChain)

The agents use LangChain chat models from an Azure AI Foundry **project**. Which model does which task is configuration (`config/agentic.yml` → `llm.models`, `llm.tasks`).

1. **Project**: in Azure AI Foundry create (or reuse) a project. Copy its endpoint: `https://<resource>.services.ai.azure.com/api/projects/<project>` → `AZURE_AI_PROJECT_ENDPOINT`.
2. **Deploy models** you want to evaluate, e.g. a Claude model, `gpt-4.1`, `gpt-4.1-mini`, `Mistral-Large-2411`. Put each **deployment name** in its variable (`FOUNDRY_CLAUDE_DEPLOYMENT`, `FOUNDRY_GPT_DEPLOYMENT`, …). Add more under `llm.models` (`kind: foundry_openai` for OpenAI-compatible models, `foundry_anthropic` for Claude).
3. **RBAC (Entra ID, no keys)**: grant `sp-agentic-dev`, `sp-agentic-qa` and `sp-agentic-prod` the **Azure AI User** role on the Foundry project (or *Cognitive Services User* on the resource). Developers running locally need the same role and `az login`.
4. **Choose models per task**: edit `llm.tasks` or set `LLM_REMEDIATION_MODEL`, `LLM_PR_MODEL`, `LLM_RELEASE_MODEL` in the variable group. Keep a fallback from a different model family.
5. **Verify**: `agentic-devops models --check` → every model reports `ok`. Typical errors: `403` (role missing), `404 DeploymentNotFound` (deployment name), DNS/`401` (endpoint or tenant).
6. **Enable**: `LLM_ENABLED=true`. Optional tracing: `LLM_TRACING=azure_monitor` + `APPLICATIONINSIGHTS_CONNECTION_STRING`, and install `agentic-devops[tracing]`.

Data sent to models: rule ids, counts, column names, commit subjects and — for remediation only — files changed in the PR (`share_source_code: true`). Row values stay out unless `share_sample_values: true`. If policy requires, set `share_source_code: false`.

## 5. Smoke test

```bash
pip install -e ".[dev,agents,foundry]"
export AZURE_DEVOPS_PAT=...           # local only
az login                              # Entra token for Databricks
agentic-devops --dry-run validate --pr-id 123
agentic-devops --dry-run promote-qa --source-branch feature/123-test
agentic-devops --dry-run release-prepare
agentic-devops models --check          # Foundry deployments reachable with your identity
agentic-devops graph --name qa-promotion
```

`--dry-run` reads everything and prints `planned_actions` without writing to Azure DevOps.
