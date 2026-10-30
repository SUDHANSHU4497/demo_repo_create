# ADR 0005 — LangGraph for agent workflows, LangChain + Azure AI Foundry for models

**Status:** Accepted · **Date:** 2026-10-30 · **Amends:** ADR 0001 (provider details only; the "deterministic gates, LLM advisory" principle is unchanged)

## Context
The organisation has Azure AI Foundry and wants to choose a model per job (Claude, GPT, Mistral, Llama, …) and change that choice as models evolve, without rewriting agents. The first version called one provider's SDK directly.

## Decision
1. **LangGraph** defines every agent workflow as a compiled `StateGraph`:
   * Data Validation, QA Promotion (promote + certify), Release (prepare + finalize) flows.
   * A **supervisor graph** routes CLI commands and Azure DevOps service-hook events to those flows.
   * The **remediation sub-agent** is a LangGraph tool-calling loop (`investigate ⇄ tools`) with read-only tools.
   * All routing between steps is plain Python (`core/flow.py`); no LLM chooses a path. Every step can end the flow with an `AgentResult`. Graphs render to Mermaid (`agentic-devops graph`, `docs/GRAPHS.md`).
2. **LangChain** chat models are the only model interface agents see (`BaseChatModel`, `with_structured_output`, `bind_tools`, `with_fallbacks`).
3. **Azure AI Foundry** hosts the models, reached through `langchain-azure-ai`:
   * `kind: foundry_openai` → `AzureAIOpenAIApiChatModel` (OpenAI-compatible API: GPT, Mistral, Llama, DeepSeek, Cohere…), Chat Completions mode for broad model support.
   * `kind: foundry_anthropic` → `AzureAIAnthropicChatModel` (Claude via the Foundry Anthropic endpoint), JSON-schema structured output by default because newer Claude models reject forced tool choice.
   * One **project endpoint**, **Entra ID** auth (`DefaultAzureCredential`: workload identity in pipelines, `az login` locally); API keys only as an opt-in.
   * `kind: langchain` → `init_chat_model("provider:model")` as an escape hatch for non-Foundry models.
4. A **model registry** maps logical names to deployments and **tasks to models with ordered fallbacks** (`llm.models`, `llm.tasks`). Switching is a config or variable-group change.
5. Structured outputs use **Pydantic schemas**; prompts are provider-neutral; untrusted text stays wrapped; outputs are sanitised; failures fall back to templates.
6. Optional **tracing** via `AzureAIOpenTelemetryTracer` to Application Insights with content recording **off**.

## Alternatives considered
| Option | Why not |
|---|---|
| Direct provider SDKs per model | Every model switch is a code change; duplicated retry/structured-output logic |
| Azure AI Foundry Agent Service (hosted agents) | Ties orchestration to one runtime; our flows must run inside Azure Pipelines with deterministic gates and the same code path in tests |
| Semantic Kernel / AutoGen | Viable, but LangGraph's explicit state graphs match our reconcile-and-gate flows and the team's Python/Databricks stack; LangChain has first-party Foundry integrations |
| LLM-driven routing (agent decides next step) | Violates ADR 0001: gates must be deterministic and auditable |

## Consequences
+ Model choice per task is configuration; fallbacks across model families protect against outages and retirements.
+ Agent flows are inspectable graphs; the same graph runs in pipelines, tests and (later) a hosted service with a LangGraph checkpointer.
+ Entra ID end to end; no model API keys in pipelines.
− New dependencies (`langgraph`, `langchain-core`, `langchain-azure-ai`); pinned to major versions and isolated in the `agents`/`foundry` extras so the Databricks validation job stays dependency-light.
− Structured-output and tool-calling behaviour differs by model; the evaluation plan (PLAN §5) measures it before a model is assigned to a task.
