from __future__ import annotations

import json

import pandas as pd
import pytest
import requests

from agentic_devops.config import EnvironmentConfig, ValidationConfig
from agentic_devops.core.audit import DatabricksSqlAuditSink
from agentic_devops.integrations.azure_devops import AzureDevOpsClient
from agentic_devops.integrations.databricks import DatabricksClient, DatabricksRunError
from agentic_devops.integrations.http import ApiError, HttpClient
from agentic_devops.validation.backends import PandasBackend
from agentic_devops.validation.contracts import contract_from_dict
from agentic_devops.validation.executors import DatabricksJobExecutor
from agentic_devops.validation.job import run_from_input
from agentic_devops.validation.naming import NamingStandards


class RecordingHttp:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def request(self, method, url, **kw):
        self.calls.append((method, url, kw))
        for (m, fragment), value in self.responses.items():
            if m == method and fragment in url:
                return value(kw) if callable(value) else value
        return {}


def ado(http):
    return AzureDevOpsClient("https://dev.azure.com/contoso", "Data Platform", "pipelines", http)


def test_create_pr_payload():
    http = RecordingHttp(
        {
            ("POST", "/pullrequests"): {
                "pullRequestId": 5,
                "sourceRefName": "refs/heads/f",
                "targetRefName": "refs/heads/main",
            }
        }
    )
    pr = ado(http).create_pr("feature/1-x", "main", "t" * 500, "d" * 5000, [12])
    method, url, kw = http.calls[0]
    assert (
        url == "https://dev.azure.com/contoso/Data%20Platform/_apis/git/repositories/pipelines/pullrequests"
    )
    assert kw["params"]["api-version"] == "7.1"
    body = kw["json"]
    assert body["sourceRefName"] == "refs/heads/feature/1-x" and body["targetRefName"] == "refs/heads/main"
    assert len(body["title"]) == 400 and len(body["description"]) == 4000
    assert body["workItemRefs"] == [{"id": "12"}]
    assert pr.id == 5 and pr.web_url.endswith("/_git/pipelines/pullrequest/5")


def test_auto_complete_never_bypasses_policy():
    http = RecordingHttp(
        {
            ("GET", "connectionData"): {"authenticatedUser": {"id": "agent-identity"}},
            ("PATCH", "/pullrequests/5"): {"pullRequestId": 5, "sourceRefName": "a", "targetRefName": "b"},
        }
    )
    ado(http).set_auto_complete(5, "squash", True, True, "msg")
    body = http.calls[-1][2]["json"]
    assert body["autoCompleteSetBy"] == {"id": "agent-identity"}
    assert body["completionOptions"]["bypassPolicy"] is False
    assert body["completionOptions"]["mergeStrategy"] == "squash"


def test_status_and_branch_head_exact_match():
    http = RecordingHttp(
        {
            ("GET", "/refs"): {
                "value": [
                    {"name": "refs/heads/main-old", "objectId": "1" * 40},
                    {"name": "refs/heads/main", "objectId": "2" * 40},
                ]
            }
        }
    )
    client = ado(http)
    assert client.get_branch_head("main") == "2" * 40  # filter is a prefix match
    client.post_pr_status(5, "failed", "data-validation", "agentic-devops", "x" * 300, "http://run", 3)
    body = http.calls[-1][2]["json"]
    assert body["context"] == {"name": "data-validation", "genre": "agentic-devops"}
    assert body["iterationId"] == 3 and len(body["description"]) == 250


def test_upsert_comment_updates_existing_thread():
    marker = "<!-- m -->"
    http = RecordingHttp(
        {
            ("GET", "/threads"): {
                "value": [
                    {"id": 1, "comments": [{"id": 1, "content": "other"}]},
                    {"id": 9, "comments": [{"id": 4, "content": f"{marker} old"}]},
                ]
            }
        }
    )
    tid = ado(http).upsert_pr_comment(5, marker, f"{marker} new", active=False)
    assert tid == 9
    assert (
        http.calls[1][0],
        http.calls[1][1].split("pullRequests/5/")[1],
    ) == ("PATCH", "threads/9/comments/4")
    assert http.calls[2][2]["json"] == {"status": "closed"}


def test_tags_use_peeled_commit():
    http = RecordingHttp(
        {
            ("GET", "/refs"): {
                "value": [
                    {"name": "refs/tags/v1.0.0", "objectId": "t" * 40, "peeledObjectId": "c" * 40},
                ]
            }
        }
    )
    tags = ado(http).list_tags("v")
    assert tags[0].name == "v1.0.0" and tags[0].commit_id == "c" * 40
    assert http.calls[0][2]["params"]["peelTags"] == "true"


class FakeResponse:
    def __init__(self, status, body=b"{}", headers=None):
        self.status_code = status
        self.content = body
        self.text = body.decode()
        self.headers = {"Content-Type": "application/json", **(headers or {})}

    def json(self):
        return json.loads(self.content)


