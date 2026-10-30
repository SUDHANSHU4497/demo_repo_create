"""QA Promotion Agent: feature branch -> ``main`` (QA workspace). Two LangGraph flows.

promote:  check_branch -> check_pushed -> find_pr --(no PR)--> check_commits -> analyze_impact
                                              |                -> draft_pr (LLM) -> create_pr --+
                                              +--(existing PR)----------------------------------+
          -> check_draft -> check_conflicts -> post_status -> enable_auto_complete -> report_blockers

          Azure DevOps merges once every branch policy passes (reviewers, build, data-validation).
          The agent never merges around a policy and its identity has no "bypass policies" permission.

certify:  check_source_branch -> check_existing_tag -> validate_qa -> tag_commit
          Runs in the CD pipeline after ``databricks bundle deploy -t qa``; only certified commits
          can be released to production.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict

from agentic_devops.agents.data_validation_agent import DataValidationAgent, ValidationRequest
from agentic_devops.core.agent import AgentContext, BaseAgent
from agentic_devops.core.flow import Step, build_flow, run_flow
from agentic_devops.core.policies import extract_work_items, is_conventional_title
from agentic_devops.integrations.azure_devops import short_ref
from agentic_devops.models import AgentResult, AgentStatus
from agentic_devops.validation.contracts import load_contracts, resolve_impacted_contracts

log = logging.getLogger(__name__)

STATUS_NAME = "qa-promotion"
COMMENT_MARKER = "<!-- agentic-devops:qa-promotion -->"


class PromoteState(TypedDict, total=False):
    source: str
    target: str
    work_item_ids: list[int]
    head: str
    pr: Any
    created: bool
    commits: list[Any]
    work_items: list[int]
    changed: list[str]
    tables: list[str]
    title: str
    description: str
    iteration: int | None
    result: AgentResult


class CertifyState(TypedDict, total=False):
    commit: str
    source_branch: str | None
    validation: AgentResult
    result: AgentResult


class QaPromotionAgent(BaseAgent):
    name = "qa-promotion-agent"

    def __init__(self, ctx: AgentContext, validation_agent: DataValidationAgent):
        super().__init__(ctx)
        self.validation_agent = validation_agent
        self.promote_graph = build_flow(
            PromoteState,
            [
                Step("check_branch", self._check_branch),
                Step("check_pushed", self._check_pushed),
                Step(
                    "find_pr",
                    self._find_pr,
                    route=self._after_find_pr,
                    targets=("check_commits", "check_draft"),
                ),
                Step("check_commits", self._check_commits),
                Step("analyze_impact", self._analyze_impact),
                Step("draft_pr", self._draft_pr),
                Step("create_pr", self._create_pr),
                Step("check_draft", self._check_draft),
                Step("check_conflicts", self._check_conflicts),
                Step("post_status", self._post_status),
                Step("enable_auto_complete", self._enable_auto_complete),
                Step("report_blockers", self._report_blockers),
            ],
            name="qa-promotion",
        )
        self.certify_graph = build_flow(
            CertifyState,
            [
                Step("check_source_branch", self._check_source_branch),
                Step("check_existing_tag", self._check_existing_tag),
                Step("validate_qa", self._validate_qa),
                Step("tag_commit", self._tag_commit),
            ],
            name="qa-certification",
        )

    # ------------------------------------------------------------------ public API
    def promote(self, source_branch: str, work_item_ids: list[int] | None = None) -> AgentResult:
        return run_flow(
            self.promote_graph,
            {
                "source": short_ref(source_branch),
                "target": self.cfg.branches.qa,
                "work_item_ids": work_item_ids or [],
            },
        )

    def certify(self, commit: str, source_branch: str | None = None) -> AgentResult:
        return run_flow(self.certify_graph, {"commit": commit, "source_branch": source_branch})

    # ------------------------------------------------------------------ promote nodes
    def _end(self, state: PromoteState, status: AgentStatus, message: str, **outputs: Any) -> dict:
        pr = state.get("pr")
        if pr is not None:
            outputs.setdefault("pr_url", pr.web_url)
        return {
            "result": self.finish(
                "promote", status, message, pr_id=pr.id if pr else None, commit=state.get("head"), **outputs
            )
        }

    def _check_branch(self, state: PromoteState) -> dict:
        b, source = self.cfg.branches, state["source"]
        if source in (b.qa, b.prod) or not (
            re.match(b.feature_pattern, source) or re.match(b.hotfix_pattern, source)
        ):
            return self._end(
                state,
                AgentStatus.BLOCKED,
                f"'{source}' is not a promotable branch. Use feature/<work-item>-<desc> "
                f"(pattern {b.feature_pattern}).",
            )
        return {}

    def _check_pushed(self, state: PromoteState) -> dict:
        head = self.ado.get_branch_head(state["source"])
        if head is None:
            return self._end(
                state,
                AgentStatus.FAILED,
                f"Branch '{state['source']}' not found in Azure Repos. Commit and push from your "
                "Databricks Git folder first.",
            )
        return {"head": head}

    def _find_pr(self, state: PromoteState) -> dict:
        return {"pr": self.ado.find_active_pr(state["source"], state["target"]), "created": False}

    @staticmethod
    def _after_find_pr(state: PromoteState) -> str:
        return "check_draft" if state.get("pr") is not None else "check_commits"

    def _check_commits(self, state: PromoteState) -> dict:
        commits = self.ado.list_commits(state["source"], state["target"])
        if not commits:
            return self._end(
                state,
                AgentStatus.SKIPPED,
                f"'{state['source']}' has no commits that are not already in '{state['target']}'.",
            )
        work_items = sorted(
            set(state.get("work_item_ids") or [])
            | extract_work_items(state["source"], [c.message for c in commits])
        )
        if self.cfg.qa_promotion.require_work_item and not work_items:
            return self._end(
                state,
                AgentStatus.BLOCKED,
                "No work item linked. Name the branch feature/<id>-<desc>, mention AB#<id> in a commit, "
                "or pass --work-item.",
            )
        return {"commits": commits, "work_items": work_items}

    def _analyze_impact(self, state: PromoteState) -> dict:
        changed = self.ado.diff_files(state["target"], state["source"])
        impacted = resolve_impacted_contracts(
            load_contracts(self.cfg.validation.contracts_dir),
            changed,
            self.cfg.validation.global_impact_paths,
        )
        return {"changed": changed, "tables": [c.table for c in impacted]}

    def _draft_pr(self, state: PromoteState) -> dict:
        source, target = state["source"], state["target"]
        subjects = [c.message.splitlines()[0] for c in state["commits"][:100] if c.message]
        suggestion = self.ctx.advisor.pr_text(
            {
                "source_branch": source,
                "target_branch": target,
                "commit_subjects": subjects,
                "changed_files": state["changed"][:300],
                "governed_tables_affected": state["tables"],
                "work_items": state["work_items"],
            }
        )
        if suggestion and is_conventional_title(suggestion.title):
            title, body = suggestion.title, suggestion.description
        else:  # template fallback (LLM disabled, failed, or produced a non-conforming title)
            first = subjects[0] if subjects else source
            title = (
                first
                if is_conventional_title(first)
                else f"feat: {source.split('/', 1)[-1].replace('-', ' ')}"
            )
            body = "\n".join(
                [f"Promote `{source}` to `{target}` (QA).", "", "### Changes"]
                + [f"- {s}" for s in subjects[:20]]
                + ([f"- …and {len(subjects) - 20} more commits"] if len(subjects) > 20 else [])
            )
        tables_md = ", ".join(f"`{t}`" for t in state["tables"]) or "none (no data contract matched)"
        footer = [
            "",
            "### Data impact",
            f"Governed tables affected: {tables_md}",
            "",
            "### Gates (enforced by branch policies)",
            f"- `{self.genre}/data-validation` — primary keys, fully-null columns, column naming",
            "- Build validation — unit tests, lint",
            "- Reviewer approval",
            "",
            "_Opened by the QA Promotion Agent. Auto-complete is enabled: Azure DevOps merges when every "
            "policy passes, then the CD pipeline deploys to the QA workspace._",
        ]
        return {"title": title, "description": "\n".join([body, *footer])}

    def _create_pr(self, state: PromoteState) -> dict:
        source, target, title = state["source"], state["target"], state["title"]
        pr = self.act(
            f"Create PR {source} -> {target}: {title}",
            self.ado.create_pr,
            source,
            target,
            title,
            state["description"],
            state["work_items"],
        )
        if pr is None:  # dry run
            return self._end(
                state,
                AgentStatus.WAITING,
                f"[dry-run] would open PR '{title}'",
                title=title,
                work_items=state["work_items"],
            )
        return {"pr": pr, "created": True}

    def _check_draft(self, state: PromoteState) -> dict:
        pr = state["pr"]
        if pr.is_draft:
            return self._end(
                state, AgentStatus.WAITING, f"PR !{pr.id} is a draft; publish it to start the gates."
            )
        return {"iteration": self.ado.latest_iteration_id(pr.id)}

    def _check_conflicts(self, state: PromoteState) -> dict:
        pr, source, target = state["pr"], state["source"], state["target"]
        if pr.merge_status != "conflicts":
            return {}
        msg = (
            f"PR !{pr.id} has merge conflicts with '{target}'. In your Databricks Git folder: pull "
            f"'{target}', merge it into '{source}', resolve, commit and push."
        )
        self.act(
            f"Set PR !{pr.id} status {STATUS_NAME}=failed",
            self.ado.post_pr_status,
            pr.id,
            "failed",
            STATUS_NAME,
            self.genre,
            "Merge conflicts with target branch",
            self.ctx.pipeline_run_url,
            state.get("iteration"),
        )
        self.act(
            f"Comment on PR !{pr.id}: conflicts",
            self.ado.upsert_pr_comment,
            pr.id,
            COMMENT_MARKER,
            f"{COMMENT_MARKER}\n### 🤖 QA Promotion Agent\n⛔ {msg}",
            True,
        )
        return self._end(state, AgentStatus.FAILED, msg)

    def _post_status(self, state: PromoteState) -> dict:
        pr = state["pr"]
        self.act(
            f"Set PR !{pr.id} status {STATUS_NAME}=succeeded",
            self.ado.post_pr_status,
            pr.id,
            "succeeded",
            STATUS_NAME,
            self.genre,
            "Branch, work item and merge checks passed",
            self.ctx.pipeline_run_url,
            state.get("iteration"),
        )
        return {}

    def _enable_auto_complete(self, state: PromoteState) -> dict:
        q, pr = self.cfg.qa_promotion, state["pr"]
        if q.auto_complete and not pr.auto_complete_set:
            self.act(
                f"Enable auto-complete ({q.merge_strategy}) on PR !{pr.id}",
                self.ado.set_auto_complete,
                pr.id,
                q.merge_strategy,
                q.delete_source_branch,
                q.transition_work_items,
                f"Merged PR {pr.id}: {pr.title}",
            )
        return {}

    def _report_blockers(self, state: PromoteState) -> dict:
        pr, created = state["pr"], state.get("created", False)
        blockers = [e.name for e in self.ado.get_policy_evaluations(pr.id) if e.is_blocking and not e.passing]
        verb = "Opened" if created else "Updated"
        if blockers:
            return self._end(
                state,
                AgentStatus.WAITING,
                f"{verb} PR !{pr.id}; auto-complete is on. Waiting on: {', '.join(blockers)}.",
                created=created,
                blockers=blockers,
            )
        return self._end(
            state,
            AgentStatus.SUCCEEDED,
            f"{verb} PR !{pr.id}; all policies pass, Azure DevOps is completing the merge into "
            f"'{state['target']}'.",
            created=created,
            blockers=[],
        )

    # ------------------------------------------------------------------ certify nodes
    def _cert_end(self, state: CertifyState, status: AgentStatus, message: str, **outputs: Any) -> dict:
        return {
            "result": self.finish(
                "certify",
                status,
                message,
                commit=state["commit"],
                environment=self.cfg.qa_promotion.environment,
                **outputs,
            )
        }

    def _check_source_branch(self, state: CertifyState) -> dict:
        b = self.cfg.branches
        if state.get("source_branch"):
            branch = short_ref(state["source_branch"])
            if branch != b.qa and not re.match(b.hotfix_pattern, branch):
                return self._cert_end(
                    state,
                    AgentStatus.BLOCKED,
                    f"Only '{b.qa}' or hotfix branches can be QA-certified, not '{branch}'.",
                )
        return {}

    def _check_existing_tag(self, state: CertifyState) -> dict:
        prefix = self.cfg.branches.qa_certified_tag_prefix
        existing = [t for t in self.ado.list_tags(prefix) if t.commit_id == state["commit"]]
        if existing:
            return self._cert_end(
                state, AgentStatus.SUCCEEDED, f"Already certified as {existing[0].name}", tag=existing[0].name
            )
        return {}

    def _validate_qa(self, state: CertifyState) -> dict:
        q, commit = self.cfg.qa_promotion, state["commit"]
        result = self.validation_agent.run(
            ValidationRequest(
                environment=q.environment,
                validate_all=True,
                profile=q.certify_profile,
                commit=commit,
                post_to_pr=False,
            )
        )
        if result.status is AgentStatus.FAILED:
            self.ctx.notifier.notify(
                "QA certification failed",
                f"Commit {commit[:8]}: {result.message}",
                "error",
                self.ctx.pipeline_run_url,
            )
            return self._cert_end(
                state,
                AgentStatus.FAILED,
                f"QA data validation failed; commit not certified. {result.message}",
                validation=result.outputs.get("summary"),
            )
        return {"validation": result}

    def _tag_commit(self, state: CertifyState) -> dict:
        commit, validation = state["commit"], state["validation"]
        tag = f"{self.cfg.branches.qa_certified_tag_prefix}{self.ctx.clock():%Y%m%d-%H%M%S}-{commit[:8]}"
        self.act(
            f"Create tag {tag} on {commit[:8]}",
            self.ado.create_annotated_tag,
            tag,
            commit,
            f"QA certified by {self.name} (run {self.ctx.run_id}). {validation.message}",
        )
        self.ctx.notifier.notify(
            "QA certified",
            f"Commit {commit[:8]} is deployed to QA and passed validation ({tag}).",
            "success",
            self.ctx.pipeline_run_url,
        )
        return self._cert_end(
            state,
            AgentStatus.SUCCEEDED,
            f"Certified {commit[:8]} as {tag}",
            tag=tag,
            validation=validation.outputs.get("summary"),
        )
