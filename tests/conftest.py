from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import yaml

from agentic_devops.config import AppConfig
from agentic_devops.core.agent import AgentContext
from agentic_devops.core.audit import MemoryAuditSink
from agentic_devops.llm.base import NullAdvisor
from agentic_devops.validation.backends import PandasBackend
from tests.fakes import FakeAdo

REPO_ROOT = Path(__file__).resolve().parents[1]

CONTRACTS = {
    "sales/orders.yml": {
        "table": "sales.orders",
        "primary_key": ["order_id"],
        "sources": ["src/pipelines/sales/orders/**"],
        "columns": [
            {"name": "order_id", "type": "bigint", "nullable": False},
            {"name": "customer_id", "type": "bigint", "nullable": False},
            {"name": "order_amount", "type": "double"},
            {"name": "discount_code", "type": "string", "allow_fully_null": True},
        ],
        "validation": {"min_rows": 1},
    },
    "sales/customers.yml": {
        "table": "sales.customers",
        "primary_key": ["customer_id"],
        "sources": ["src/pipelines/sales/customers/**"],
        "columns": [
            {"name": "customer_id", "type": "bigint", "nullable": False},
            {"name": "customer_name", "type": "string"},
        ],
    },
}


def good_tables(catalog: str = "dev") -> dict[str, pd.DataFrame]:
    return {
        f"{catalog}.sales.orders": pd.DataFrame(
            {
                "order_id": [1, 2, 3],
                "customer_id": [10, 11, 10],
                "order_amount": [9.5, 20.0, 3.25],
                "discount_code": [None, None, None],
            }
        ),
        f"{catalog}.sales.customers": pd.DataFrame(
            {"customer_id": [10, 11], "customer_name": ["Asha", "Ravi"]}
        ),
    }


def write_contracts(root: Path, contracts: dict | None = None) -> Path:
    cdir = root / "contracts"
    for rel, data in (contracts or CONTRACTS).items():
        p = cdir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(yaml.safe_dump(data), encoding="utf-8")
    return cdir


def make_config(tmp_path: Path, **sections) -> AppConfig:
    cdir = write_contracts(tmp_path)
    raw = {
        "azure_devops": {"organization_url": "https://dev.azure.com/x", "project": "p", "repository": "r"},
        "environments": {
            "dev": {"catalog": "dev"},
            "qa": {"catalog": "qa"},
            "prod": {"catalog": "prod"},
        },
        "validation": {
            "contracts_dir": str(cdir),
            "naming_standards_path": str(REPO_ROOT / "config" / "naming_standards.yml"),
            "execution_mode": "local",
            "global_impact_paths": ["src/common/**"],
        },
        "release": {"initial_version": "1.0.0", "high_risk_reviewer_ids": ["release-managers"]},
    }
    for key, value in sections.items():
        raw.setdefault(key, {}).update(value)
    return AppConfig.from_dict(raw)


@pytest.fixture
def fake_ado() -> FakeAdo:
    return FakeAdo(branches={"main": "a" * 40, "release": "b" * 40})


@pytest.fixture
def tables() -> dict[str, pd.DataFrame]:
    t = good_tables("dev")
    t.update(good_tables("qa"))
    t.update(good_tables("prod"))
    return t


@pytest.fixture
def make_ctx(tmp_path, fake_ado, tables):
    def _make(config: AppConfig | None = None, advisor=None, now=None, dry_run=False):
        cfg = config or make_config(tmp_path)
        return AgentContext(
            config=cfg,
            ado=fake_ado,
            advisor=advisor or NullAdvisor(),
            audit=MemoryAuditSink(),
            notifier=RecordingNotifier(),
            backend_factory=lambda env: PandasBackend(tables),
            clock=lambda: now or datetime(2026, 10, 27, 6, 0, tzinfo=timezone.utc),  # Tue 11:30 IST
            dry_run=dry_run,
            artifacts_dir=tmp_path / "artifacts",
            run_id="testrun",
        )

    return _make


class RecordingNotifier:
    def __init__(self):
        self.messages: list[tuple[str, str, str]] = []

    def notify(self, title, text, level="info", url=None):
        self.messages.append((title, text, level))
