"""Typed configuration loaded from ``config/agentic.yml``.

Values may reference environment variables as ``${NAME}`` or ``${NAME:-default}`` so secrets and
per-environment IDs stay in Azure DevOps variable groups / Key Vault, never in git.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(ValueError):
    pass


def _interpolate(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), m.group(2) if m.group(2) is not None else ""), value
        )
    if isinstance(value, dict):
        return {k: _interpolate(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_interpolate(v) for v in value]
    return value


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    text = _opt_str(value)
    if text is None:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ConfigError(f"Expected an integer, got {value!r}") from exc


def _bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


@dataclass
class AzureDevOpsConfig:
    organization_url: str
    project: str
    repository: str
    status_genre: str = "agentic-devops"
    api_version: str = "7.1"
    pipelines: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AzureDevOpsConfig:
        return cls(
            organization_url=(_opt_str(d.get("organization_url")) or "").rstrip("/"),
            project=_opt_str(d.get("project")) or "",
            repository=_opt_str(d.get("repository")) or "",
            status_genre=_opt_str(d.get("status_genre")) or "agentic-devops",
            api_version=_opt_str(d.get("api_version")) or "7.1",
            pipelines={
                k: v for k, v in ((k, _opt_int(v)) for k, v in (d.get("pipelines") or {}).items()) if v
            },
        )


@dataclass
class BranchConfig:
    qa: str = "main"
    prod: str = "release"
    feature_pattern: str = r"^(feature|bugfix)/[A-Za-z0-9._-]+$"
    hotfix_pattern: str = r"^hotfix/[A-Za-z0-9._-]+$"
    release_candidate_prefix: str = "rc/"
    qa_certified_tag_prefix: str = "qa-certified/"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BranchConfig:
        base = cls()
        return cls(
            qa=_opt_str(d.get("qa")) or base.qa,
            prod=_opt_str(d.get("prod")) or base.prod,
            feature_pattern=_opt_str(d.get("feature_pattern")) or base.feature_pattern,
            hotfix_pattern=_opt_str(d.get("hotfix_pattern")) or base.hotfix_pattern,
            release_candidate_prefix=_opt_str(d.get("release_candidate_prefix"))
            or base.release_candidate_prefix,
            qa_certified_tag_prefix=_opt_str(d.get("qa_certified_tag_prefix"))
            or base.qa_certified_tag_prefix,
        )


@dataclass
class EnvironmentConfig:
    name: str
    catalog: str
    workspace_url: str | None = None
    validation_job_id: int | None = None
    volume_path: str = "/Volumes/ops/agentic/validation"
    results_table: str | None = None

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> EnvironmentConfig:
        catalog = _opt_str(d.get("catalog"))
        if not catalog:
            raise ConfigError(f"environments.{name}.catalog is required")
        return cls(
            name=name,
            catalog=catalog,
            workspace_url=(_opt_str(d.get("workspace_url")) or "").rstrip("/") or None,
            validation_job_id=_opt_int(d.get("validation_job_id")),
            volume_path=(_opt_str(d.get("volume_path")) or cls.volume_path).rstrip("/"),
            results_table=_opt_str(d.get("results_table")),
        )


@dataclass
class ValidationConfig:
    contracts_dir: str = "contracts"
    naming_standards_path: str = "config/naming_standards.yml"
    execution_mode: str = "databricks_job"  # databricks_job | local
    gate_environments: dict[str, str] = field(default_factory=lambda: {"main": "dev", "release": "qa"})
    fail_on_warnings: bool = False
    null_ratio_warn_threshold: float = 0.95
    treat_blank_as_null: bool = True
    require_primary_key: bool = True
    sample_limit: int = 5
    global_impact_paths: list[str] = field(default_factory=list)
    allow_fully_null_columns: list[str] = field(default_factory=list)
    severity_overrides: dict[str, str] = field(default_factory=dict)
    poll_interval_seconds: int = 20
    timeout_minutes: int = 60
    max_findings_in_comment: int = 50

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ValidationConfig:
        base = cls()
        mode = _opt_str(d.get("execution_mode")) or base.execution_mode
        if mode not in ("databricks_job", "local"):
            raise ConfigError("validation.execution_mode must be 'databricks_job' or 'local'")
        threshold = float(d.get("null_ratio_warn_threshold", base.null_ratio_warn_threshold))
        if not 0 < threshold <= 1:
            raise ConfigError("validation.null_ratio_warn_threshold must be in (0, 1]")
        return cls(
            contracts_dir=_opt_str(d.get("contracts_dir")) or base.contracts_dir,
            naming_standards_path=_opt_str(d.get("naming_standards_path")) or base.naming_standards_path,
            execution_mode=mode,
            gate_environments=dict(d.get("gate_environments") or base.gate_environments),
            fail_on_warnings=_bool(d.get("fail_on_warnings"), base.fail_on_warnings),
            null_ratio_warn_threshold=threshold,
            treat_blank_as_null=_bool(d.get("treat_blank_as_null"), base.treat_blank_as_null),
            require_primary_key=_bool(d.get("require_primary_key"), base.require_primary_key),
            sample_limit=int(d.get("sample_limit", base.sample_limit)),
            global_impact_paths=list(d.get("global_impact_paths") or []),
            allow_fully_null_columns=[c.lower() for c in d.get("allow_fully_null_columns") or []],
            severity_overrides={k: str(v).lower() for k, v in (d.get("severity_overrides") or {}).items()},
            poll_interval_seconds=int(d.get("poll_interval_seconds", base.poll_interval_seconds)),
            timeout_minutes=int(d.get("timeout_minutes", base.timeout_minutes)),
            max_findings_in_comment=int(d.get("max_findings_in_comment", base.max_findings_in_comment)),
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialisable subset shipped to the Databricks validation job."""
        return {
            "fail_on_warnings": self.fail_on_warnings,
            "null_ratio_warn_threshold": self.null_ratio_warn_threshold,
            "treat_blank_as_null": self.treat_blank_as_null,
            "require_primary_key": self.require_primary_key,
            "sample_limit": self.sample_limit,
            "allow_fully_null_columns": self.allow_fully_null_columns,
            "severity_overrides": self.severity_overrides,
            "execution_mode": "local",
        }


