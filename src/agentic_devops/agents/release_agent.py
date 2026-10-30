"""Release Agent: QA-certified commit -> ``release`` (production workspace).

prepare()   guardrails (change window, freeze, certification, one release at a time), optional
            fresh QA validation, version from Conventional Commits, ``rc/vX.Y.Z`` branch pinned to
            the certified commit, PR into ``release`` with notes + risk, auto-complete (merge commit).
            Humans approve; the agent can add required reviewers for high risk but never approves.
finalize()  runs after ``databricks bundle deploy -t prod``: post-deploy verification, ``vX.Y.Z``
            tag, back-merge PR for hotfixes. On failure the pipeline's rollback stage redeploys the
            previous tag.
"""

from __future__ import annotations

import logging
import re

from agentic_devops.agents.data_validation_agent import DataValidationAgent, ValidationRequest
from agentic_devops.core.agent import AgentContext, BaseAgent
from agentic_devops.core.policies import (
    assess_release_risk,
    check_change_window,
    escalate_only,
    extract_work_items,
)
from agentic_devops.models import AgentResult, AgentStatus
from agentic_devops.versioning import SemVer, next_version

log = logging.getLogger(__name__)

QA_CERT_STATUS = "qa-certified"
COMMENT_MARKER = "<!-- agentic-devops:release -->"
_VERSION_IN_TEXT = re.compile(r"v?(\d+\.\d+\.\d+)")


