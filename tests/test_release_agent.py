from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from agentic_devops.agents.data_validation_agent import DataValidationAgent
from agentic_devops.agents.release_agent import COMMENT_MARKER, QA_CERT_STATUS, ReleaseAgent
from agentic_devops.core.llm import ReleaseAssessment
from agentic_devops.integrations.azure_devops import Commit, GitTag, PullRequest
from agentic_devops.models import AgentStatus
from tests.conftest import make_config

CAND = "c" * 40
MERGE = "e" * 40


def agent(ctx):
    return ReleaseAgent(ctx, DataValidationAgent(ctx))


def seed(fake_ado, messages=("Merged PR 12: feat(sales): orders dedupe", "Merged PR 13: fix: typo #4521")):
    fake_ado.tags += [
        GitTag("v1.3.0", "1" * 40),
        GitTag("v1.2.9", "2" * 40),
        GitTag("qa-certified/20261026-100000-cccccccc", CAND),
    ]
    fake_ado.commits[(CAND, "release")] = [Commit(f"{i:040d}", m) for i, m in enumerate(messages)]
    fake_ado.diffs[("release", CAND)] = ["/src/pipelines/sales/orders/job.py"]


def test_prepare_opens_release_pr(make_ctx, fake_ado):
    seed(fake_ado)
    result = agent(make_ctx()).prepare()
    assert result.status is AgentStatus.WAITING, result.message
    assert result.outputs["version"] == "1.4.0"  # feat -> minor
    assert fake_ado.branches["rc/v1.4.0"] == CAND
    pr = fake_ado.prs[result.outputs["pr_id"]]
    assert (pr.source_branch, pr.target_branch, pr.title) == ("rc/v1.4.0", "release", "Release v1.4.0")
    assert pr.work_items == [4521]
    assert fake_ado.last_status(pr.id, QA_CERT_STATUS)["state"] == "succeeded"
    assert fake_ado.auto_complete[pr.id]["strategy"] == "noFastForward"
    assert "QA certification" in fake_ado.comments[(pr.id, COMMENT_MARKER)]["content"]
    assert result.outputs["risk"] == "low" and fake_ado.reviewers == []


def test_prepare_is_idempotent(make_ctx, fake_ado):
    seed(fake_ado)
    a = agent(make_ctx())
    first = a.prepare()
    second = a.prepare()
    assert second.status is AgentStatus.WAITING and second.outputs["pr_id"] == first.outputs["pr_id"]
    assert len(fake_ado.prs) == 1


def test_prepare_blocked_outside_change_window_and_in_freeze(make_ctx, fake_ado, tmp_path):
    seed(fake_ado)
    cfg = make_config(
        tmp_path,
        release={
            "change_window": {
                "timezone": "Asia/Kolkata",
                "days": ["Mon", "Tue"],
                "start": "10:00",
                "end": "17:00",
            },
            "freeze_periods": [{"start": "2026-12-24", "end": "2027-01-02", "reason": "Year-end"}],
        },
    )
    saturday = datetime(2026, 10, 31, 6, 0, tzinfo=timezone.utc)
    assert agent(make_ctx(cfg, now=saturday)).prepare().status is AgentStatus.BLOCKED
    late = datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc)  # Tue 18:30 IST
    assert "outside the change window" in agent(make_ctx(cfg, now=late)).prepare().message
    xmas = datetime(2026, 12, 28, 6, 0, tzinfo=timezone.utc)  # a Monday, but frozen
    assert "Year-end" in agent(make_ctx(cfg, now=xmas)).prepare().message
    assert agent(make_ctx(cfg)).prepare().status is AgentStatus.WAITING  # Tue 11:30 IST
    assert fake_ado.prs


def test_prepare_requires_certification_and_single_release(make_ctx, fake_ado):
    a = agent(make_ctx())
    assert a.prepare().status is AgentStatus.BLOCKED  # nothing certified
    seed(fake_ado)
    assert a.prepare(commit="9" * 40).status is AgentStatus.BLOCKED  # not certified
    fake_ado.prs[1] = PullRequest(1, "Release v1.3.1", "rc/v1.3.1", "release")
    result = a.prepare()
    assert result.status is AgentStatus.BLOCKED and "already in flight" in result.message