@dataclass
class QaPromotionConfig:
    merge_strategy: str = "squash"
    delete_source_branch: bool = True
    transition_work_items: bool = True
    require_work_item: bool = True
    auto_complete: bool = True
    auto_promote_on_push: bool = False
    environment: str = "qa"  # workspace that `main` deploys to
    certify_profile: str = "full"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> QaPromotionConfig:
        base = cls()
        return cls(
            merge_strategy=_opt_str(d.get("merge_strategy")) or base.merge_strategy,
            delete_source_branch=_bool(d.get("delete_source_branch"), base.delete_source_branch),
            transition_work_items=_bool(d.get("transition_work_items"), base.transition_work_items),
            require_work_item=_bool(d.get("require_work_item"), base.require_work_item),
            auto_complete=_bool(d.get("auto_complete"), base.auto_complete),
            auto_promote_on_push=_bool(d.get("auto_promote_on_push"), base.auto_promote_on_push),
            environment=_opt_str(d.get("environment")) or base.environment,
            certify_profile=_opt_str(d.get("certify_profile")) or base.certify_profile,
        )


@dataclass
class ChangeWindow:
    timezone: str = "UTC"
    days: list[str] = field(default_factory=lambda: ["Mon", "Tue", "Wed", "Thu"])
    start: str = "09:00"
    end: str = "17:00"


@dataclass
class FreezePeriod:
    start: date
    end: date
    reason: str = ""


@dataclass
class ReleaseConfig:
    merge_strategy: str = "noFastForward"
    tag_prefix: str = "v"
    initial_version: str = "0.1.0"
    change_window: ChangeWindow | None = None
    freeze_periods: list[FreezePeriod] = field(default_factory=list)
    revalidate_in_qa: bool = True
    high_risk_reviewer_ids: list[str] = field(default_factory=list)
    verify_profile: str = "schema_only"
    environment: str = "prod"  # workspace that `release` deploys to
    qa_environment: str = "qa"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ReleaseConfig:
        base = cls()
        window = None
        if d.get("change_window"):
            w = d["change_window"]
            window = ChangeWindow(
                timezone=_opt_str(w.get("timezone")) or "UTC",
                days=[str(x)[:3].title() for x in (w.get("days") or ChangeWindow().days)],
                start=_opt_str(w.get("start")) or "09:00",
                end=_opt_str(w.get("end")) or "17:00",
            )
        freezes = []
        for f in d.get("freeze_periods") or []:
            freezes.append(
                FreezePeriod(
                    start=_to_date(f["start"]), end=_to_date(f["end"]), reason=_opt_str(f.get("reason")) or ""
                )
            )
        return cls(
            merge_strategy=_opt_str(d.get("merge_strategy")) or base.merge_strategy,
            tag_prefix=_opt_str(d.get("tag_prefix")) or base.tag_prefix,
            initial_version=_opt_str(d.get("initial_version")) or base.initial_version,
            change_window=window,
            freeze_periods=freezes,
            revalidate_in_qa=_bool(d.get("revalidate_in_qa"), base.revalidate_in_qa),
            high_risk_reviewer_ids=[
                x for x in (_opt_str(v) for v in d.get("high_risk_reviewer_ids") or []) if x
            ],
            verify_profile=_opt_str(d.get("verify_profile")) or base.verify_profile,
            environment=_opt_str(d.get("environment")) or base.environment,
            qa_environment=_opt_str(d.get("qa_environment")) or base.qa_environment,
        )