class ReleaseAgent(BaseAgent):
    name = "release-agent"

    def __init__(self, ctx: AgentContext, validation_agent: DataValidationAgent):
        super().__init__(ctx)
        self.validation_agent = validation_agent

    def _versions(self) -> list[tuple[SemVer, str, str]]:
        prefix = self.cfg.release.tag_prefix
        out = []
        for t in self.ado.list_tags(prefix):
            v = SemVer.parse(t.name, prefix)
            if v:
                out.append((v, t.name, t.commit_id))
        return sorted(out)

    # ------------------------------------------------------------------ prepare
    def prepare(self, commit: str | None = None, bump: str = "auto") -> AgentResult:
        b, r = self.cfg.branches, self.cfg.release

        allowed, reason = check_change_window(self.ctx.clock(), r.change_window, r.freeze_periods)
        if not allowed:
            return self.finish("prepare", AgentStatus.BLOCKED, f"Release not allowed now: {reason}")

        certified = sorted(self.ado.list_tags(b.qa_certified_tag_prefix), key=lambda t: t.name)
        if commit:
            matches = [t for t in certified if t.commit_id == commit]
            if not matches:
                return self.finish(
                    "prepare",
                    AgentStatus.BLOCKED,
                    f"Commit {commit[:8]} is not QA-certified; deploy it to QA and run certify-qa first.",
                    commit=commit,
                )
            cert = matches[-1]
        else:
            if not certified:
                return self.finish("prepare", AgentStatus.BLOCKED, "No QA-certified commit found.")
            cert = certified[-1]
        candidate = cert.commit_id

        release_head = self.ado.get_branch_head(b.prod)
        commits = self.ado.list_commits(candidate, b.prod if release_head else None)
        if not commits:
            return self.finish(
                "prepare",
                AgentStatus.SKIPPED,
                f"Nothing to release: '{b.prod}' already contains {candidate[:8]}.",
                commit=candidate,
            )

        versions = self._versions()
        current = versions[-1][0] if versions else None
        version = next_version(current, [c.message for c in commits], bump, r.initial_version)
        tag_name = f"{r.tag_prefix}{version}"
        rc_branch = f"{b.release_candidate_prefix}{tag_name}"
        if any(v == version for v, _, _ in versions):
            return self.finish(
                "prepare", AgentStatus.BLOCKED, f"Tag {tag_name} already exists.", commit=candidate
            )

        existing_pr = None
        for pr in self.ado.list_pull_requests(b.prod, "active"):
            if not pr.source_branch.startswith(b.release_candidate_prefix):
                continue
            if pr.source_branch == rc_branch:
                existing_pr = pr
            else:
                return self.finish(
                    "prepare",
                    AgentStatus.BLOCKED,
                    f"Release PR !{pr.id} ({pr.source_branch}) is already in flight. Complete or abandon it first.",
                    pr_id=pr.id,
                    commit=candidate,
                )

        validation_summary = None
        if r.revalidate_in_qa:
            vr = self.validation_agent.run(
                ValidationRequest(
                    environment=r.qa_environment, validate_all=True, commit=candidate, post_to_pr=False
                )
            )
            if vr.status is AgentStatus.FAILED:
                return self.finish(
                    "prepare",
                    AgentStatus.BLOCKED,
                    f"Fresh QA validation failed, release stopped: {vr.message}",
                    commit=candidate,
                )
            validation_summary = vr.outputs.get("summary")

        rc_head = self.ado.get_branch_head(rc_branch)
        if rc_head is None:
            self.act(
                f"Create branch {rc_branch} at {candidate[:8]}", self.ado.create_branch, rc_branch, candidate
            )
        elif rc_head != candidate:
            return self.finish(
                "prepare",
                AgentStatus.BLOCKED,
                f"Branch {rc_branch} exists at {rc_head[:8]}, not {candidate[:8]}. Delete it and retry.",
                commit=candidate,
            )

        changed = self.ado.diff_files(b.prod, candidate) if release_head else []
        work_items = sorted(extract_work_items("", [c.message for c in commits]))
        warnings = int((validation_summary or {}).get("warnings", 0))
        baseline, factors = assess_release_risk(
            changed,
            self.cfg.validation.contracts_dir,
            self.cfg.validation.global_impact_paths,
            len(commits),
            warnings,
        )
        assessment = self.ctx.advisor.release_assessment(
            {
                "version": tag_name,
                "commit_subjects": [c.message.splitlines()[0] for c in commits[:150] if c.message],
                "changed_files": changed[:300],
                "rule_based_risk": baseline,
                "rule_based_factors": factors,
                "qa_validation": validation_summary,
            }
        )
        risk = escalate_only(baseline, assessment.risk_level if assessment else None)

        description = self._description(
            tag_name, cert.name, candidate, commits, work_items, risk, factors, assessment, validation_summary
        )
        pr = existing_pr or self.act(
            f"Create release PR {rc_branch} -> {b.prod}",
            self.ado.create_pr,
            rc_branch,
            b.prod,
            f"Release {tag_name}",
            description,
            work_items,
        )
        if pr is None:  # dry run
            return self.finish(
                "prepare",
                AgentStatus.WAITING,
                f"[dry-run] would open release PR for {tag_name}",
                commit=candidate,
                version=str(version),
                risk=risk,
            )

        iteration = self.ado.latest_iteration_id(pr.id)
        self.act(
            f"Set PR !{pr.id} status {QA_CERT_STATUS}=succeeded",
            self.ado.post_pr_status,
            pr.id,
            "succeeded",
            QA_CERT_STATUS,
            self.genre,
            f"{candidate[:8]} certified by {cert.name}",
            self.ctx.pipeline_run_url,
            iteration,
        )
        self.act(
            f"Upsert release evidence on PR !{pr.id}",
            self.ado.upsert_pr_comment,
            pr.id,
            COMMENT_MARKER,
            self._evidence(tag_name, cert.name, candidate, risk, factors, assessment, validation_summary),
            False,
        )
        if risk == "high":
            for reviewer in r.high_risk_reviewer_ids:
                self.act(
                    f"Add required reviewer {reviewer} (high risk)",
                    self.ado.add_reviewer,
                    pr.id,
                    reviewer,
                    True,
                )
        if not pr.auto_complete_set:
            self.act(
                f"Enable auto-complete ({r.merge_strategy}) on PR !{pr.id}",
                self.ado.set_auto_complete,
                pr.id,
                r.merge_strategy,
                True,
                False,
                f"Release {tag_name}",
            )
        self.ctx.notifier.notify(
            f"Release {tag_name} awaiting approval",
            f"Risk: {risk.upper()}. {len(commits)} commit(s), {len(work_items)} work item(s). "
            "Approve the PR to deploy to production.",
            "warning" if risk == "high" else "info",
            pr.web_url,
        )
        return self.finish(
            "prepare",
            AgentStatus.WAITING,
            f"Release PR !{pr.id} for {tag_name} is waiting for approval (risk {risk}).",
            pr_id=pr.id,
            pr_url=pr.web_url,
            commit=candidate,
            version=str(version),
            risk=risk,
            risk_factors=factors,
            rc_branch=rc_branch,
        )

    def _description(
        self, tag, cert_tag, candidate, commits, work_items, risk, factors, assessment, validation
    ) -> str:
        lines = [
            f"Release **{tag}** to production. Candidate commit `{candidate[:10]}` (certified by `{cert_tag}`).",
            "",
            f"**Risk: {risk.upper()}**",
            *[f"- {f}" for f in factors],
        ]
        if assessment and assessment.risk_reasons:
            lines += ["", "_Advisor (AI-generated):_", *[f"- {x}" for x in assessment.risk_reasons]]
        if assessment and assessment.notes_markdown:
            lines += ["", assessment.notes_markdown]
        lines += ["", f"### Commits ({len(commits)})"]
        lines += [f"- {c.message.splitlines()[0]}" for c in commits[:40] if c.message]
        if len(commits) > 40:
            lines.append(f"- …and {len(commits) - 40} more")
        if validation:
            lines += [
                "",
                f"QA validation: {validation.get('errors', 0)} error(s), {validation.get('warnings', 0)} "
                f"warning(s) across {validation.get('tables', 0)} table(s).",
            ]
        lines += [
            "",
            "_Opened by the Release Agent. Merges automatically after the required approvals; "
            "production deployment then waits on the `databricks-prod` environment approval._",
        ]
        return "\n".join(lines)

    def _evidence(self, tag, cert_tag, candidate, risk, factors, assessment, validation) -> str:
        rows = [
            COMMENT_MARKER,
            f"## 🤖 Release Agent — {tag}",
            "",
            "| Check | Result |",
            "|---|---|",
            f"| QA certification | ✅ `{cert_tag}` → `{candidate[:10]}` |",
            f"| Change window | ✅ open at {self.ctx.clock():%Y-%m-%d %H:%M} UTC |",
        ]
        if validation:
            rows.append(
                f"| Fresh QA validation | ✅ {validation.get('errors', 0)} errors / "
                f"{validation.get('warnings', 0)} warnings |"
            )
        rows.append(f"| Risk (rule-based floor{', advisor' if assessment else ''}) | **{risk.upper()}** |")
        rows += ["", "**Risk factors**", *[f"- {f}" for f in factors]]
        return "\n".join(rows)

    # ------------------------------------------------------------------ finalize
    def finalize(self, commit: str) -> AgentResult:
        b, r = self.cfg.branches, self.cfg.release
        completed = self.ado.list_pull_requests(b.prod, "completed", top=30)
        pr = next((p for p in completed if p.last_merge_commit == commit), None)
        versions = self._versions()

        version = None
        if pr:
            m = _VERSION_IN_TEXT.search(pr.source_branch) or _VERSION_IN_TEXT.search(pr.title)
            version = SemVer.parse(m.group(1), "") if m else None
        if version is None:  # e.g. a hotfix PR merged straight into release
            version = next_version(
                versions[-1][0] if versions else None, ["fix: hotfix"], "patch", r.initial_version
            )
        tag_name = f"{r.tag_prefix}{version}"

        for v, name, sha in versions:
            if v == version:
                if sha == commit:
                    return self.finish(
                        "finalize",
                        AgentStatus.SUCCEEDED,
                        f"{name} already tagged on {commit[:8]}",
                        commit=commit,
                        environment=r.environment,
                        version=str(version),
                        tag=name,
                    )
                return self.finish(
                    "finalize",
                    AgentStatus.FAILED,
                    f"Tag {name} already points at {sha[:8]}, not {commit[:8]}.",
                    commit=commit,
                    environment=r.environment,
                )

        verify = self.validation_agent.run(
            ValidationRequest(
                environment=r.environment,
                validate_all=True,
                profile=r.verify_profile,
                commit=commit,
                post_to_pr=False,
            )
        )
        if verify.status is AgentStatus.FAILED:
            rollback = versions[-1][1] if versions else None
            self.ctx.notifier.notify(
                f"Production verification failed for {tag_name}",
                f"{verify.message}. Rolling back to {rollback or '(no previous release)'}.",
                "error",
                self.ctx.pipeline_run_url,
            )
            return self.finish(
                "finalize",
                AgentStatus.FAILED,
                f"Post-deploy verification failed: {verify.message}",
                commit=commit,
                environment=r.environment,
                version=str(version),
                rollback_tag=rollback,
            )

        self.act(
            f"Create tag {tag_name} on {commit[:8]}",
            self.ado.create_annotated_tag,
            tag_name,
            commit,
            f"Production release {tag_name} (run {self.ctx.run_id}).",
        )

        back_merge = None
        if pr and pr.last_merge_source_commit:
            missing_in_qa = self.ado.list_commits(pr.last_merge_source_commit, b.qa)
            if missing_in_qa:  # hotfix commits that main does not have yet
                back_merge = self.ado.find_active_pr(b.prod, b.qa)
                if back_merge is None:
                    back_merge = self.act(
                        f"Open back-merge PR {b.prod} -> {b.qa}",
                        self.ado.create_pr,
                        b.prod,
                        b.qa,
                        f"chore: back-merge {tag_name} into {b.qa}",
                        f"{tag_name} contains {len(missing_in_qa)} commit(s) not yet in `{b.qa}` (hotfix). "
                        "Merge them back so the next release does not regress.",
                    )

        self.ctx.notifier.notify(
            f"Released {tag_name} to production",
            f"Commit {commit[:8]} verified and tagged.",
            "success",
            self.ctx.pipeline_run_url,
        )
        return self.finish(
            "finalize",
            AgentStatus.SUCCEEDED,
            f"Released {tag_name}",
            commit=commit,
            environment=r.environment,
            version=str(version),
            tag=tag_name,
            back_merge_pr=getattr(back_merge, "id", None),
        )

    # ------------------------------------------------------------------ rollback
    def rollback_target(self, failed_commit: str | None = None) -> AgentResult:
        candidates = [(v, name, sha) for v, name, sha in self._versions() if sha != failed_commit]
        if not candidates:
            return self.finish(
                "rollback-target", AgentStatus.FAILED, "No previous production release tag found."
            )
        _, name, sha = candidates[-1]
        return self.finish(
            "rollback-target",
            AgentStatus.SUCCEEDED,
            f"Roll back to {name} ({sha[:8]})",
            rollback_tag=name,
            rollback_commit=sha,
        )
