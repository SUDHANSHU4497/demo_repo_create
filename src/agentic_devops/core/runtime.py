"""Helpers for running inside Azure Pipelines (output variables, run links)."""

from __future__ import annotations

import os


def in_azure_pipelines() -> bool:
    return os.environ.get("TF_BUILD", "").lower() == "true"


def pipeline_run_url() -> str | None:
    collection = os.environ.get("SYSTEM_COLLECTIONURI") or os.environ.get(
        "SYSTEM_TEAMFOUNDATIONCOLLECTIONURI"
    )
    project = os.environ.get("SYSTEM_TEAMPROJECT")
    build_id = os.environ.get("BUILD_BUILDID")
    if collection and project and build_id:
        return f"{collection.rstrip('/')}/{project}/_build/results?buildId={build_id}"
    return None


def pipeline_context() -> dict[str, str]:
    keys = {
        "build_id": "BUILD_BUILDID",
        "build_number": "BUILD_BUILDNUMBER",
        "pipeline": "BUILD_DEFINITIONNAME",
        "source_branch": "BUILD_SOURCEBRANCH",
        "source_version": "BUILD_SOURCEVERSION",
        "requested_for": "BUILD_REQUESTEDFOR",
    }
    return {k: os.environ[v] for k, v in keys.items() if os.environ.get(v)}


def set_output_variable(name: str, value: object) -> None:
    """Expose a value to later steps/stages (``dependencies.<job>.outputs['<step>.<name>']``)."""
    if in_azure_pipelines():
        print(f"##vso[task.setvariable variable={name};isOutput=true]{value}")


def log_issue(kind: str, message: str) -> None:
    """kind: error | warning - shows up on the pipeline run summary."""
    if in_azure_pipelines():
        print(f"##vso[task.logissue type={kind}]{message}")
