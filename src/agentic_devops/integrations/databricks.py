"""Azure Databricks REST client: Jobs 2.1, Files API (Unity Catalog volumes), SQL Statement Execution."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

from agentic_devops.integrations.http import HttpClient

TERMINAL_STATES = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}


class DatabricksRunError(RuntimeError):
    pass


class DatabricksClient:
    def __init__(
        self,
        workspace_url: str,
        http: HttpClient,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        if not workspace_url:
            raise ValueError("workspace_url is required")
        self.host = workspace_url.rstrip("/")
        self.http = http
        self.sleep = sleep
        self.monotonic = monotonic

    # -------------------------------------------------------------- jobs
    def run_job(
        self, job_id: int, job_parameters: dict[str, str], idempotency_token: str | None = None
    ) -> int:
        body: dict[str, Any] = {"job_id": job_id, "job_parameters": job_parameters}
        if idempotency_token:
            body["idempotency_token"] = idempotency_token[:64]
        return int(self.http.request("POST", f"{self.host}/api/2.1/jobs/run-now", json=body)["run_id"])

    def get_run(self, run_id: int) -> dict[str, Any]:
        return self.http.request("GET", f"{self.host}/api/2.1/jobs/runs/get", params={"run_id": run_id})

    def run_page_url(self, run: dict[str, Any]) -> str | None:
        return run.get("run_page_url")

    def wait_for_run(self, run_id: int, timeout_s: float, poll_s: float = 20.0) -> dict[str, Any]:
        deadline = self.monotonic() + timeout_s
        while True:
            run = self.get_run(run_id)
            state = run.get("state", {})
            if state.get("life_cycle_state") in TERMINAL_STATES:
                if state.get("result_state") != "SUCCESS":
                    raise DatabricksRunError(
                        f"Run {run_id} ended {state.get('life_cycle_state')}/{state.get('result_state')}: "
                        f"{state.get('state_message', '')} {run.get('run_page_url', '')}"
                    )
                return run
            if self.monotonic() > deadline:
                self.http.request("POST", f"{self.host}/api/2.1/jobs/runs/cancel", json={"run_id": run_id})
                raise DatabricksRunError(f"Run {run_id} timed out after {timeout_s:.0f}s and was cancelled")
            self.sleep(poll_s)

    # -------------------------------------------------------------- files (UC volumes)
    def upload_file(self, path: str, content: bytes) -> None:
        self.http.request(
            "PUT",
            f"{self.host}/api/2.0/fs/files{quote(path)}",
            params={"overwrite": "true"},
            data=content,
            headers={"Content-Type": "application/octet-stream"},
        )

    def download_file(self, path: str) -> bytes:
        return (
            self.http.request(
                "GET",
                f"{self.host}/api/2.0/fs/files{quote(path)}",
                headers={"Accept": "application/octet-stream"},
                raw=True,
            )
            or b""
        )

    # -------------------------------------------------------------- SQL (audit sink)
    def execute_sql(
        self, warehouse_id: str, statement: str, parameters: list[dict[str, Any]] | None = None
    ) -> dict:
        body = {
            "warehouse_id": warehouse_id,
            "statement": statement,
            "parameters": parameters or [],
            "wait_timeout": "30s",
            "on_wait_timeout": "CONTINUE",
        }
        return self.http.request("POST", f"{self.host}/api/2.0/sql/statements", json=body)
