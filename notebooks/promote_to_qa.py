# Databricks notebook source
# MAGIC %md
# MAGIC # Promote my branch to QA
# MAGIC
# MAGIC Queues the `promote-to-qa` Azure DevOps pipeline for the branch this Git folder is on.
# MAGIC The QA Promotion Agent then opens (or refreshes) the PR into `main` with auto-complete on.
# MAGIC
# MAGIC Before running: commit and **push** your changes from the Git folder UI.
# MAGIC Auth: a PAT with *Build (read & execute)* in secret scope `agentic-devops`, key `ado-pat`
# MAGIC (or ask the platform team to enable the Entra ID service principal flow).

# COMMAND ----------

dbutils.widgets.text("work_item_id", "0", "Work item id (optional)")

# COMMAND ----------

import base64

import requests
from databricks.sdk import WorkspaceClient

ADO_ORG_URL = "https://dev.azure.com/contoso"      # set by the platform team
ADO_PROJECT = "DataPlatform"
PIPELINE_ID = 0                                   # id of pipelines/promote-to-qa.yml

ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()
notebook_path = ctx.notebookPath().get()
repo_path = "/".join(notebook_path.split("/")[:-2])  # <git folder>/notebooks/<this notebook>

w = WorkspaceClient()
repos = [r for r in w.repos.list(path_prefix=repo_path) if r.path and repo_path.endswith(r.path.rstrip("/"))]
if not repos:
    raise RuntimeError(f"{repo_path} is not a Databricks Git folder")
branch = repos[0].branch
print(f"Git folder {repo_path} is on branch {branch}")
if branch in ("main", "release"):
    raise RuntimeError("Switch to your feature branch first.")

# COMMAND ----------

pat = dbutils.secrets.get("agentic-devops", "ado-pat")
auth = "Basic " + base64.b64encode(f":{pat}".encode()).decode()
resp = requests.post(
    f"{ADO_ORG_URL}/{ADO_PROJECT}/_apis/pipelines/{PIPELINE_ID}/runs?api-version=7.1",
    headers={"Authorization": auth, "Content-Type": "application/json"},
    json={
        "resources": {"repositories": {"self": {"refName": "refs/heads/main"}}},
        "templateParameters": {"sourceBranch": branch, "workItemId": dbutils.widgets.get("work_item_id") or "0"},
    },
    timeout=30,
)
resp.raise_for_status()
run = resp.json()
print(f"Queued promotion run {run['id']}: {run['_links']['web']['href']}")