def _to_date(value: Any) -> date:
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


MODEL_KINDS = ("foundry_openai", "foundry_anthropic", "langchain")
STRUCTURED_OUTPUT_METHODS = ("function_calling", "json_schema", "json_mode")
LLM_TASKS = ("remediation", "pr_description", "release_assessment")


@dataclass
class ModelConfig:
    """A logical model name mapped to a deployment.

    kind:
      foundry_openai     Azure AI Foundry, OpenAI-compatible API: GPT, Mistral, Llama, DeepSeek, Cohere, ...
      foundry_anthropic  Azure AI Foundry, Anthropic Messages API: Claude
      langchain          any LangChain provider via init_chat_model("provider:model"), e.g. Databricks serving
    """

    name: str
    kind: str
    deployment: str
    endpoint: str | None = None  # overrides llm.project_endpoint for this model
    max_tokens: int | None = 4096
    timeout: float = 120.0
    max_retries: int = 2
    structured_output: str = "function_calling"
    params: dict[str, Any] = field(
        default_factory=dict
    )  # provider-specific (temperature, reasoning_effort, ...)

    @classmethod
    def from_dict(cls, name: str, d: dict[str, Any]) -> ModelConfig:
        kind = (_opt_str(d.get("kind")) or "").lower()
        if kind not in MODEL_KINDS:
            raise ConfigError(f"llm.models.{name}.kind must be one of {MODEL_KINDS}")
        deployment = _opt_str(d.get("deployment"))
        if not deployment:
            raise ConfigError(f"llm.models.{name}.deployment is required")
        # Claude: native JSON-schema structured outputs. Newer Claude models reject the *forced* tool
        # choice that "function_calling" relies on, and Foundry deployment names hide the model version.
        default_method = "json_schema" if kind == "foundry_anthropic" else "function_calling"
        method = (_opt_str(d.get("structured_output")) or default_method).lower()
        if method not in STRUCTURED_OUTPUT_METHODS:
            raise ConfigError(
                f"llm.models.{name}.structured_output must be one of {STRUCTURED_OUTPUT_METHODS}"
            )
        return cls(
            name=name,
            kind=kind,
            deployment=deployment,
            endpoint=_opt_str(d.get("endpoint")),
            max_tokens=_opt_int(d.get("max_tokens")) if d.get("max_tokens") is not None else 4096,
            timeout=float(d.get("timeout", 120.0)),
            max_retries=int(d.get("max_retries", 2)),
            structured_output=method,
            params=dict(d.get("params") or {}),
        )


@dataclass
class TaskConfig:
    """Which model handles a task, with ordered fallbacks (used when the primary errors or times out)."""

    model: str
    fallbacks: list[str] = field(default_factory=list)
    use_tools: bool = False  # remediation only: let the model investigate with read-only tools
    max_tool_rounds: int = 4  # investigation rounds (each round may call several tools)

    @classmethod
    def from_dict(cls, task: str, d: dict[str, Any]) -> TaskConfig:
        model = _opt_str(d.get("model"))
        if not model:
            raise ConfigError(f"llm.tasks.{task}.model is required")
        return cls(
            model=model,
            fallbacks=[f for f in (_opt_str(x) for x in d.get("fallbacks") or []) if f and f != model],
            use_tools=_bool(d.get("use_tools"), False),
            max_tool_rounds=int(d.get("max_tool_rounds", 4)),
        )


