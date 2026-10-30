from __future__ import annotations

import json
from types import SimpleNamespace

import pandas as pd

from agentic_devops.config import LlmConfig, ValidationConfig
from agentic_devops.core.llm import ClaudeAdvisor, NullAdvisor, build_advisor
from agentic_devops.validation.backends import PandasBackend
from agentic_devops.validation.contracts import contract_from_dict
from agentic_devops.validation.engine import ValidationEngine
from agentic_devops.validation.naming import NamingStandards


class FakeMessages:
    def __init__(self, payload=None, stop_reason="end_turn", error=None):
        self.payload, self.stop_reason, self.error = payload, stop_reason, error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        text = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return SimpleNamespace(
            stop_reason=self.stop_reason, content=[SimpleNamespace(type="text", text=text)]
        )


def advisor(messages, **cfg):
    client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return ClaudeAdvisor(
        client, LlmConfig(enabled=True, provider="anthropic", **cfg), server_side_fallbacks=True
    )


def failing_report():
    df = pd.DataFrame({"id": [1, 1], "cust_id": [5, 6]})
    contract = contract_from_dict({"table": "s.t", "primary_key": ["id"]})
    eng = ValidationEngine(
        PandasBackend({"c.s.t": df}), ValidationConfig(), NamingStandards(abbreviations={"cust": "customer"})
    )
    return eng.validate([contract], "c", "dev")


def test_remediation_request_shape_and_rendering():
    msgs = FakeMessages(
        {
            "summary": "Duplicate order ids <script>",
            "actions": [
                {
                    "title": "Dedupe",
                    "detail": "Use ROW_NUMBER ![x](http://evil/x.png)",
                    "language": "sql",
                    "code": "SELECT 1 ``` DROP",
                }
            ],
        }
    )
    md = advisor(msgs).remediation(failing_report())
    call = msgs.calls[0]
    assert call["model"] == "claude-opus-5-5"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert call["output_config"]["effort"] == "medium"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert "<untrusted_input>" in call["messages"][0]["content"]
    # row values never leave the platform unless explicitly allowed
    assert "sample_duplicates" not in call["messages"][0]["content"]
    assert "&lt;script&gt;" in md and "evil" not in md
    assert "```sql" in md and "``` DROP" not in md


def test_sample_values_shared_only_when_enabled():
    msgs = FakeMessages({"summary": "s", "actions": []})
    advisor(msgs, share_sample_values=True).remediation(failing_report())
    assert "sample_duplicates" in msgs.calls[0]["messages"][0]["content"]


def test_failures_degrade_to_none():
    report = failing_report()
    assert advisor(FakeMessages(error=TimeoutError("boom"))).remediation(report) is None
    assert (
        advisor(FakeMessages({"summary": "x", "actions": []}, stop_reason="refusal")).remediation(report)
        is None
    )
    assert advisor(FakeMessages("not json")).pr_text({"x": 1}) is None


def test_pr_text_and_release_assessment():
    msgs = FakeMessages(
        {"title": "feat(sales): add x", "summary": "Adds x.", "changes": ["a"], "review_focus": ["b"]}
    )
    pr = advisor(msgs).pr_text({"source_branch": "feature/1-x"})
    assert pr.title == "feat(sales): add x" and "### Review focus" in pr.description

    msgs = FakeMessages(
        {
            "risk_level": "HIGH",
            "risk_reasons": ["PK change"],
            "highlights": ["h"],
            "data_changes": [],
            "rollback_considerations": ["RESTORE sales.orders"],
        }
    )
    ra = advisor(msgs).release_assessment({"version": "v1"})
    assert ra.risk_level == "high" and "Rollback considerations" in ra.notes_markdown


def test_build_advisor_disabled_or_unavailable():
    assert isinstance(build_advisor(LlmConfig(enabled=False)), NullAdvisor)
    # Foundry without a resource/credentials must not crash the pipeline
    assert isinstance(
        build_advisor(LlmConfig(enabled=True, provider="foundry")), (NullAdvisor, ClaudeAdvisor)
    )
