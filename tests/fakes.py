"""In-memory Azure DevOps double with the same methods the agents call."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentic_devops.integrations.azure_devops import Commit, GitTag, PolicyEvaluation, PullRequest
from agentic_devops.models import ValidationReport


@dataclass
class FakeAdo:
    branches: dict[str, str] = field(default_factory=dict)
    commits: dict[tuple[str, str | None], list[Commit]] = field(default_factory=dict)
    diffs: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    prs: dict[int, PullRequest] = field(default_factory=dict)
    pr_files: dict[int, list[str]] = field(default_factory=dict)
    statuses: list[dict[str, Any]] = field(default_factory=list)
    comments: dict[tuple[int, str], dict[str, Any]] = field(default_factory=dict)
    tags: list[GitTag] = field(default_factory=list)
    reviewers: list[tuple[int, str, bool]] = field(default_factory=list)
    policies: dict[int, list[PolicyEvaluation]] = field(default_factory=dict)
    auto_complete: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_pr_id: int = 100

    # refs / tags / commits
    def get_branch_head(self, branch):
        return self.branches.get(branch)

    def create_branch(self, branch, commit_id):
        assert branch not in self.branches
        self.branches[branch] = commit_id

    def list_tags(self, prefix=""):
        return [t for t in self.tags if t.name.startswith(prefix)]

    def create_annotated_tag(self, name, commit_id, message):
        assert all(t.name != name for t in self.tags), f"duplicate tag {name}"
        self.tags.append(GitTag(name, commit_id))

    def list_commits(self, item_version, compare_version=None, top=500):
        return list(self.commits.get((item_version, compare_version), []))

    def diff_files(self, base_version, target_version):
        return list(self.diffs.get((base_version, target_version), []))

    # pull requests
    def find_active_pr(self, source_branch, target_branch):
        for pr in self.prs.values():
            if (
                pr.status == "active"
                and pr.source_branch == source_branch
                and pr.target_branch == target_branch
            ):
                return pr
        return None

    def list_pull_requests(self, target_branch, status="active", top=50):
        return [p for p in self.prs.values() if p.target_branch == target_branch and p.status == status][:top]

    def get_pr(self, pr_id):
        return self.prs[pr_id]

    def create_pr(self, source_branch, target_branch, title, description, work_item_ids=None, is_draft=False):
        pr = PullRequest(
            id=self.next_pr_id,
            title=title,
            description=description,
            source_branch=source_branch,
            target_branch=target_branch,
            merge_status="succeeded",
            is_draft=is_draft,
            last_merge_source_commit=self.branches.get(source_branch),
            web_url=f"https://dev.azure.com/x/p/_git/r/pullrequest/{self.next_pr_id}",
        )
        pr.work_items = list(work_item_ids or [])  # type: ignore[attr-defined]
        self.prs[pr.id] = pr
        self.next_pr_id += 1
        return pr

    def set_auto_complete(self, pr_id, merge_strategy, delete_source_branch, transition_work_items, message):
        self.prs[pr_id].auto_complete_set = True
        self.auto_complete[pr_id] = {
            "strategy": merge_strategy,
            "delete": delete_source_branch,
            "message": message,
        }
        return self.prs[pr_id]

    def latest_iteration_id(self, pr_id):
        return 1

    def get_pr_changed_files(self, pr_id):
        return list(self.pr_files.get(pr_id, []))

    def post_pr_status(self, pr_id, state, name, genre, description, target_url=None, iteration_id=None):
        self.statuses.append(
            {"pr": pr_id, "state": state, "name": name, "genre": genre, "description": description}
        )

    def upsert_pr_comment(self, pr_id, marker, content, active=True):
        self.comments[(pr_id, marker)] = {"content": content, "active": active}
        return 1

    def add_reviewer(self, pr_id, reviewer_id, required=True):
        self.reviewers.append((pr_id, reviewer_id, required))

    def get_policy_evaluations(self, pr_id):
        return list(self.policies.get(pr_id, []))

    # helpers for assertions
    def last_status(self, pr_id, name):
        matches = [s for s in self.statuses if s["pr"] == pr_id and s["name"] == name]
        return matches[-1] if matches else None


class StubExecutor:
    """Returns a canned report (used where the test is about agent flow, not rules)."""

    def __init__(self, report: ValidationReport | None = None, error: Exception | None = None):
        self.report = report
        self.error = error
        self.calls: list[dict[str, Any]] = []

    def execute(self, contracts, reference_contracts, environment, run_id, profile, context):
        self.calls.append(
            {"tables": [c.table for c in contracts], "env": environment.name, "profile": profile}
        )
        if self.error:
            raise self.error
        report = self.report or ValidationReport(run_id, environment.name, environment.catalog, "now")
        report.environment = environment.name
        return report, None
