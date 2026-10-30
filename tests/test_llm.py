from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import Field

from agentic_devops.config import ConfigError, LlmConfig, ValidationConfig
from agentic_devops.llm.advisor import LangChainAdvisor, build_advisor
from agentic_devops.llm.base import NullAdvisor, RemediationContext
from agentic_devops.llm.registry import ModelRegistry, build_chat_model, check_models
from agentic_devops.validation.backends import PandasBackend
from agentic_devops.validation.contracts import contract_from_dict
from agentic_devops.validation.engine import ValidationEngine
from agentic_devops.validation.naming import NamingStandards


class ScriptedChatModel(BaseChatModel):
    """Returns queued AIMessages; records every call. Supports tool binding like a real model."""

    script: list[Any] = Field(default_factory=list)
    calls: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def with_structured_output(self, schema, *, method="function_calling", **kwargs):
        if method == "json_schema":  # like ChatAnthropic: native output format, JSON parsed from the text
            return self.bind(output_format="json_schema") | PydanticOutputParser(pydantic_object=schema)
        return super().with_structured_output(schema, method=method, **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return ChatResult(generations=[ChatGeneration(message=item)])


def tool_call(name: str, args: dict[str, Any], call_id: str = "c1") -> AIMessage:
    return AIMessage(
        content="", tool_calls=[{"name": name, "args": args, "id": call_id, "type": "tool_call"}]
    )


def llm_config(**tasks) -> LlmConfig:
    return LlmConfig.from_dict(
        {
            "enabled": True,
            "project_endpoint": "https://res.services.ai.azure.com/api/projects/p",
            "models": {
                "claude": {"kind": "foundry_anthropic", "deployment": "claude-opus-5-5"},
                "gpt": {"kind": "foundry_openai", "deployment": "gpt-4.1"},
            },
            "tasks": tasks
            or {
                "remediation": {"model": "claude", "fallbacks": ["gpt"]},
                "pr_description": {"model": "gpt"},
                "release_assessment": {"model": "claude", "fallbacks": ["gpt"]},
            },
        }
    )


def registry_with(models: dict[str, BaseChatModel], config: LlmConfig) -> ModelRegistry:
    return ModelRegistry(
        config, builder=lambda spec, cfg, cred: models[spec.name], credential_factory=lambda cfg: object()
    )


def failing_report():
    df = pd.DataFrame({"order_id": [1, 1], "cust_id": [5, 6]})
    contract = contract_from_dict(
        {"table": "sales.orders", "primary_key": ["order_id"], "sources": ["src/pipelines/sales/orders/**"]}
    )
    engine = ValidationEngine(
        PandasBackend({"dev.sales.orders": df}),
        ValidationConfig(),
        NamingStandards(abbreviations={"cust": "customer"}),
    )
    return engine.validate([contract], "dev", "dev"), contract


PLAN = {
    "summary": "Duplicate order ids <script>",
    "actions": [
        {
            "title": "Dedupe",
            "detail": "Use ROW_NUMBER ![x](http://evil/x.png)",
            "language": "sql",
            "code": "SELECT 1 ``` DROP",
        }
    ],
}


# ----------------------------------------------------------------------------- registry
def test_registry_builds_foundry_models_offline():
    cfg = llm_config()

    class NoNetworkCredential:
        def get_token(self, *a, **k):
            raise AssertionError("no token should be requested when building models")

    claude = build_chat_model(cfg.models["claude"], cfg, NoNetworkCredential())
    gpt = build_chat_model(cfg.models["gpt"], cfg, NoNetworkCredential())
    assert type(claude).__name__ == "AzureAIAnthropicChatModel" and claude.model == "claude-opus-5-5"
    assert type(gpt).__name__ == "AzureAIOpenAIApiChatModel" and gpt.model_name == "gpt-4.1"
    assert gpt.use_responses_api is False  # chat completions: works for every Foundry model
    assert cfg.models["claude"].structured_output == "json_schema"  # no forced tool choice for Claude
    assert cfg.models["gpt"].structured_output == "function_calling"


def test_switching_a_task_model_is_config_only():
    gpt, claude = (
        ScriptedChatModel(
            script=[
                tool_call(
                    "PullRequestDraft",
                    {
                        "title": "feat(sales): add orders",
                        "summary": "s",
                        "changes": ["c"],
                        "review_focus": [],
                    },
                )
            ]
        ),
        ScriptedChatModel(),
    )
    cfg = llm_config(pr_description={"model": "gpt"})
    pr = LangChainAdvisor(registry_with({"gpt": gpt, "claude": claude}, cfg), cfg).pr_text({"x": 1})
    assert pr.title == "feat(sales): add orders" and len(gpt.calls) == 1 and claude.calls == []

    cfg = llm_config(pr_description={"model": "claude"})
    claude.script.append(
        AIMessage(
            content=json.dumps(
                {  # Claude answers via JSON-schema output
                    "title": "fix(sales): y",
                    "summary": "s",
                    "changes": [],
                    "review_focus": [],
                }
            )
        )
    )
    pr = LangChainAdvisor(registry_with({"gpt": gpt, "claude": claude}, cfg), cfg).pr_text({"x": 1})
    assert pr.title == "fix(sales): y" and len(claude.calls) == 1


def test_fallback_model_used_when_primary_fails():
    claude = ScriptedChatModel(script=[TimeoutError("primary down")])
    gpt = ScriptedChatModel(script=[tool_call("RemediationPlan", PLAN)])
    cfg = llm_config()
    report, _ = failing_report()
    md = LangChainAdvisor(registry_with({"claude": claude, "gpt": gpt}, cfg), cfg).remediation(report)
    assert md and "Dedupe" in md
    assert len(claude.calls) == 1 and len(gpt.calls) == 1


def test_all_models_failing_degrades_to_none():
    cfg = llm_config()
    models = {
        "claude": ScriptedChatModel(script=[RuntimeError("x")]),
        "gpt": ScriptedChatModel(script=[RuntimeError("y")]),
    }
    report, _ = failing_report()
    assert LangChainAdvisor(registry_with(models, cfg), cfg).remediation(report) is None


def test_remediation_guardrails_untrusted_input_and_no_row_values():
    gpt = ScriptedChatModel(script=[tool_call("RemediationPlan", PLAN)])
    cfg = llm_config(remediation={"model": "gpt"})
    report, _ = failing_report()
    md = LangChainAdvisor(registry_with({"gpt": gpt}, cfg), cfg).remediation(report)
    sent = gpt.calls[0]["messages"][-1].content
    assert "<untrusted_input>" in sent and "sample_duplicates" not in sent
    assert "&lt;script&gt;" in md and "evil" not in md  # sanitised prose, no remote images
    assert "```sql" in md and "``` DROP" not in md


def test_remediation_agent_investigates_with_read_only_tools():
    report, contract = failing_report()
    changed = ["/src/pipelines/sales/orders/build.py"]
    reads = []

    def read_file(path):
        reads.append(path)
        return "df.write.mode('append').saveAsTable('sales.orders')"

    claude = ScriptedChatModel(
        script=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "get_data_contract",
                        "args": {"table": "sales.orders"},
                        "id": "a",
                        "type": "tool_call",
                    },
                    {
                        "name": "read_changed_file",
                        "args": {"path": "src/pipelines/sales/orders/build.py"},
                        "id": "b",
                        "type": "tool_call",
                    },
                ],
            ),
            tool_call("read_changed_file", {"path": "/etc/passwd"}, "c"),
            AIMessage(content="Root cause: append instead of MERGE in build.py"),
            AIMessage(content=json.dumps(PLAN)),  # json_schema structured output for Claude
        ]
    )
    cfg = llm_config(remediation={"model": "claude", "use_tools": True, "max_tool_rounds": 4})
    advisor = LangChainAdvisor(registry_with({"claude": claude}, cfg), cfg)
    ctx = RemediationContext(
        contracts=[contract], naming=NamingStandards(), changed_files=changed, read_file=read_file
    )
    md = advisor.remediation(report, ctx)

    assert md and "Dedupe" in md
    assert reads == ["/src/pipelines/sales/orders/build.py"]  # only PR files are readable
    tools_offered = {t["function"]["name"] for t in claude.calls[0]["kwargs"]["tools"]}
    assert tools_offered == {
        "get_data_contract",
        "get_findings",
        "suggest_column_name",
        "list_changed_files",
        "read_changed_file",
    }
    final_prompt = claude.calls[-1]["messages"][-1].content
    assert "append instead of MERGE" in final_prompt and "saveAsTable" in final_prompt
    assert "Only files changed in this pull request" in final_prompt


