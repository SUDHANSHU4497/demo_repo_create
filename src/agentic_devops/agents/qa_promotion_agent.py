"""QA Promotion Agent: feature branch -> ``main`` (QA workspace).

promote(): pre-flight checks, open (or reuse) the PR, enable auto-complete. Azure DevOps merges it
           once every branch policy passes (reviewers, build, data-validation status). The agent
           never merges around a policy and its identity has no "bypass policies" permission.
certify(): runs in the CD pipeline after ``databricks bundle deploy -t qa``. Validates the QA
           tables and, if they pass, tags the commit ``qa-certified/<timestamp>-<sha>``. Only
           certified commits can be released to production.
"""

from __future__ import annotations

import logging
import re

from agentic_devops.agents.data_validation_agent import DataValidationAgent, ValidationRequest
from agentic_devops.core.agent import AgentContext, BaseAgent
from agentic_devops.core.policies import extract_work_items, is_conventional_title
from agentic_devops.integrations.azure_devops import short_ref
from agentic_devops.models import AgentResult, AgentStatus
from agentic_devops.validation.contracts import load_contracts, resolve_impacted_contracts

log = logging.getLogger(__name__)

STATUS_NAME = "qa-promotion"
COMMENT_MARKER = "<!-- agentic-devops:qa-promotion -->"


