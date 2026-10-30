"""Audit trail: every agent decision is recorded (JSONL pipeline artifact + optional Delta table)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

_IDENT = re.compile(r"^[A-Za-z0-9_]+(\.[A-Za-z0-9_]+){0,2}$")


@dataclass
class AuditEvent:
    timestamp: str
    run_id: str
    agent: str
    action: str
    status: str
    message: str
    pr_id: int | None = None
    commit: str | None = None
    environment: str | None = None
    dry_run: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class MemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


class JsonlAuditSink:
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def record(self, event: AuditEvent) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(event), default=str) + "\n")


class DatabricksSqlAuditSink:
    """Appends to a Delta table through a SQL warehouse (parameterised, no string interpolation of values).

    DDL: see docs/LLD.md section "Audit table".
    """

    def __init__(self, client: Any, warehouse_id: str, table: str):
        if not _IDENT.match(table):
            raise ValueError(f"Invalid audit table name: {table!r}")
        self.client = client
        self.warehouse_id = warehouse_id
        self.table = table

    def record(self, event: AuditEvent) -> None:
        params = [
            {"name": "ts", "value": event.timestamp, "type": "TIMESTAMP"},
            {"name": "run_id", "value": event.run_id},
            {"name": "agent", "value": event.agent},
            {"name": "action", "value": event.action},
            {"name": "status", "value": event.status},
            {"name": "message", "value": event.message[:4000]},
            {"name": "pr_id", "value": None if event.pr_id is None else str(event.pr_id), "type": "INT"},
            {"name": "commit_sha", "value": event.commit},
            {"name": "environment", "value": event.environment},
            {"name": "dry_run", "value": str(event.dry_run).lower(), "type": "BOOLEAN"},
            {"name": "details", "value": json.dumps(event.details, default=str)[:30000]},
        ]
        # The Statement Execution API reads an omitted "value" as NULL.
        params = [{k: v for k, v in p.items() if not (k == "value" and v is None)} for p in params]
        stmt = (
            f"INSERT INTO {self.table} (event_ts, run_id, agent, action, status, message, pr_id, commit_sha, "
            "environment, dry_run, details) VALUES (:ts, :run_id, :agent, :action, :status, :message, :pr_id, "
            ":commit_sha, :environment, :dry_run, :details)"
        )
        self.client.execute_sql(self.warehouse_id, stmt, params)


class CompositeAuditSink:
    """Writes to every sink; a failing sink is logged and does not stop the others."""

    def __init__(self, sinks: list[AuditSink]):
        self.sinks = sinks

    def record(self, event: AuditEvent) -> None:
        for sink in self.sinks:
            try:
                sink.record(event)
            except Exception:  # noqa: BLE001
                log.exception("Audit sink %s failed", type(sink).__name__)
