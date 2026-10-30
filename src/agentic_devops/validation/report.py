"""Markdown rendering of validation reports for Azure DevOps PR comments."""

from __future__ import annotations

from agentic_devops.models import Finding, Severity, ValidationReport

COMMENT_MARKER = "<!-- agentic-devops:data-validation -->"
_ICON = {Severity.ERROR: "❌", Severity.WARNING: "⚠️", Severity.INFO: "ℹ️"}


def _cell(text: object) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def _finding_line(f: Finding) -> str:
    where = f"**{f.table}**" if f.table else "**(cross-table)**"
    if f.column:
        where += f" · `{f.column}`"
    line = f"- {where} · `{f.rule_id}` — {f.message}"
    if f.suggestion:
        line += f"<br/>&nbsp;&nbsp;↳ _Fix:_ {f.suggestion}"
    samples = f.details.get("sample_duplicates") if f.details else None
    if samples:
        shown = ", ".join(
            "(" + ", ".join(f"{k}={v}" for k, v in s["key"].items()) + f") ×{s['count']}" for s in samples[:5]
        )
        line += f"<br/>&nbsp;&nbsp;↳ _Sample duplicate keys:_ {shown}"
    return line


def render_markdown(
    report: ValidationReport,
    *,
    advisor_markdown: str | None = None,
    commit: str | None = None,
    run_url: str | None = None,
    max_findings: int = 50,
) -> str:
    status = "✅ PASSED" if report.passed else "❌ FAILED"
    meta = [f"Environment: **{report.environment}** (catalog `{report.catalog}`)", f"Run: `{report.run_id}`"]
    if commit:
        meta.append(f"Commit: `{commit[:10]}`")
    if report.profile != "full":
        meta.append(f"Profile: `{report.profile}`")
    if run_url:
        meta.append(f"[Pipeline run]({run_url})")

    lines = [
        COMMENT_MARKER,
        f"## 🤖 Data Validation Agent — {status}",
        " · ".join(meta),
        "",
        f"**{report.error_count} error(s)**, **{report.warning_count} warning(s)** across "
        f"{len(report.tables)} table(s)."
        + (" Warnings are blocking (`fail_on_warnings: true`)." if report.fail_on_warnings else ""),
        "",
        "| Table | Rows | Errors | Warnings | Result |",
        "|---|---:|---:|---:|:---:|",
    ]
    for t in report.tables:
        rows = f"{t.row_count:,}" if t.row_count is not None else "—"
        ok = t.error_count == 0 and not (report.fail_on_warnings and t.warning_count)
        lines.append(
            f"| {_cell(t.table)} | {rows} | {t.error_count} | {t.warning_count} | {'✅' if ok else '❌'} |"
        )

    findings = report.all_findings
    for sev, title in ((Severity.ERROR, "Errors"), (Severity.WARNING, "Warnings")):
        items = [f for f in findings if f.severity is sev]
        if not items:
            continue
        lines += ["", f"### {_ICON[sev]} {title} ({len(items)})"]
        lines += [_finding_line(f) for f in items[:max_findings]]
        if len(items) > max_findings:
            lines.append(
                f"- …and {len(items) - max_findings} more (see the `validation-report.json` artifact)."
            )

    infos = [f for f in findings if f.severity is Severity.INFO]
    if infos:
        lines += ["", f"<details><summary>ℹ️ Info ({len(infos)})</summary>", ""]
        lines += [_finding_line(f) for f in infos[:max_findings]]
        lines += ["", "</details>"]

    if advisor_markdown:
        lines += [
            "",
            "### 🧠 Advisor notes",
            "_AI-generated suggestions. Advisory only: the gate result above comes from the deterministic rules._",
            "",
            advisor_markdown.strip(),
        ]
    lines += ["", "_Re-runs automatically on every push to this PR._"]
    text = "\n".join(lines)
    # Azure DevOps comments are capped at 150k characters.
    return text if len(text) < 140_000 else text[:140_000] + "\n\n…(truncated)"