class QaPromotionAgent(BaseAgent):
    name = "qa-promotion-agent"

    def __init__(self, ctx: AgentContext, validation_agent: DataValidationAgent):
        super().__init__(ctx)
        self.validation_agent = validation_agent

    # ------------------------------------------------------------------ promote
    def promote(self, source_branch: str, work_item_ids: list[int] | None = None) -> AgentResult:
        b, q = self.cfg.branches, self.cfg.qa_promotion
        source, target = short_ref(source_branch), b.qa

        if source in (b.qa, b.prod) or not (
            re.match(b.feature_pattern, source) or re.match(b.hotfix_pattern, source)
        ):
            return self.finish(
                "promote",
                AgentStatus.BLOCKED,
                f"'{source}' is not a promotable branch. Use feature/<work-item>-<desc> "
                f"(pattern {b.feature_pattern}).",
            )
        head = self.ado.get_branch_head(source)
        if head is None:
            return self.finish(
                "promote",
                AgentStatus.FAILED,
                f"Branch '{source}' not found in Azure Repos. Commit and push from your Databricks Git folder first.",
            )

        pr = self.ado.find_active_pr(source, target)
        created = False
        if pr is None:
            commits = self.ado.list_commits(source, target)
            if not commits:
                return self.finish(
                    "promote",
                    AgentStatus.SKIPPED,
                    f"'{source}' has no commits that are not already in '{target}'.",
                    commit=head,
                )
            messages = [c.message for c in commits]
            work_items = sorted(set(work_item_ids or []) | extract_work_items(source, messages))
            if q.require_work_item and not work_items:
                return self.finish(
                    "promote",
                    AgentStatus.BLOCKED,
                    "No work item linked. Name the branch feature/<id>-<desc>, mention AB#<id> in a commit, "
                    "or pass --work-item.",
                    commit=head,
                )
            changed = self.ado.diff_files(target, source)
            impacted = resolve_impacted_contracts(
                load_contracts(self.cfg.validation.contracts_dir),
                changed,
                self.cfg.validation.global_impact_paths,
            )
            title, description = self._pr_text(
                source, target, commits, changed, [c.table for c in impacted], work_items
            )
            pr = self.act(
                f"Create PR {source} -> {target}: {title}",
                self.ado.create_pr,
                source,
                target,
                title,
                description,
                work_items,
            )
            if pr is None:  # dry run
                return self.finish(
                    "promote",
                    AgentStatus.WAITING,
                    f"[dry-run] would open PR '{title}'",
                    commit=head,
                    title=title,
                    work_items=work_items,
                )
            created = True

        if pr.is_draft:
            return self.finish(
                "promote",
                AgentStatus.WAITING,
                f"PR !{pr.id} is a draft; publish it to start the gates.",
                pr_id=pr.id,
                pr_url=pr.web_url,
            )

        iteration = self.ado.latest_iteration_id(pr.id)
        if pr.merge_status == "conflicts":
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
                iteration,
            )
            self.act(
                f"Comment on PR !{pr.id}: conflicts",
                self.ado.upsert_pr_comment,
                pr.id,
                COMMENT_MARKER,
                f"{COMMENT_MARKER}\n### 🤖 QA Promotion Agent\n⛔ {msg}",
                True,
            )
            return self.finish("promote", AgentStatus.FAILED, msg, pr_id=pr.id, pr_url=pr.web_url)

        self.act(
            f"Set PR !{pr.id} status {STATUS_NAME}=succeeded",
            self.ado.post_pr_status,
            pr.id,
            "succeeded",
            STATUS_NAME,
            self.genre,
            "Branch, work item and merge checks passed",
            self.ctx.pipeline_run_url,
            iteration,
        )

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

        blockers = [e.name for e in self.ado.get_policy_evaluations(pr.id) if e.is_blocking and not e.passing]
        verb = "Opened" if created else "Updated"
        if blockers:
            return self.finish(
                "promote",
                AgentStatus.WAITING,
                f"{verb} PR !{pr.id}; auto-complete is on. Waiting on: {', '.join(blockers)}.",
                pr_id=pr.id,
                pr_url=pr.web_url,
                created=created,
                blockers=blockers,
            )
        return self.finish(
            "promote",
            AgentStatus.SUCCEEDED,
            f"{verb} PR !{pr.id}; all policies pass, Azure DevOps is completing the merge into '{target}'.",
            pr_id=pr.id,
            pr_url=pr.web_url,
            created=created,
            blockers=[],
        )

    def _pr_text(self, source, target, commits, changed, tables, work_items) -> tuple[str, str]:
        context = {
            "source_branch": source,
            "target_branch": target,
            "commit_subjects": [c.message.splitlines()[0] for c in commits[:100] if c.message],
            "changed_files": changed[:300],
            "governed_tables_affected": tables,
            "work_items": work_items,
        }
        suggestion = self.ctx.advisor.pr_text(context)
        subjects = context["commit_subjects"]
        if suggestion and is_conventional_title(suggestion.title):
            title, body = suggestion.title, suggestion.description
        else:
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
        tables_md = ", ".join(f"`{t}`" for t in tables) if tables else "none (no data contract matched)"
        footer = [
            "",
            "### Data impact",
            f"Governed tables affected: {tables_md}",
            "",
            "### Gates (enforced by branch policies)",
            f"- `{self.genre}/data-validation` — primary keys, fully-null columns, column naming",
            "- Build validation — unit tests, lint, `databricks bundle validate`",
            "- Reviewer approval",
            "",
            "_Opened by the QA Promotion Agent. Auto-complete is enabled: Azure DevOps merges when every "
            "policy passes, then the CD pipeline deploys to the QA workspace._",
        ]
        return title, "\n".join([body, *footer])

    # ------------------------------------------------------------------ certify
    def certify(self, commit: str, source_branch: str | None = None) -> AgentResult:
        b, q = self.cfg.branches, self.cfg.qa_promotion
        if source_branch:
            branch = short_ref(source_branch)
            if branch != b.qa and not re.match(b.hotfix_pattern, branch):
                return self.finish(
                    "certify",
                    AgentStatus.BLOCKED,
                    f"Only '{b.qa}' or hotfix branches can be QA-certified, not '{branch}'.",
                    commit=commit,
                )

        existing = [t for t in self.ado.list_tags(b.qa_certified_tag_prefix) if t.commit_id == commit]
        if existing:
            return self.finish(
                "certify",
                AgentStatus.SUCCEEDED,
                f"Already certified as {existing[0].name}",
                commit=commit,
                environment=q.environment,
                tag=existing[0].name,
            )

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
            return self.finish(
                "certify",
                AgentStatus.FAILED,
                f"QA data validation failed; commit not certified. {result.message}",
                commit=commit,
                environment=q.environment,
                validation=result.outputs.get("summary"),
            )

        tag = f"{b.qa_certified_tag_prefix}{self.ctx.clock():%Y%m%d-%H%M%S}-{commit[:8]}"
        self.act(
            f"Create tag {tag} on {commit[:8]}",
            self.ado.create_annotated_tag,
            tag,
            commit,
            f"QA certified by {self.name} (run {self.ctx.run_id}). {result.message}",
        )
        self.ctx.notifier.notify(
            "QA certified",
            f"Commit {commit[:8]} is deployed to QA and passed validation ({tag}).",
            "success",
            self.ctx.pipeline_run_url,
        )
        return self.finish(
            "certify",
            AgentStatus.SUCCEEDED,
            f"Certified {commit[:8]} as {tag}",
            commit=commit,
            environment=q.environment,
            tag=tag,
            validation=result.outputs.get("summary"),
        )
