"""Command line entry point used by the Azure DevOps pipelines.

    agentic-devops validate        --pr-id 123                     # Data Validation Agent (PR gate)
    agentic-devops validate        --environment qa --all          # ad-hoc / scheduled checks
    agentic-devops promote-qa      --source-branch feature/123-x    # QA Promotion Agent: open PR to main
    agentic-devops certify-qa      --commit <sha>                   # after QA deploy: validate + tag
    agentic-devops release-prepare [--commit <sha>] [--bump auto]   # Release Agent: PR to release
    agentic-devops release-finalize --commit <sha>                  # after prod deploy: verify + tag
    agentic-devops rollback-target [--failed-commit <sha>]          # previous good release tag
    agentic-devops handle-event    --file event.json               # service-hook payload -> agent
    agentic-devops graph           [--name release-preparation]     # print LangGraph flows as Mermaid
    agentic-devops models          [--check]                        # task -> Foundry model routing

Exit code 0 = succeeded / waiting / skipped, 1 = failed / blocked, 2 = usage or config error.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from agentic_devops.config import ConfigError, load_config
from agentic_devops.core.runtime import log_issue, set_output_variable
from agentic_devops.models import AgentResult


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="agentic-devops", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--config", default="config/agentic.yml")
    p.add_argument("--dry-run", action="store_true", help="Plan only: no writes to Azure DevOps")
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="Run the Data Validation Agent")
    v.add_argument("--pr-id", type=int)
    v.add_argument("--environment")
    v.add_argument("--table", action="append", dest="tables", help="Contract table, e.g. sales.orders")
    v.add_argument("--all", action="store_true", dest="validate_all")
    v.add_argument("--profile", choices=["full", "schema_only"], default="full")
    v.add_argument("--commit")
    v.add_argument("--no-pr-comment", action="store_true")

    q = sub.add_parser("promote-qa", help="QA Promotion Agent: feature branch -> main")
    q.add_argument("--source-branch", required=True)
    q.add_argument("--work-item", type=int, action="append", dest="work_items")

    c = sub.add_parser("certify-qa", help="QA Promotion Agent: validate QA after deploy and tag the commit")
    c.add_argument("--commit", required=True)
    c.add_argument("--source-branch")

    rp = sub.add_parser("release-prepare", help="Release Agent: open the release PR")
    rp.add_argument("--commit")
    rp.add_argument("--bump", choices=["auto", "major", "minor", "patch"], default="auto")

    rf = sub.add_parser("release-finalize", help="Release Agent: verify production after deploy and tag")
    rf.add_argument("--commit", required=True)

    rb = sub.add_parser("rollback-target", help="Release Agent: previous good production tag")
    rb.add_argument("--failed-commit")

    e = sub.add_parser("handle-event", help="Route an Azure DevOps service-hook payload")
    e.add_argument("--file", required=True)

    g = sub.add_parser("graph", help="Print the agents' LangGraph flows as Mermaid")
    g.add_argument("--name", help="One graph (default: all)")

    m = sub.add_parser("models", help="Show which Foundry model handles each LLM task")
    m.add_argument("--check", action="store_true", help="Send a tiny test prompt to every configured model")
    return p


def _emit(result: AgentResult) -> int:
    print(json.dumps(result.to_dict(), indent=2, default=str))
    set_output_variable("agentStatus", result.status.value)
    for key, value in result.outputs.items():
        if isinstance(value, (str, int, float, bool)):
            set_output_variable(key, value)
    if result.exit_code:
        log_issue("error", f"{result.agent}/{result.action}: {result.message}")
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    try:
        from agentic_devops.core.factory import build_agents, build_context
        from agentic_devops.core.flow import to_mermaid

        config = load_config(args.config)
        agents = build_agents(build_context(config, dry_run=args.dry_run))
    except ImportError as exc:
        print(
            f"Missing dependency ({exc.name or exc}): pip install 'agentic-devops[agents,foundry]'",
            file=sys.stderr,
        )
        return 2
    except (ConfigError, FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    cmd = args.command
    if cmd == "graph":
        graphs = agents.orchestrator.graphs()
        names = [args.name] if args.name else list(graphs)
        for name in names:
            if name not in graphs:
                print(f"Unknown graph {name!r}. Known: {sorted(graphs)}", file=sys.stderr)
                return 2
            print(f"%% {name}\n{to_mermaid(graphs[name])}\n")
        return 0
    if cmd == "models":
        from agentic_devops.llm.registry import ModelRegistry, check_models

        registry = ModelRegistry(config.llm)
        report = {"enabled": config.llm.enabled, "tasks": registry.describe()}
        if args.check:
            report["check"] = check_models(registry)
        print(json.dumps(report, indent=2))
        return 0 if all(v == "ok" for v in report.get("check", {}).values()) else 1

    run = agents.orchestrator.run_command
    if cmd == "validate":
        result = run(
            "validate",
            pr_id=args.pr_id,
            environment=args.environment,
            tables=args.tables,
            validate_all=args.validate_all,
            profile=args.profile,
            commit=args.commit,
            post_to_pr=not args.no_pr_comment,
        )
    elif cmd == "promote-qa":
        result = run("promote-qa", source_branch=args.source_branch, work_item_ids=args.work_items)
    elif cmd == "certify-qa":
        result = run("certify-qa", commit=args.commit, source_branch=args.source_branch)
    elif cmd == "release-prepare":
        result = run("release-prepare", commit=args.commit, bump=args.bump)
    elif cmd == "release-finalize":
        result = run("release-finalize", commit=args.commit)
    elif cmd == "rollback-target":
        result = run("rollback-target", failed_commit=args.failed_commit)
    elif cmd == "handle-event":
        with open(args.file, encoding="utf-8") as fh:
            result = agents.orchestrator.handle_event(json.load(fh))
    else:  # pragma: no cover - argparse enforces the choices
        return 2
    return _emit(result)


if __name__ == "__main__":
    raise SystemExit(main())