def test_http_retries_429_then_succeeds():
    responses = [
        FakeResponse(429, headers={"Retry-After": "3"}),
        FakeResponse(503),
        FakeResponse(200, b'{"ok": 1}'),
    ]
    sleeps = []

    class Session:
        def request(self, *a, **k):
            return responses.pop(0)

    http = HttpClient(lambda: "Bearer x", session=Session(), sleep=sleeps.append)
    assert http.request("GET", "https://x") == {"ok": 1}
    assert sleeps[0] == 3.0 and len(sleeps) == 2


def test_http_raises_api_error():
    class Session:
        def request(self, *a, **k):
            return FakeResponse(403, b"forbidden")

    with pytest.raises(ApiError) as exc:
        HttpClient(lambda: "Bearer x", session=Session(), sleep=lambda s: None).request("GET", "https://x")
    assert exc.value.status == 403


def test_http_retries_connection_errors():
    calls = []

    class Session:
        def request(self, *a, **k):
            calls.append(1)
            raise requests.ConnectionError("reset")

    http = HttpClient(lambda: "x", session=Session(), sleep=lambda s: None, max_attempts=3)
    with pytest.raises(ApiError):
        http.request("GET", "https://x")
    assert len(calls) == 3


class FakeDatabricks:
    """Simulates the workspace: runs the real job entry point against pandas tables."""

    def __init__(self, tables, result_state="SUCCESS"):
        self.files = {}
        self.tables = tables
        self.result_state = result_state
        self.jobs = []

    def upload_file(self, path, content):
        self.files[path] = content

    def run_job(self, job_id, params, idempotency_token=None):
        self.jobs.append((job_id, params, idempotency_token))
        payload = json.loads(self.files[params["input_path"]])
        report = run_from_input(payload, PandasBackend(self.tables))
        self.files[params["output_path"]] = report.to_json().encode()
        return 42

    def wait_for_run(self, run_id, timeout_s, poll_s):
        if self.result_state != "SUCCESS":
            raise DatabricksRunError("failed")
        return {"run_page_url": "https://adb/run/42"}

    def download_file(self, path):
        return self.files[path]


def test_databricks_job_executor_round_trip():
    dbx = FakeDatabricks({"qa.s.t": pd.DataFrame({"id": [1, 1]})})
    env = EnvironmentConfig("qa", "qa", "https://adb-1.azuredatabricks.net", 77, "/Volumes/qa/ops/v")
    executor = DatabricksJobExecutor(lambda name: dbx, ValidationConfig(), NamingStandards())
    contract = contract_from_dict({"table": "s.t", "primary_key": ["id"]})
    report, url = executor.execute([contract], [contract], env, "run1", "full", {"pr_id": 3})
    assert not report.passed and url == "https://adb/run/42"
    job_id, params, token = dbx.jobs[0]
    assert job_id == 77 and params["input_path"] == "/Volumes/qa/ops/v/runs/run1/input.json"
    assert token == "agentic-run1-full"


def test_wait_for_run_handles_failure_and_timeout():
    states = iter(
        [
            {"state": {"life_cycle_state": "RUNNING"}},
            {"state": {"life_cycle_state": "TERMINATED", "result_state": "FAILED", "state_message": "OOM"}},
        ]
    )
    http = RecordingHttp({("GET", "runs/get"): lambda kw: next(states)})
    client = DatabricksClient("https://adb", http, sleep=lambda s: None)
    with pytest.raises(DatabricksRunError, match="OOM"):
        client.wait_for_run(1, timeout_s=100)

    clock = iter([0, 0, 1000])
    http = RecordingHttp({("GET", "runs/get"): {"state": {"life_cycle_state": "RUNNING"}}})
    client = DatabricksClient("https://adb", http, sleep=lambda s: None, monotonic=lambda: next(clock))
    with pytest.raises(DatabricksRunError, match="timed out"):
        client.wait_for_run(1, timeout_s=10)
    assert http.calls[-1][1].endswith("/runs/cancel")


def test_sql_audit_sink_is_parameterised():
    from agentic_devops.core.audit import AuditEvent

    calls = []

    class Client:
        def execute_sql(self, warehouse, stmt, params):
            calls.append((stmt, params))

    sink = DatabricksSqlAuditSink(Client(), "wh1", "ops.agentic.audit_events")
    sink.record(AuditEvent("2026-10-27T06:00:00+00:00", "r", "a", "x", "succeeded", "it's fine"))
    stmt, params = calls[0]
    assert "it's fine" not in stmt and ":message" in stmt
    assert {"name": "pr_id", "type": "INT"} in params  # NULL -> value omitted
    with pytest.raises(ValueError):
        DatabricksSqlAuditSink(Client(), "wh1", "x; DROP TABLE y")
