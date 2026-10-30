"""Release Agent: QA-certified commit -> ``release`` (production workspace). LangGraph flows.

prepare:  check_change_window -> select_candidate -> collect_changes -> compute_version
          -> check_in_flight -> revalidate_qa -> ensure_rc_branch -> assess_risk (rules + LLM)
          -> open_release_pr -> publish_evidence -> escalate_reviewers -> enable_auto_complete
          -> notify_approvers                                   (ends WAITING for human approval)

finalize: identify_release -> check_existing_tag -> verify_production -> tag_release
          -> open_back_merge -> announce                        (runs after bundle deploy -t prod)

Humans approve; the agent can add required reviewers for high risk but never approves. On a
failed verification the pipeline's rollback stage redeploys ``rollback_target()``.
"""

from __future__ import annotations

import logging
import re
from typing import Any, TypedDict

from agentic_devops.agents.data_validation_agent import DataValidationAgent, ValidationRequest
from agentic_devops.core.agent import AgentContext, BaseAgent
from agentic_devops.core.flow import Step, build_flow, run_flow
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


class PrepareState(TypedDict, total=False):
    commit: str | None
    bump: str
    cert: Any
    candidate: str
    release_head: str | None
    commits: list[Any]
    versions: list[tuple[SemVer, str, str]]
    version: SemVer
    tag_name: str
    rc_branch: str
    existing_pr: Any
    validation_summary: dict[str, Any] | None
    changed: list[str]
    work_items: list[int]
    risk: str
    factors: list[str]
    assessment: Any
    pr: Any
    result: AgentResult


class FinalizeState(TypedDict, total=False):
    commit: str
    pr: Any
    versions: list[tuple[SemVer, str, str]]
    version: SemVer
    tag_name: str
    back_merge: Any
    result: AgentResult