def test_prepare_skips_when_nothing_new(make_ctx, fake_ado):
    seed(fake_ado, messages=())
    assert agent(make_ctx()).prepare().status is AgentStatus.SKIPPED


def test_prepare_blocks_when_fresh_qa_validation_fails(make_ctx, fake_ado, tables):
    seed(fake_ado)
    tables["qa.sales.orders"] = pd.DataFrame(
        {"order_id": [1, 1], "customer_id": [1, 1], "order_amount": [1.0, 1.0], "discount_code": [None, None]}
    )
    result = agent(make_ctx()).prepare()
    assert result.status is AgentStatus.BLOCKED and "QA validation failed" in result.message
    assert fake_ado.prs == {}


def test_high_risk_adds_required_reviewers_and_advisor_cannot_lower_risk(make_ctx, fake_ado):
    seed(fake_ado)
    fake_ado.diffs[("release", CAND)] = ["/src/common/io.py"]  # global impact -> high

    class Advisor:
        def release_assessment(self, context):
            assert context["rule_based_risk"] == "high"
            return ReleaseAssessment("low", ["looks harmless"], "**Highlights**\n- dedupe")

        def remediation(self, report):
            return None

        def pr_text(self, context):
            return None

    result = agent(make_ctx(advisor=Advisor())).prepare()
    assert result.outputs["risk"] == "high"
    assert fake_ado.reviewers == [(result.outputs["pr_id"], "release-managers", True)]
    assert "looks harmless" in fake_ado.prs[result.outputs["pr_id"]].description


def test_finalize_tags_release_and_is_idempotent(make_ctx, fake_ado):
    fake_ado.tags.append(GitTag("v1.3.0", "1" * 40))
    fake_ado.prs[5] = PullRequest(
        5,
        "Release v1.4.0",
        "rc/v1.4.0",
        "release",
        status="completed",
        last_merge_commit=MERGE,
        last_merge_source_commit=CAND,
    )
    ctx = make_ctx()
    result = agent(ctx).finalize(MERGE)
    assert result.status is AgentStatus.SUCCEEDED, result.message
    assert GitTag("v1.4.0", MERGE) in fake_ado.tags
    assert result.outputs["back_merge_pr"] is None
    assert agent(ctx).finalize(MERGE).status is AgentStatus.SUCCEEDED
    assert len([t for t in fake_ado.tags if t.name == "v1.4.0"]) == 1


def test_finalize_opens_back_merge_for_hotfix(make_ctx, fake_ado):
    fake_ado.prs[5] = PullRequest(
        5,
        "Release v1.3.1",
        "rc/v1.3.1",
        "release",
        status="completed",
        last_merge_commit=MERGE,
        last_merge_source_commit=CAND,
    )
    fake_ado.commits[(CAND, "main")] = [Commit("7" * 40, "fix: hotfix null keys")]
    result = agent(make_ctx()).finalize(MERGE)
    bm = fake_ado.prs[result.outputs["back_merge_pr"]]
    assert (bm.source_branch, bm.target_branch) == ("release", "main")


def test_finalize_failure_returns_rollback_target(make_ctx, fake_ado, tables):
    fake_ado.tags += [GitTag("v1.3.0", "1" * 40), GitTag("v1.2.0", "2" * 40)]
    del tables["prod.sales.orders"]  # deployment broke the table
    result = agent(make_ctx()).finalize(MERGE)
    assert result.status is AgentStatus.FAILED
    assert result.outputs["rollback_tag"] == "v1.3.0"
    assert all(t.name != "v1.3.1" for t in fake_ado.tags)


def test_rollback_target(make_ctx, fake_ado):
    a = agent(make_ctx())
    assert a.rollback_target().status is AgentStatus.FAILED
    fake_ado.tags += [GitTag("v1.10.0", "a1" * 20), GitTag("v1.9.0", "b2" * 20)]
    assert a.rollback_target().outputs["rollback_tag"] == "v1.10.0"  # semver order, not string order
    assert a.rollback_target(failed_commit="a1" * 20).outputs["rollback_tag"] == "v1.9.0"
