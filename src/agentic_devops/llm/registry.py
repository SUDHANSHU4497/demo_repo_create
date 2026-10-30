"""Model registry: logical model names -> Azure AI Foundry deployments, routed per task with fallbacks.

Switching models is configuration only (``config/agentic.yml`` -> ``llm.models`` / ``llm.tasks``):

    models:
      claude:  {kind: foundry_anthropic, deployment: claude-opus-5-5}
      gpt:     {kind: foundry_openai,    deployment: gpt-4.1}
      mistral: {kind: foundry_openai,    deployment: Mistral-Large-2411}
    tasks:
      remediation: {model: claude, fallbacks: [gpt]}

Every model is a LangChain ``BaseChatModel``, so agents never depend on a provider SDK.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.runnables import Runnable

from agentic_devops.config import LlmConfig, ModelConfig, TaskConfig

log = logging.getLogger(__name__)

ModelBuilder = Callable[[ModelConfig, LlmConfig, Any], BaseChatModel]


def foundry_credential(config: LlmConfig) -> Any:
    """Entra ID (workload identity / managed identity / az login) or an API key from the environment."""
    if config.auth == "api_key":
        key = os.environ.get(config.api_key_env)
        if not key:
            raise RuntimeError(f"llm.auth=api_key but {config.api_key_env} is not set")
        return key
    from azure.identity import DefaultAzureCredential

    return DefaultAzureCredential(exclude_interactive_browser_credential=True)


def _endpoint_kwargs(spec: ModelConfig, config: LlmConfig) -> dict[str, Any]:
    if spec.endpoint:
        return {"endpoint": spec.endpoint}
    if config.project_endpoint:
        return {"project_endpoint": config.project_endpoint}
    return {}  # the integration falls back to AZURE_AI_PROJECT_ENDPOINT


def build_chat_model(spec: ModelConfig, config: LlmConfig, credential: Any) -> BaseChatModel:
    """Create the LangChain chat model for one configured deployment (no network calls here)."""
    if spec.kind == "foundry_openai":
        from langchain_azure_ai.chat_models import AzureAIOpenAIApiChatModel

        kwargs: dict[str, Any] = {
            "model": spec.deployment,
            "credential": credential,
            "max_tokens": spec.max_tokens,
            "timeout": spec.timeout,
            "max_retries": spec.max_retries,
            # Chat Completions works for every Foundry model (GPT, Mistral, Llama, DeepSeek...);
            # set params.use_responses_api: true for OpenAI-only Responses features.
            "use_responses_api": False,
            **_endpoint_kwargs(spec, config),
        }
        kwargs.update(spec.params)
        return AzureAIOpenAIApiChatModel(**kwargs)

    if spec.kind == "foundry_anthropic":
        from langchain_azure_ai.chat_models import AzureAIAnthropicChatModel

        kwargs = {
            "model": spec.deployment,
            "credential": credential,
            "max_tokens": spec.max_tokens or 4096,
            "timeout": spec.timeout,
            "max_retries": spec.max_retries,
            **_endpoint_kwargs(spec, config),
        }
        kwargs.update(spec.params)
        return AzureAIAnthropicChatModel(**kwargs)

    if spec.kind == "langchain":
        from langchain.chat_models import init_chat_model

        return init_chat_model(spec.deployment, **spec.params)

    raise ValueError(f"Unknown model kind {spec.kind!r}")


class ModelRegistry:
    def __init__(
        self,
        config: LlmConfig,
        builder: ModelBuilder = build_chat_model,
        credential_factory: Callable[[LlmConfig], Any] = foundry_credential,
    ):
        self.config = config
        self.builder = builder
        self.credential_factory = credential_factory
        self._credential: Any = None
        self._models: dict[str, BaseChatModel] = {}

    def task(self, task: str) -> TaskConfig | None:
        return self.config.tasks.get(task) if self.config.enabled else None

    def chat_model(self, name: str) -> BaseChatModel:
        if name not in self._models:
            spec = self.config.models[name]
            if self._credential is None and spec.kind != "langchain":
                self._credential = self.credential_factory(self.config)
            self._models[name] = self.builder(spec, self.config, self._credential)
            log.info("LLM model '%s' -> %s deployment '%s'", name, spec.kind, spec.deployment)
        return self._models[name]

    def _chain(self, names: list[str], wrap: Callable[[str, BaseChatModel], Runnable]) -> Runnable | None:
        runnables = []
        for name in names:
            try:
                runnables.append(wrap(name, self.chat_model(name)))
            except Exception:  # noqa: BLE001 - a broken model config must not break the gate
                log.exception("Could not initialise model '%s'", name)
        if not runnables:
            return None
        primary, *rest = runnables
        return primary.with_fallbacks(rest) if rest else primary

    def structured(self, task: str, schema: type) -> Runnable | None:
        """Model for ``task`` returning ``schema`` instances, with the task's fallback models."""
        spec = self.task(task)
        if spec is None:
            return None
        return self._chain(
            [spec.model, *spec.fallbacks],
            lambda name, m: m.with_structured_output(
                schema, method=self.config.models[name].structured_output
            ),
        )

    def with_tools(self, task: str, tools: list[Any]) -> Runnable | None:
        """Tool-calling model for ``task`` (used by the remediation investigation loop)."""
        spec = self.task(task)
        if spec is None:
            return None
        return self._chain([spec.model, *spec.fallbacks], lambda name, m: m.bind_tools(tools))

    def describe(self) -> dict[str, Any]:
        return {
            task: {
                "model": t.model,
                "deployment": self.config.models[t.model].deployment,
                "fallbacks": t.fallbacks,
            }
            for task, t in self.config.tasks.items()
        }


def check_models(registry: ModelRegistry) -> dict[str, str]:
    """Send a tiny prompt to every configured model: verifies endpoint, deployment name and RBAC."""
    results: dict[str, str] = {}
    for name in registry.config.models:
        try:
            reply = registry.chat_model(name).invoke("Reply with the single word OK.")
            results[name] = "ok" if str(reply.text).strip() else "empty reply"
        except Exception as exc:  # noqa: BLE001
            results[name] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return results


def tracing_callbacks(config: LlmConfig) -> list[Any]:
    """Optional OpenTelemetry GenAI spans to Application Insights (prompt/response content not recorded)."""
    if config.tracing != "azure_monitor":
        return []
    conn = os.environ.get("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if not conn:
        log.warning("llm.tracing=azure_monitor but APPLICATIONINSIGHTS_CONNECTION_STRING is not set")
        return []
    try:
        from langchain_azure_ai.callbacks.tracers import AzureAIOpenTelemetryTracer

        return [
            AzureAIOpenTelemetryTracer(
                connection_string=conn,
                enable_content_recording=False,
                name="agentic-devops",
                trace_all_langgraph_nodes=True,
            )
        ]
    except Exception:  # noqa: BLE001
        log.exception("Tracing disabled: install 'agentic-devops[tracing]'")
        return []
