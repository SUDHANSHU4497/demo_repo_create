"""Azure DevOps REST client (Git, Pull Requests, Policies, Pipelines). API version 7.1.

Only the endpoints the agents need are implemented. Every write is idempotent from the agent's
point of view: callers look up existing state first (open PR, existing tag, existing comment).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from agentic_devops.integrations.http import HttpClient

ZERO_SHA = "0" * 40
_SHA = re.compile(r"^[0-9a-f]{40}$")

VOTE_APPROVED = 10
VOTE_APPROVED_WITH_SUGGESTIONS = 5
VOTE_WAITING = -5
VOTE_REJECTED = -10


def ref(branch: str) -> str:
    return branch if branch.startswith("refs/") else f"refs/heads/{branch}"


def short_ref(name: str | None) -> str:
    if not name:
        return ""
    for prefix in ("refs/heads/", "refs/tags/"):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name


@dataclass
class Reviewer:
    id: str
    display_name: str
    vote: int = 0
    is_required: bool = False

    @property
    def approved(self) -> bool:
        return self.vote >= VOTE_APPROVED_WITH_SUGGESTIONS

    @property
    def rejected(self) -> bool:
        return self.vote <= VOTE_WAITING


@dataclass
class PullRequest:
    id: int
    title: str
    source_branch: str
    target_branch: str
    status: str = "active"  # active | completed | abandoned
    description: str = ""
    merge_status: str | None = None  # succeeded | conflicts | queued | failure | rejectedByPolicy ...
    is_draft: bool = False
    last_merge_source_commit: str | None = None
    last_merge_commit: str | None = None
    auto_complete_set: bool = False
    reviewers: list[Reviewer] = field(default_factory=list)
    web_url: str | None = None

    @classmethod
    def from_api(cls, d: dict[str, Any], web_base: str | None = None) -> PullRequest:
        pr_id = int(d["pullRequestId"])
        return cls(
            id=pr_id,
            title=d.get("title", ""),
            description=d.get("description", "") or "",
            source_branch=short_ref(d.get("sourceRefName")),
            target_branch=short_ref(d.get("targetRefName")),
            status=d.get("status", "active"),
            merge_status=d.get("mergeStatus"),
            is_draft=bool(d.get("isDraft", False)),
            last_merge_source_commit=(d.get("lastMergeSourceCommit") or {}).get("commitId"),
            last_merge_commit=(d.get("lastMergeCommit") or {}).get("commitId"),
            auto_complete_set=bool(d.get("autoCompleteSetBy")),
            reviewers=[
                Reviewer(
                    id=r.get("id", ""),
                    display_name=r.get("displayName", ""),
                    vote=int(r.get("vote", 0)),
                    is_required=bool(r.get("isRequired", False)),
                )
                for r in d.get("reviewers") or []
            ],
            web_url=f"{web_base}/pullrequest/{pr_id}" if web_base else None,
        )


@dataclass
class Commit:
    id: str
    message: str
    author: str = ""


@dataclass
class GitTag:
    name: str
    commit_id: str


@dataclass
class PolicyEvaluation:
    name: str
    status: str  # approved | rejected | running | queued | notApplicable | broken
    is_blocking: bool

    @property
    def passing(self) -> bool:
        return self.status in ("approved", "notApplicable")


class AzureDevOpsClient:
    def __init__(
        self, organization_url: str, project: str, repository: str, http: HttpClient, api_version: str = "7.1"
    ):
        if not organization_url or not project or not repository:
            raise ValueError("azure_devops.organization_url, project and repository are required")
        self.org = organization_url.rstrip("/")
        self.project = project
        self.repository = repository
        self.http = http
        self.api_version = api_version
        self.base = f"{self.org}/{quote(project)}/_apis"
        self.repo_base = f"{self.base}/git/repositories/{quote(repository)}"
        self.web_base = f"{self.org}/{quote(project)}/_git/{quote(repository)}"
        self._identity_id: str | None = None
        self._project_id: str | None = None

    # ------------------------------------------------------------------ helpers
    def _get(
        self, url: str, params: dict[str, Any] | None = None, api_version: str | None = None, **kw
    ) -> Any:
        p = {"api-version": api_version or self.api_version, **(params or {})}
        return self.http.request("GET", url, params=p, **kw)

    def _send(
        self,
        method: str,
        url: str,
        body: Any,
        params: dict[str, Any] | None = None,
        api_version: str | None = None,
    ) -> Any:
        p = {"api-version": api_version or self.api_version, **(params or {})}
        return self.http.request(method, url, params=p, json=body)

    @staticmethod
    def _version_type(version: str) -> str:
        return "commit" if _SHA.match(version) else "branch"

    # ------------------------------------------------------------------ identity / project
    def current_identity_id(self) -> str:
        if not self._identity_id:
            data = self.http.request("GET", f"{self.org}/_apis/connectionData")
            self._identity_id = data["authenticatedUser"]["id"]
        return self._identity_id

    def project_id(self) -> str:
        if not self._project_id:
            self._project_id = self._get(f"{self.org}/_apis/projects/{quote(self.project)}")["id"]
        return self._project_id

    # ------------------------------------------------------------------ refs / tags / commits
    def get_branch_head(self, branch: str) -> str | None:
        full = ref(branch)
        data = self._get(f"{self.repo_base}/refs", {"filter": full[len("refs/") :]})
        for r in (data or {}).get("value", []):
            if r["name"] == full:  # filter is a prefix match, so compare exactly
                return r["objectId"]
        return None

    def create_branch(self, branch: str, commit_id: str) -> None:
        body = [{"name": ref(branch), "oldObjectId": ZERO_SHA, "newObjectId": commit_id}]
        res = self._send("POST", f"{self.repo_base}/refs", body)
        update = (res or {}).get("value", [{}])[0]
        if not update.get("success", False):
            raise RuntimeError(f"Could not create branch {branch}: {update}")

    def list_tags(self, prefix: str = "") -> list[GitTag]:
        data = self._get(f"{self.repo_base}/refs", {"filter": f"tags/{prefix}", "peelTags": "true"})
        tags = []
        for r in (data or {}).get("value", []):
            tags.append(GitTag(short_ref(r["name"]), r.get("peeledObjectId") or r["objectId"]))
        return tags

    def create_annotated_tag(self, name: str, commit_id: str, message: str) -> None:
        body = {"name": name, "taggedObject": {"objectId": commit_id}, "message": message}
        self._send("POST", f"{self.repo_base}/annotatedtags", body)

    def list_commits(
        self, item_version: str, compare_version: str | None = None, top: int = 500
    ) -> list[Commit]:
        """Commits reachable from ``item_version`` but not from ``compare_version``."""
        params: dict[str, Any] = {
            "searchCriteria.itemVersion.version": item_version,
            "searchCriteria.itemVersion.versionType": self._version_type(item_version),
            "searchCriteria.$top": top,
        }
        if compare_version:
            params["searchCriteria.compareVersion.version"] = compare_version
            params["searchCriteria.compareVersion.versionType"] = self._version_type(compare_version)
        data = self._get(f"{self.repo_base}/commits", params)
        return [
            Commit(c["commitId"], c.get("comment", ""), (c.get("author") or {}).get("name", ""))
            for c in (data or {}).get("value", [])
        ]

    def diff_files(self, base_version: str, target_version: str) -> list[str]:
        """Files changed between two branches/commits (like ``git diff base...target --name-only``)."""
        files: list[str] = []
        skip = 0
        while True:
            data = (
                self._get(
                    f"{self.repo_base}/diffs/commits",
                    {
                        "baseVersion": base_version,
                        "baseVersionType": self._version_type(base_version),
                        "targetVersion": target_version,
                        "targetVersionType": self._version_type(target_version),
                        "$top": 2000,
                        "$skip": skip,
                    },
                )
                or {}
            )
            changes = data.get("changes", [])
            files += [
                c["item"]["path"]
                for c in changes
                if c.get("item", {}).get("path") and not c["item"].get("isFolder")
            ]
            if data.get("allChangesIncluded", True) or not changes:
                break
            skip += len(changes)
        return sorted(set(files))

    # ------------------------------------------------------------------ pull requests
    def find_active_pr(self, source_branch: str, target_branch: str) -> PullRequest | None:
        data = self._get(
            f"{self.repo_base}/pullrequests",
            {
                "searchCriteria.sourceRefName": ref(source_branch),
                "searchCriteria.targetRefName": ref(target_branch),
                "searchCriteria.status": "active",
            },
        )
        prs = (data or {}).get("value", [])
        return PullRequest.from_api(prs[0], self.web_base) if prs else None

    def list_pull_requests(
        self, target_branch: str, status: str = "active", top: int = 50
    ) -> list[PullRequest]:
        data = self._get(
            f"{self.repo_base}/pullrequests",
            {
                "searchCriteria.targetRefName": ref(target_branch),
                "searchCriteria.status": status,
                "$top": top,
            },
        )
        return [PullRequest.from_api(p, self.web_base) for p in (data or {}).get("value", [])]

    def get_pr(self, pr_id: int) -> PullRequest:
        return PullRequest.from_api(self._get(f"{self.repo_base}/pullrequests/{pr_id}"), self.web_base)

    def create_pr(
        self,
        source_branch: str,
        target_branch: str,
        title: str,
        description: str,
        work_item_ids: list[int] | None = None,
        is_draft: bool = False,
    ) -> PullRequest:
        body = {
            "sourceRefName": ref(source_branch),
            "targetRefName": ref(target_branch),
            "title": title[:400],
            "description": description[:4000],  # Azure DevOps limit
            "isDraft": is_draft,
            "workItemRefs": [{"id": str(i)} for i in work_item_ids or []],
        }
        return PullRequest.from_api(self._send("POST", f"{self.repo_base}/pullrequests", body), self.web_base)

    def update_pr(self, pr_id: int, **fields: Any) -> PullRequest:
        if "description" in fields:
            fields["description"] = str(fields["description"])[:4000]
        return PullRequest.from_api(
            self._send("PATCH", f"{self.repo_base}/pullrequests/{pr_id}", fields), self.web_base
        )

    def set_auto_complete(
        self,
        pr_id: int,
        merge_strategy: str,
        delete_source_branch: bool,
        transition_work_items: bool,
        merge_commit_message: str,
    ) -> PullRequest:
        """Azure DevOps completes the PR itself once every branch policy passes.

        ``bypassPolicy`` is always False: the agent identity must never hold "Bypass policies".
        """
        return self.update_pr(
            pr_id,
            autoCompleteSetBy={"id": self.current_identity_id()},
            completionOptions={
                "mergeStrategy": merge_strategy,
                "deleteSourceBranch": delete_source_branch,
                "transitionWorkItems": transition_work_items,
                "mergeCommitMessage": merge_commit_message,
                "bypassPolicy": False,
            },
        )

    def latest_iteration_id(self, pr_id: int) -> int | None:
        data = self._get(f"{self.repo_base}/pullRequests/{pr_id}/iterations")
        its = (data or {}).get("value", [])
        return max(int(i["id"]) for i in its) if its else None

    def get_pr_changed_files(self, pr_id: int) -> list[str]:
        it = self.latest_iteration_id(pr_id)
        if it is None:
            return []
        files: list[str] = []
        skip = 0
        while True:
            data = (
                self._get(
                    f"{self.repo_base}/pullRequests/{pr_id}/iterations/{it}/changes",
                    {"$compareTo": 0, "$top": 2000, "$skip": skip},
                )
                or {}
            )
            for entry in data.get("changeEntries", []):
                item = entry.get("item") or {}
                if item.get("isFolder"):
                    continue
                for key in ("path", "originalPath"):
                    if item.get(key):
                        files.append(item[key])
                if entry.get("originalPath"):
                    files.append(entry["originalPath"])
            if not data.get("nextTop"):
                break
            skip = int(data.get("nextSkip", skip + 2000))
        return sorted(set(files))

    def get_pr_work_items(self, pr_id: int) -> list[int]:
        data = self._get(f"{self.repo_base}/pullRequests/{pr_id}/workitems")
        return [int(w["id"]) for w in (data or {}).get("value", [])]

    def post_pr_status(
        self,
        pr_id: int,
        state: str,
        name: str,
        genre: str,
        description: str,
        target_url: str | None = None,
        iteration_id: int | None = None,
    ) -> None:
        """state: pending | succeeded | failed | error | notApplicable.

        Statuses are attached to the latest iteration so a branch policy configured with
        "reset status when there are new changes" invalidates them on every push.
        """
        body: dict[str, Any] = {
            "state": state,
            "description": description[:250],
            "context": {"name": name, "genre": genre},
        }
        if target_url:
            body["targetUrl"] = target_url
        if iteration_id is not None:
            body["iterationId"] = iteration_id
        self._send("POST", f"{self.repo_base}/pullRequests/{pr_id}/statuses", body)

    def upsert_pr_comment(self, pr_id: int, marker: str, content: str, active: bool = True) -> int:
        """Create or update the single thread identified by ``marker``; returns the thread id.

        Failing reports stay ``active`` so the "comments must be resolved" policy also blocks;
        passing reports are ``closed``.
        """
        threads = (self._get(f"{self.repo_base}/pullRequests/{pr_id}/threads") or {}).get("value", [])
        status = "active" if active else "closed"
        for t in threads:
            comments = t.get("comments") or []
            if comments and marker in (comments[0].get("content") or ""):
                tid, cid = t["id"], comments[0]["id"]
                self._send(
                    "PATCH",
                    f"{self.repo_base}/pullRequests/{pr_id}/threads/{tid}/comments/{cid}",
                    {"content": content},
                )
                self._send(
                    "PATCH", f"{self.repo_base}/pullRequests/{pr_id}/threads/{tid}", {"status": status}
                )
                return int(tid)
        body = {"comments": [{"parentCommentId": 0, "content": content, "commentType": 1}], "status": status}
        res = self._send("POST", f"{self.repo_base}/pullRequests/{pr_id}/threads", body)
        return int(res["id"])

    def add_reviewer(self, pr_id: int, reviewer_id: str, required: bool = True) -> None:
        self._send(
            "PUT",
            f"{self.repo_base}/pullRequests/{pr_id}/reviewers/{reviewer_id}",
            {"vote": 0, "isRequired": required},
        )

    def get_policy_evaluations(self, pr_id: int) -> list[PolicyEvaluation]:
        artifact = f"vstfs:///CodeReview/CodeReviewId/{self.project_id()}/{pr_id}"
        data = self._get(
            f"{self.base}/policy/evaluations", {"artifactId": artifact}, api_version="7.1-preview.1"
        )
        out = []
        for e in (data or {}).get("value", []):
            conf = e.get("configuration") or {}
            name = ((conf.get("type") or {}).get("displayName")) or "policy"
            settings = conf.get("settings") or {}
            if settings.get("statusName"):
                name = f"{name}: {settings.get('statusGenre', '')}/{settings['statusName']}"
            elif settings.get("displayName"):
                name = f"{name}: {settings['displayName']}"
            out.append(
                PolicyEvaluation(
                    name=name, status=e.get("status", ""), is_blocking=bool(conf.get("isBlocking"))
                )
            )
        return out

    # ------------------------------------------------------------------ pipelines
    def run_pipeline(
        self, pipeline_id: int, branch: str, template_parameters: dict[str, Any] | None = None
    ) -> dict:
        body = {
            "resources": {"repositories": {"self": {"refName": ref(branch)}}},
            "templateParameters": template_parameters or {},
        }
        return self._send("POST", f"{self.base}/pipelines/{pipeline_id}/runs", body)