def test_tool_rounds_are_capped():
    report, contract = failing_report()
    looping = [tool_call("get_findings", {"table": "sales.orders"}, f"id{i}") for i in range(3)]
    claude = ScriptedChatModel(script=[*looping, AIMessage(content=json.dumps(PLAN))])
    cfg = llm_config(remediation={"model": "claude", "use_tools": True, "max_tool_rounds": 2})
    md = LangChainAdvisor(registry_with({"claude": claude}, cfg), cfg).remediation(
        report, RemediationContext(contracts=[contract])
    )
    investigate_calls = [c for c in claude.calls if "tools" in c["kwargs"]]
    assert len(investigate_calls) == 3  # rounds 1-2 run tools; round 3 wants more but is stopped
    assert "Dedupe" in md  # the final answer is still produced from what was gathered


def test_release_assessment_parses():
    risk = {
        "risk_level": "high",
        "risk_reasons": ["PK change"],
        "highlights": ["h"],
        "data_changes": [],
        "rollback_considerations": ["RESTORE sales.orders"],
    }
    claude = ScriptedChatModel(script=[AIMessage(content=json.dumps(risk))])
    cfg = llm_config()
    ra = LangChainAdvisor(
        registry_with({"claude": claude, "gpt": ScriptedChatModel()}, cfg), cfg
    ).release_assessment({})
    assert ra.risk_level == "high" and "Rollback considerations" in ra.notes_markdown


def test_build_advisor_and_config_validation():
    assert isinstance(build_advisor(LlmConfig()), NullAdvisor)
    assert isinstance(build_advisor(llm_config()), LangChainAdvisor)  # lazy: no credentials needed yet
    with pytest.raises(ConfigError):
        LlmConfig.from_dict({"models": {"a": {"kind": "openai", "deployment": "x"}}})
    with pytest.raises(ConfigError):
        LlmConfig.from_dict({"models": {}, "tasks": {"remediation": {"model": "ghost"}}})
    with pytest.raises(ConfigError):
        LlmConfig.from_dict({"models": {}, "tasks": {"translate": {"model": "x"}}})


def test_check_models_reports_per_model():
    cfg = llm_config()
    models = {
        "claude": ScriptedChatModel(script=[AIMessage(content="OK")]),
        "gpt": ScriptedChatModel(script=[PermissionError("403 RBAC")]),
    }
    result = check_models(registry_with(models, cfg))
    assert result["claude"] == "ok" and result["gpt"].startswith("PermissionError")
    assert isinstance(models["claude"].calls[0]["messages"][0], HumanMessage)