class ReleaseAgent(BaseAgent):
    name = "release-agent"

    def __init__(self, ctx: AgentContext, validation_agent: DataValidationAgent):
        super().__init__(ctx)
        self.validation_agent = validation_agent
        self.prepare_graph = build_flow(
            PrepareState,
            [
                Step("check_change_window", self._check_change_window),
                Step("select_candidate", self._select_candidate),
                Step("collect_changes", self._collect_changes),
                Step("compute_version", self._compute_version),
                Step("check_in_flight", self._check_in_flight),
                Step("revalidate_qa", self._revalidate_qa),
                Step("ensure_rc_branch", self._ensure_rc_branch),
                Step("assess_risk", self._assess_risk),
                Step("open_release_pr", self._open_release_pr),
                Step("publish_evidence", self._publish_evidence),
                Step("escalate_reviewers", self._escalate_reviewers),
                Step("enable_auto_complete", self._enable_auto_complete),
                Step("notify_approvers", self._notify_approvers),
            ],
            name="release-preparation",
        )
        self.finalize_graph = build_flow(
            FinalizeState,
            [
                Step("identify_release", self._identify_release),
                Step("check_existing_tag", self._check_existing_tag),
                Step("verify_production", self._verify_production),
                Step("tag_release", self._tag_release),
                Step("open_back_merge", self._open_back_merge),
                Step("announce", self._announce),
            ],
            name="release-finalization",
        )

    # ------------------------------------------------------------------ public API
    def prepare(self, commit: str | None = None, bump: str = "auto") -> AgentResult:
        return run_flow(self.prepare_graph, {"commit": commit, "bump": bump})

    def finalize(self, commit: str) -> AgentResult:
        return run_flow(self.finalize_graph, {"commit": commit})

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

    def _versions(self) -> list[tuple[SemVer, str, str]]:
        prefix = self.cfg.release.tag_prefix
        out = []
        for t in self.ado.list_tags(prefix):
            v = SemVer.parse(t.name, prefix)
            if v:
                out.append((v, t.name, t.commit_id))
        return sorted(out)

    # ------------------------------------------------------------------ prepare nodes
    def _end(self, state: PrepareState, status: AgentStatus, message: str, **outputs: Any) -> dict:
        pr = state.get("pr") or state.get("existing_pr")
        return {
            "result": self.finish(
                "prepare",
                status,
                message,
                pr_id=pr.id if pr else None,
                commit=state.get("candidate") or state.get("commit"),
                **outputs,
            )
        }

    def _check_change_window(self, state: PrepareState) -> dict:
        r = self.cfg.release
        allowed, reason = check_change_window(self.ctx.clock(), r.change_window, r.freeze_periods)
        return {} if allowed else self._end(state, AgentStatus.BLOCKED, f"Release not allowed now: {reason}")

    def _select_candidate(self, state: PrepareState) -> dict:
        certified = sorted(
            self.ado.list_tags(self.cfg.branches.qa_certified_tag_prefix), key=lambda t: t.name
        )
        commit = state.get("commit")
        if commit:
            matches = [t for t in certified if t.commit_id == commit]
            if not matches:
                return self._end(
                    state,
                    AgentStatus.BLOCKED,
                    f"Commit {commit[:8]} is not QA-certified; deploy it to QA and run certify-qa first.",
                )
            cert = matches[-1]
        elif certified:
            cert = certified[-1]
        else:
            return self._end(state, AgentStatus.BLOCKED, "No QA-certified commit found.")
        return {"cert": cert, "candidate": cert.commit_id}

    def _collect_changes(self, state: PrepareState) -> dict:
        prod, candidate = self.cfg.branches.prod, state["candidate"]
        release_head = self.ado.get_branch_head(prod)
        commits = self.ado.list_commits(candidate, prod if release_head else None)
        if not commits:
            return self._end(
                state, AgentStatus.SKIPPED, f"Nothing to release: '{prod}' already contains {candidate[:8]}."
            )
        return {"release_head": release_head, "commits": commits}

    def _compute_version(self, state: PrepareState) -> dict:
        r, b = self.cfg.release, self.cfg.branches
        versions = self._versions()
        current = versions[-1][0] if versions else None
        version = next_version(
            current, [c.message for c in state["commits"]], state["bump"], r.initial_version
        )
        tag_name = f"{r.tag_prefix}{version}"
        if any(v == version for v, _, _ in versions):
            return self._end(state, AgentStatus.BLOCKED, f"Tag {tag_name} already exists.")
        return {
            "versions": versions,
            "version": version,
            "tag_name": tag_name,
            "rc_branch": f"{b.release_candidate_prefix}{tag_name}",
        }

    def _check_in_flight(self, state: PrepareState) -> dict:
        b, existing = self.cfg.branches, None
        for pr in self.ado.list_pull_requests(b.prod, "active"):
            if not pr.source_branch.startswith(b.release_candidate_prefix):
                continue
            if pr.source_branch == state["rc_branch"]:
                existing = pr  # re-run: reconcile the same release
            else:
                return self._end(
                    {**state, "existing_pr": pr},
                    AgentStatus.BLOCKED,
                    f"Release PR !{pr.id} ({pr.source_branch}) is already in flight. "
                    "Complete or abandon it first.",
                )
        return {"existing_pr": existing}

    def _revalidate_qa(self, state: PrepareState) -> dict:
        r = self.cfg.release
        if not r.revalidate_in_qa:
            return {"validation_summary": None}
        vr = self.validation_agent.run(
            ValidationRequest(
                environment=r.qa_environment, validate_all=True, commit=state["candidate"], post_to_pr=False
            )
        )
        if vr.status is AgentStatus.FAILED:
            return self._end(
                state, AgentStatus.BLOCKED, f"Fresh QA validation failed, release stopped: {vr.message}"
            )
        return {"validation_summary": vr.outputs.get("summary")}

    def _ensure_rc_branch(self, state: PrepareState) -> dict:
        rc_branch, candidate = state["rc_branch"], state["candidate"]
        rc_head = self.ado.get_branch_head(rc_branch)
        if rc_head is None:
            self.act(
                f"Create branch {rc_branch} at {candidate[:8]}", self.ado.create_branch, rc_branch, candidate
            )
        elif rc_head != candidate:
            return self._end(
                state,
                AgentStatus.BLOCKED,
                f"Branch {rc_branch} exists at {rc_head[:8]}, not {candidate[:8]}. Delete it and retry.",
            )
        return {}

    def _assess_risk(self, state: PrepareState) -> dict:
        v = self.cfg.validation
        commits = state["commits"]
        changed = (
            self.ado.diff_files(self.cfg.branches.prod, state["candidate"])
            if state.get("release_head")
            else []
        )
        summary = state.get("validation_summary") or {}
        baseline, factors = assess_release_risk(
            changed, v.contracts_dir, v.global_impact_paths, len(commits), int(summary.get("warnings", 0))
        )
        assessment = self.ctx.advisor.release_assessment(
            {
                "version": state["tag_name"],
                "commit_subjects": [c.message.splitlines()[0] for c in commits[:150] if c.message],
                "changed_files": changed[:300],
                "rule_based_risk": baseline,
                "rule_based_factors": factors,
                "qa_validation": state.get("validation_summary"),
            }
        )
        return {
            "changed": changed,
            "work_items": sorted(extract_work_items("", [c.message for c in commits])),
            "risk": escalate_only(
                baseline, assessment.risk_level if assessment else None
            ),  # LLM can only raise
            "factors": factors,
            "assessment": assessment,
        }

    def _open_release_pr(self, state: PrepareState) -> dict:
        if state.get("existing_pr") is not None:
            return {"pr": state["existing_pr"]}
        tag_name, rc_branch = state["tag_name"], state["rc_branch"]
        pr = self.act(
            f"Create release PR {rc_branch} -> {self.cfg.branches.prod}",
            self.ado.create_pr,
            rc_branch,
            self.cfg.branches.prod,
            f"Release {tag_name}",
            self._description(state),
            state["work_items"],
        )
        if pr is None:  # dry run
            return self._end(
                state,
                AgentStatus.WAITING,
                f"[dry-run] would open release PR for {tag_name}",
                version=str(state["version"]),
                risk=state["risk"],
            )
        return {"pr": pr}

    def _publish_evidence(self, state: PrepareState) -> dict:
        pr, cert, candidate = state["pr"], state["cert"], state["candidate"]
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
            self._evidence(state),
            False,
        )
        return {}

    def _escalate_reviewers(self, state: PrepareState) -> dict:
        if state["risk"] == "high":
            for reviewer in self.cfg.release.high_risk_reviewer_ids:
                self.act(
                    f"Add required reviewer {reviewer} (high risk)",
                    self.ado.add_reviewer,
                    state["pr"].id,
                    reviewer,
                    True,
                )
        return {}

    def _enable_auto_complete(self, state: PrepareState) -> dict:
        pr, r = state["pr"], self.cfg.release
        if not pr.auto_complete_set:
            self.act(
                f"Enable auto-complete ({r.merge_strategy}) on PR !{pr.id}",
                self.ado.set_auto_complete,
                pr.id,
                r.merge_strategy,
                True,
                False,
                f"Release {state['tag_name']}",
            )
        return {}

    def _notify_approvers(self, state: PrepareState) -> dict:
        pr, risk, tag_name = state["pr"], state["risk"], state["tag_name"]
        self.ctx.notifier.notify(
            f"Release {tag_name} awaiting approval",
            f"Risk: {risk.upper()}. {len(state['commits'])} commit(s), {len(state['work_items'])} work item(s). "
            "Approve the PR to deploy to production.",
            "warning" if risk == "high" else "info",
            pr.web_url,
        )
        return self._end(
            state,
            AgentStatus.WAITING,
            f"Release PR !{pr.id} for {tag_name} is waiting for approval (risk {risk}).",
            pr_url=pr.web_url,
            version=str(state["version"]),
            risk=risk,
            risk_factors=state["factors"],
            rc_branch=state["rc_branch"],
        )

    def _description(self, state: PrepareState) -> str:
        tag, cert, candidate = state["tag_name"], state["cert"], state["candidate"]
        commits, assessment, validation = (
            state["commits"],
            state.get("assessment"),
            state.get("validation_summary"),
        )
        lines = [
            f"Release **{tag}** to production. Candidate commit `{candidate[:10]}` (certified by `{cert.name}`).",
            "",
            f"**Risk: {state['risk'].upper()}**",
            *[f"- {f}" for f in state["factors"]],
        ]
        if assessment and assessment.risk_reasons:
            lines += ["", "_Advisor (AI-generated):_", *[f"- {x}" for x in assessment.risk_reasons]]
        if assessment and assessment.notes_markdown:
            lines += ["", assessment.notes_markdown]
        lines += [
            "",
            f"### Commits ({len(commits)})",
            *[f"- {c.message.splitlines()[0]}" for c in commits[:40] if c.message],
        ]
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
            "_Opened by the Release Agent. Merges automatically after the required approvals; production "
            "deployment then waits on the `databricks-prod` environment approval._",
        ]
        return "\n".join(lines)

    def _evidence(self, state: PrepareState) -> str:
        validation, assessment = state.get("validation_summary"), state.get("assessment")
        rows = [
            COMMENT_MARKER,
            f"## 🤖 Release Agent — {state['tag_name']}",
            "",
            "| Check | Result |",
            "|---|---|",
            f"| QA certification | ✅ `{state['cert'].name}` → `{state['candidate'][:10]}` |",
            f"| Change window | ✅ open at {self.ctx.clock():%Y-%m-%d %H:%M} UTC |",
        ]
        if validation:
            rows.append(
                f"| Fresh QA validation | ✅ {validation.get('errors', 0)} errors / "
                f"{validation.get('warnings', 0)} warnings |"
            )
        rows.append(
            f"| Risk (rule-based floor{', advisor' if assessment else ''}) | **{state['risk'].upper()}** |"
        )
        rows += ["", "**Risk factors**", *[f"- {f}" for f in state["factors"]]]
        return "\n".join(rows)

    # ------------------------------------------------------------------ finalize nodes
    def _fin_end(self, state: FinalizeState, status: AgentStatus, message: str, **outputs: Any) -> dict:
        return {
            "result": self.finish(
                "finalize",
                status,
                message,
                commit=state["commit"],
                environment=self.cfg.release.environment,
                **outputs,
            )
        }

    def _identify_release(self, state: FinalizeState) -> dict:
        r, commit = self.cfg.release, state["commit"]
        completed = self.ado.list_pull_requests(self.cfg.branches.prod, "completed", top=30)
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
        return {"pr": pr, "versions": versions, "version": version, "tag_name": f"{r.tag_prefix}{version}"}

    def _check_existing_tag(self, state: FinalizeState) -> dict:
        for v, name, sha in state["versions"]:
            if v == state["version"]:
                if sha == state["commit"]:
                    return self._fin_end(
                        state,
                        AgentStatus.SUCCEEDED,
                        f"{name} already tagged on {sha[:8]}",
                        version=str(v),
                        tag=name,
                    )
                return self._fin_end(
                    state,
                    AgentStatus.FAILED,
                    f"Tag {name} already points at {sha[:8]}, not {state['commit'][:8]}.",
                )
        return {}

    def _verify_production(self, state: FinalizeState) -> dict:
        r, tag_name = self.cfg.release, state["tag_name"]
        verify = self.validation_agent.run(
            ValidationRequest(
                environment=r.environment,
                validate_all=True,
                profile=r.verify_profile,
                commit=state["commit"],
                post_to_pr=False,
            )
        )
        if verify.status is AgentStatus.FAILED:
            rollback = state["versions"][-1][1] if state["versions"] else None
            self.ctx.notifier.notify(
                f"Production verification failed for {tag_name}",
                f"{verify.message}. Rolling back to {rollback or '(no previous release)'}.",
                "error",
                self.ctx.pipeline_run_url,
            )
            return self._fin_end(
                state,
                AgentStatus.FAILED,
                f"Post-deploy verification failed: {verify.message}",
                version=str(state["version"]),
                rollback_tag=rollback,
            )
        return {}

    def _tag_release(self, state: FinalizeState) -> dict:
        tag_name, commit = state["tag_name"], state["commit"]
        self.act(
            f"Create tag {tag_name} on {commit[:8]}",
            self.ado.create_annotated_tag,
            tag_name,
            commit,
            f"Production release {tag_name} (run {self.ctx.run_id}).",
        )
        return {}

    def _open_back_merge(self, state: FinalizeState) -> dict:
        b, pr, tag_name = self.cfg.branches, state.get("pr"), state["tag_name"]
        if not (pr and pr.last_merge_source_commit):
            return {"back_merge": None}
        missing_in_qa = self.ado.list_commits(pr.last_merge_source_commit, b.qa)
        if not missing_in_qa:  # normal release: main already has everything
            return {"back_merge": None}
        back_merge = self.ado.find_active_pr(b.prod, b.qa) or self.act(
            f"Open back-merge PR {b.prod} -> {b.qa}",
            self.ado.create_pr,
            b.prod,
            b.qa,
            f"chore: back-merge {tag_name} into {b.qa}",
            f"{tag_name} contains {len(missing_in_qa)} commit(s) not yet in `{b.qa}` (hotfix). "
            "Merge them back so the next release does not regress.",
        )
        return {"back_merge": back_merge}

    def _announce(self, state: FinalizeState) -> dict:
        tag_name, commit = state["tag_name"], state["commit"]
        self.ctx.notifier.notify(
            f"Released {tag_name} to production",
            f"Commit {commit[:8]} verified and tagged.",
            "success",
            self.ctx.pipeline_run_url,
        )
        return self._fin_end(
            state,
            AgentStatus.SUCCEEDED,
            f"Released {tag_name}",
            version=str(state["version"]),
            tag=tag_name,
            back_merge_pr=getattr(state.get("back_merge"), "id", None),
        )
