"""Deterministic guardrails shared by the agents: change windows, work items, PR titles, release risk."""

from __future__ import annotations

import re
from datetime import datetime, time
from fnmatch import fnmatch
from zoneinfo import ZoneInfo

from agentic_devops.config import ChangeWindow, FreezePeriod

_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_WORK_ITEM_IN_TEXT = re.compile(r"(?:AB#|(?<![\w&])#)(\d{1,9})\b")
_WORK_ITEM_IN_BRANCH = re.compile(r"^[^/]+/(\d{1,9})(?:[-_].*)?$")
CONVENTIONAL_TITLE = re.compile(
    r"^(feat|fix|chore|docs|refactor|perf|test|build|ci|style|revert)(\([a-z0-9._/-]+\))?!?: \S.{2,}$"
)
RISK_LEVELS = ("low", "medium", "high")


def check_change_window(
    now_utc: datetime, window: ChangeWindow | None, freezes: list[FreezePeriod]
) -> tuple[bool, str]:
    """Is a production release allowed right now? Returns (allowed, reason)."""
    tz = ZoneInfo(window.timezone) if window else ZoneInfo("UTC")
    local = now_utc.astimezone(tz)
    for f in freezes:
        if f.start <= local.date() <= f.end:
            return False, f"Change freeze {f.start}..{f.end}: {f.reason or 'no reason given'}"
    if window is None:
        return True, "No change window configured"
    day = _DAYS[local.weekday()]
    if day not in window.days:
        return (
            False,
            f"{day} is outside the change window ({', '.join(window.days)} {window.start}-{window.end} {window.timezone})",
        )
    start, end = time.fromisoformat(window.start), time.fromisoformat(window.end)
    if not (start <= local.time() <= end):
        return False, (
            f"{local:%H:%M} {window.timezone} is outside the change window ({window.start}-{window.end})"
        )
    return True, f"Inside change window ({day} {local:%H:%M} {window.timezone})"


def extract_work_items(branch: str, messages: list[str]) -> set[int]:
    """Work items from ``feature/1234-desc`` branch names and ``AB#1234`` / ``#1234`` in commit messages."""
    ids: set[int] = set()
    m = _WORK_ITEM_IN_BRANCH.match(branch)
    if m:
        ids.add(int(m.group(1)))
    for msg in messages:
        ids.update(int(x) for x in _WORK_ITEM_IN_TEXT.findall(msg))
    return ids


def is_conventional_title(title: str) -> bool:
    return bool(CONVENTIONAL_TITLE.match(title.strip()))


def assess_release_risk(
    changed_files: list[str],
    contracts_dir: str,
    global_impact_paths: list[str],
    commit_count: int,
    validation_warnings: int = 0,
) -> tuple[str, list[str]]:
    """Rule-based risk floor. The LLM advisor may raise it, never lower it."""
    factors: list[str] = []
    files = [f.lstrip("/") for f in changed_files]
    contracts_prefix = contracts_dir.strip("/") + "/"
    contract_changes = [f for f in files if f.startswith(contracts_prefix)]
    global_changes = [f for f in files if any(fnmatch(f, g) for g in global_impact_paths)]
    infra_changes = [
        f for f in files if f in ("databricks.yml",) or f.startswith(("resources/", "pipelines/"))
    ]

    level = 0
    if contract_changes:
        factors.append(f"{len(contract_changes)} data contract(s) changed: {', '.join(contract_changes[:5])}")
        level = max(level, 1)
    if global_changes:
        factors.append(f"Shared code changed (affects every table): {', '.join(global_changes[:5])}")
        level = max(level, 2)
    if infra_changes:
        factors.append(f"Job/bundle/pipeline definitions changed: {', '.join(infra_changes[:5])}")
        level = max(level, 1)
    if len(files) > 50 or commit_count > 30:
        factors.append(f"Large change set ({len(files)} files, {commit_count} commits)")
        level = max(level, 2)
    elif len(files) > 15:
        factors.append(f"Medium change set ({len(files)} files)")
        level = max(level, 1)
    if validation_warnings:
        factors.append(f"{validation_warnings} data validation warning(s) in QA")
        level = max(level, 1)
    if not factors:
        factors.append("Small change set with no contract, shared-code or infrastructure changes")
    return RISK_LEVELS[level], factors


def escalate_only(baseline: str, proposed: str | None) -> str:
    """The advisor can raise risk but can never lower the deterministic baseline."""
    if proposed not in RISK_LEVELS:
        return baseline
    return RISK_LEVELS[max(RISK_LEVELS.index(baseline), RISK_LEVELS.index(proposed))]