@dataclass
class LlmConfig:
    enabled: bool = False
    project_endpoint: str | None = None  # https://<resource>.services.ai.azure.com/api/projects/<project>
    auth: str = "entra"  # entra (managed identity / workload identity / az login) | api_key
    api_key_env: str = "AZURE_AI_FOUNDRY_API_KEY"
    models: dict[str, ModelConfig] = field(default_factory=dict)
    tasks: dict[str, TaskConfig] = field(default_factory=dict)
    share_sample_values: bool = False
    share_source_code: bool = True
    tracing: str = "none"  # none | azure_monitor

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LlmConfig:
        base = cls()
        auth = (_opt_str(d.get("auth")) or base.auth).lower()
        if auth not in ("entra", "api_key"):
            raise ConfigError("llm.auth must be 'entra' or 'api_key'")
        tracing = (_opt_str(d.get("tracing")) or base.tracing).lower()
        if tracing not in ("none", "azure_monitor"):
            raise ConfigError("llm.tracing must be 'none' or 'azure_monitor'")
        models = {name: ModelConfig.from_dict(name, m or {}) for name, m in (d.get("models") or {}).items()}
        tasks = {}
        for task, t in (d.get("tasks") or {}).items():
            if task not in LLM_TASKS:
                raise ConfigError(f"Unknown llm task '{task}'. Known: {LLM_TASKS}")
            spec = TaskConfig.from_dict(task, t or {})
            for ref in [spec.model, *spec.fallbacks]:
                if ref not in models:
                    raise ConfigError(
                        f"llm.tasks.{task} refers to unknown model '{ref}'. Known: {sorted(models)}"
                    )
            tasks[task] = spec
        return cls(
            enabled=_bool(d.get("enabled"), base.enabled),
            project_endpoint=(_opt_str(d.get("project_endpoint")) or "").rstrip("/") or None,
            auth=auth,
            api_key_env=_opt_str(d.get("api_key_env")) or base.api_key_env,
            models=models,
            tasks=tasks,
            share_sample_values=_bool(d.get("share_sample_values"), base.share_sample_values),
            share_source_code=_bool(d.get("share_source_code"), base.share_source_code),
            tracing=tracing,
        )


@dataclass
class NotificationConfig:
    teams_webhook_url: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NotificationConfig:
        return cls(teams_webhook_url=_opt_str(d.get("teams_webhook_url")))


@dataclass
class AuditConfig:
    sink: str = "jsonl"  # jsonl | databricks_sql | none
    jsonl_path: str = "agentic-artifacts/audit.jsonl"
    environment: str = "qa"  # whose SQL warehouse stores the audit table
    warehouse_id: str | None = None
    table: str = "ops.agentic.audit_events"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> AuditConfig:
        base = cls()
        sink = (_opt_str(d.get("sink")) or base.sink).lower()
        if sink not in ("jsonl", "databricks_sql", "none"):
            raise ConfigError("audit.sink must be one of jsonl | databricks_sql | none")
        return cls(
            sink=sink,
            jsonl_path=_opt_str(d.get("jsonl_path")) or base.jsonl_path,
            environment=_opt_str(d.get("environment")) or base.environment,
            warehouse_id=_opt_str(d.get("warehouse_id")),
            table=_opt_str(d.get("table")) or base.table,
        )


@dataclass
class AppConfig:
    azure_devops: AzureDevOpsConfig
    environments: dict[str, EnvironmentConfig]
    branches: BranchConfig = field(default_factory=BranchConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    qa_promotion: QaPromotionConfig = field(default_factory=QaPromotionConfig)
    release: ReleaseConfig = field(default_factory=ReleaseConfig)
    llm: LlmConfig = field(default_factory=LlmConfig)
    notifications: NotificationConfig = field(default_factory=NotificationConfig)
    audit: AuditConfig = field(default_factory=AuditConfig)

    def environment(self, name: str) -> EnvironmentConfig:
        try:
            return self.environments[name]
        except KeyError as exc:
            raise ConfigError(f"Unknown environment '{name}'. Known: {sorted(self.environments)}") from exc

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AppConfig:
        data = _interpolate(raw or {})
        envs = {
            name: EnvironmentConfig.from_dict(name, d or {})
            for name, d in (data.get("environments") or {}).items()
        }
        if not envs:
            raise ConfigError("At least one entry under 'environments' is required")
        cfg = cls(
            azure_devops=AzureDevOpsConfig.from_dict(data.get("azure_devops") or {}),
            environments=envs,
            branches=BranchConfig.from_dict(data.get("branches") or {}),
            validation=ValidationConfig.from_dict(data.get("validation") or {}),
            qa_promotion=QaPromotionConfig.from_dict(data.get("qa_promotion") or {}),
            release=ReleaseConfig.from_dict(data.get("release") or {}),
            llm=LlmConfig.from_dict(data.get("llm") or {}),
            notifications=NotificationConfig.from_dict(data.get("notifications") or {}),
            audit=AuditConfig.from_dict(data.get("audit") or {}),
        )
        for branch, env in cfg.validation.gate_environments.items():
            if env not in envs:
                raise ConfigError(
                    f"validation.gate_environments.{branch} points to unknown environment '{env}'"
                )
        for key, env in (
            ("qa_promotion.environment", cfg.qa_promotion.environment),
            ("release.environment", cfg.release.environment),
            ("release.qa_environment", cfg.release.qa_environment),
        ):
            if env not in envs:
                raise ConfigError(f"{key} points to unknown environment '{env}'")
        return cfg


def load_config(path: str | Path) -> AppConfig:
    with open(path, encoding="utf-8") as fh:
        return AppConfig.from_dict(yaml.safe_load(fh) or {})
