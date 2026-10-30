from __future__ import annotations

import pandas as pd

from agentic_devops.agents.data_validation_agent import STATUS_NAME, DataValidationAgent, ValidationRequest
from agentic_devops.integrations.azure_devops import PullRequest
from agentic_devops.models import AgentStatus
from agentic_devops.validation.report import COMMENT_MARKER
from tests.fakes import StubExecutor


def open_pr(fake_ado, pr_id=7, target="main", files=None):
    fake_ado.prs[pr_id] = PullRequest(
        pr_id, "feat: x", "feature/1-x", target, last_merge_source_commit="c" * 40
    )
    fake_ado.pr_files[pr_id] = files or ["/src/pipelines/sales/orders/job.py"]
    return fake_ado.prs[pr_id]


def test_pr_gate_passes_and_posts_status_and_comment(make_ctx, fake_ado):
    open_pr(fake_ado)
    result = DataValidationAgent(make_ctx()).run(ValidationRequest(pr_id=7))
    assert result.status is AgentStatus.SUCCEEDED, result.message
    assert result.outputs["tables"] == ["sales.orders"]  # only the impacted table
    states = [s["state"] for s in fake_ado.statuses if s["name"] == STATUS_NAME]
    assert states == ["pending", "succeeded"]
    comment = fake_ado.comments[(7, COMMENT_MARKER)]
    assert "✅ PASSED" in comment["content"] and comment["active"] is False


def test_pr_gate_fails_on_bad_data(make_ctx, fake_ado, tables):
    tables["dev.sales.orders"] = pd.DataFrame(
        {
            "order_id": [1, 1],
            "customer_id": [None, None],
            "order_amount": [1.0, 2.0],
            "discount_code": [None, None],
        }
    )
    open_pr(fake_ado)
    ctx = make_ctx()
    result = DataValidationAgent(ctx).run(ValidationRequest(pr_id=7))
    assert result.status is AgentStatus.FAILED
    assert fake_ado.last_status(7, STATUS_NAME)["state"] == "failed"
    content = fake_ado.comments[(7, COMMENT_MARKER)]["content"]
    assert "pk.unique" in content and "nulls.fully_null_column" in content
    assert fake_ado.comments[(7, COMMENT_MARKER)]["active"] is True
    assert (ctx.artifacts_dir / "validation-report-dev.json").exists()


def test_pr_without_governed_tables_is_skipped_but_green(make_ctx, fake_ado):
    open_pr(fake_ado, files=["/README.md"])
    result = DataValidationAgent(make_ctx()).run(ValidationRequest(pr_id=7))
    assert result.status is AgentStatus.SKIPPED
    assert fake_ado.last_status(7, STATUS_NAME)["state"] == "succeeded"


def test_release_pr_validates_qa_data(make_ctx, fake_ado):
    open_pr(fake_ado, target="release")
    stub = StubExecutor()
    DataValidationAgent(make_ctx(), executor=stub).run(ValidationRequest(pr_id=7))
    assert stub.calls[0]["env"] == "qa"


def test_executor_crash_fails_closed(make_ctx, fake_ado):
    open_pr(fake_ado)
    agent = DataValidationAgent(make_ctx(), executor=StubExecutor(error=TimeoutError("cluster")))
    result = agent.run(ValidationRequest(pr_id=7))
    assert result.status is AgentStatus.FAILED
    assert fake_ado.last_status(7, STATUS_NAME)["state"] == "error"
    assert "ERROR" in fake_ado.comments[(7, COMMENT_MARKER)]["content"]


def test_non_gated_branch_and_closed_pr_are_skipped(make_ctx, fake_ado):
    open_pr(fake_ado, pr_id=8, target="develop")
    agent = DataValidationAgent(make_ctx())
    assert agent.run(ValidationRequest(pr_id=8)).status is AgentStatus.SKIPPED
    open_pr(fake_ado, pr_id=9).status = "completed"
    assert agent.run(ValidationRequest(pr_id=9)).status is AgentStatus.SKIPPED
    assert fake_ado.statuses == []


def test_dry_run_writes_nothing(make_ctx, fake_ado):
    open_pr(fake_ado)
    result = DataValidationAgent(make_ctx(dry_run=True)).run(ValidationRequest(pr_id=7))
    assert result.status is AgentStatus.SUCCEEDED
    assert fake_ado.statuses == [] and fake_ado.comments == {}
    assert any("status" in a for a in result.planned_actions)


def test_rerun_updates_single_comment(make_ctx, fake_ado):
    open_pr(fake_ado)
    agent = DataValidationAgent(make_ctx())
    agent.run(ValidationRequest(pr_id=7))
    agent.run(ValidationRequest(pr_id=7))
    assert len(fake_ado.comments) == 1


def test_audit_event_recorded(make_ctx, fake_ado):
    open_pr(fake_ado)
    ctx = make_ctx()
    DataValidationAgent(ctx).run(ValidationRequest(pr_id=7))
    event = ctx.audit.events[-1]
    assert (event.agent, event.action, event.status, event.pr_id) == (
        "data-validation-agent",
        "validate",
        "succeeded",
        7,
    )


def test_failed_validation_hands_remediation_context_to_advisor(make_ctx, fake_ado, tables):
    tables["dev.sales.orders"] = pd.DataFrame(
        {"order_id": [1, 1], "customer_id": [1, 2], "order_amount": [1.0, 2.0], "discount_code": [None, None]}
    )
    open_pr(fake_ado)
    fake_ado.files[("/src/pipelines/sales/orders/job.py", "c" * 40)] = "df.write.saveAsTable('sales.orders')"
    seen = {}

    class Advisor:
        def remediation(self, report, context=None):
            seen["files"] = context.changed_files
            seen["code"] = context.read_file("/src/pipelines/sales/orders/job.py")
            seen["contracts"] = sorted(c.table for c in context.contracts)
            return "1. **Use MERGE** — append creates duplicates"

        def pr_text(self, context):
            return None

        def release_assessment(self, context):
            return None

    result = DataValidationAgent(make_ctx(advisor=Advisor())).run(ValidationRequest(pr_id=7))
    assert result.status is AgentStatus.FAILED and result.outputs["advisor_used"] is True
    assert seen == {
        "files": ["/src/pipelines/sales/orders/job.py"],
        "code": "df.write.saveAsTable('sales.orders')",
        "contracts": ["sales.customers", "sales.orders"],
    }
    assert "Use MERGE" in fake_ado.comments[(7, COMMENT_MARKER)]["content"]
