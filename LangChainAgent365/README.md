# `LangChainAgent365/` — LangChain `AgentExecutor` integrated with Microsoft Agent 365

This folder ports the [`BasicAgent365/`](../BasicAgent365/) sample (which used the raw OpenAI Responses API) to LangChain's **classic `AgentExecutor`** agent loop while keeping the same Agent 365 observability surface area.

> **About LangChain 1.0:** in LangChain 1.0 the imperative `AgentExecutor` + `create_tool_calling_agent` helpers were moved out of the core `langchain` package into the separate **[`langchain-classic`](https://pypi.org/project/langchain-classic/)** distribution — the modern `langchain` package now exposes a graph-based `langchain.agents.create_agent` prebuilt that is *literally* a LangGraph state machine under the hood. This sample deliberately stays on the **classic** pattern because:
> 1. It is the LangChain idiom that most engineers learned from 0.x tutorials and Microsoft's own LangChain quick-starts.
> 2. It contrasts cleanly with [`../LangGraphAgent365/`](../LangGraphAgent365/), which shows the same agent built as an explicit `StateGraph` — i.e. the **state-machine** alternative.
>
> If you only need the modern LangChain 1.x prebuilt agent, see the LangGraph sample — the prebuilt and the explicit graph are isomorphic and the A365 bridge below works identically.

| Concern | Same as `BasicAgent365/`? |
|---------|---------------------------|
| 3-hop FMI/FIC token chain | ✅ identical — same `observability_token_service.py` |
| In-memory token cache | ✅ identical — same `token_cache.py` |
| `import_a365_config.py` bootstrap | ✅ identical script |
| Spans emitted (`invoke_agent` / `chat` / `execute_tool`) | ✅ same semantic conventions |
| **How those spans are produced** | ❌ different — native LangChain auto-instrumentation, no manual `InvokeAgentScope` / `InferenceScope` / `ExecuteToolScope` |
| **Agent loop** | ❌ different — `AgentExecutor` + `create_tool_calling_agent` instead of a hand-rolled Responses API loop |

If you have already read `BasicAgent365/README.md` and `BasicAgent365/docs/INTEGRATION_GUIDE.md`, you only need [§ The wiring](#the-wiring--langchain-auto-instrumentation) and [§ Run it](#run-it) below.

---

## Layout

```text
LangChainAgent365/
├── agent_basic.py                 # Base LangChain agent — no telemetry
├── agent_a365.py                  # Same agent + Agent 365 instrumentation
├── observability_token_service.py # 3-hop FMI/FIC token mint (copy of BasicAgent365)
├── token_cache.py                 # In-memory bearer cache (copy of BasicAgent365)
├── import_a365_config.py          # Bridges a365.generated.config.json → .env
├── requirements.txt               # Base LangChain stack
├── requirements-a365.txt          # + microsoft-opentelemetry + msal
├── .env.example
├── .gitignore
└── README.md                      # this file
```

## Provisioning

Provisioning is **identical** to `BasicAgent365/`. Do not duplicate the work.

1. Follow [`../BasicAgent365/README.md` → Phase 1 (host machine)](../BasicAgent365/README.md) to run `a365 setup all` and grant the `Agent365.Observability.OtelWrite` Application permission. This produces `a365.generated.config.json` and prints the plaintext Blueprint client secret **once**.
2. Copy `a365.generated.config.json` into **this** folder.
3. Run `python import_a365_config.py` here — it writes a local `.env` with `chmod 600`.

> Tip: if you already ran `import_a365_config.py` inside `BasicAgent365/`, you can simply `cp ../BasicAgent365/.env .` and adjust `AGENT365_AGENT_NAME` to `inditex-langchain-A365`. Same tenant, same agent identity, same blueprint secret — the only thing different across the three sample folders is the agent's display name.

## Install

```bash
# Base LangChain agent only:
pip install -r requirements.txt

# With Agent 365 telemetry (also includes the base deps):
pip install --pre -r requirements-a365.txt
```

## Run it

```bash
# Base LangChain agent (Azure OpenAI + tool calling, no telemetry):
az login --allow-no-subscriptions
python agent_basic.py

# Same agent + full Agent 365 telemetry:
python agent_a365.py
```

The base agent only needs the `AZURE_OPENAI_*` env vars. The A365 version additionally needs all `AGENT365_*` vars and the `Agent365.Observability.OtelWrite` Application role assignment on the agent identity SP (otherwise the token mint succeeds but the ingest API returns 403). See [`../BasicAgent365/docs/INTEGRATION_GUIDE.md`](../BasicAgent365/docs/INTEGRATION_GUIDE.md) § 15 for the manual remediation snippet.

## The wiring — LangChain auto-instrumentation

`microsoft-opentelemetry` 1.2.0+ ships a built-in `LangChainInstrumentor` (at `microsoft.opentelemetry._genai._langchain._tracer_instrumentor`) that patches LangChain's `Runnable` / `BaseChatModel` / `BaseTool` callbacks at import time and emits OTel GenAI semantic-convention spans:

| LangChain event                  | Auto-emitted span                                        |
|----------------------------------|----------------------------------------------------------|
| `Agent.invoke(...)` (or graph)   | `invoke_agent <AGENT_NAME>` (`gen_ai.operation.name=invoke_agent`) |
| `ChatModel.invoke(...)`          | `chat <MODEL>` (`gen_ai.operation.name=chat`, `SpanKind.CLIENT`) |
| `Tool.invoke(...)`               | `execute_tool <tool_name>` (`gen_ai.operation.name=execute_tool`)|

We enable it by passing `instrumentation_options` to the distro — exact pattern parity with the official Node.js LangChain sample (`useMicrosoftOpenTelemetry({ instrumentationOptions: { langchain: {} } })`):

```python
use_microsoft_opentelemetry(
    enable_a365=True,
    enable_azure_monitor=False,
    a365_use_s2s_endpoint=True,
    a365_enable_observability_exporter=True,
    a365_token_resolver=_token_resolver,
    instrumentation_options={
        "langchain": {
            "enabled":           True,
            "agent_id":          AGENT_ID,
            "agent_name":        AGENT_NAME,
            "agent_description": AGENT_DESC,
        },
    },
)
```

### Why we still register one extra `A365SpanProcessor`

The A365 ingest exporter only ships spans that have both `microsoft.tenant.id` **and** `gen_ai.agent.id` set as attributes (see `microsoft.opentelemetry.a365.core.exporters.utils.filter_and_partition_by_identity`). The distro registers an `A365SpanProcessor()` with **no arguments** — it relies on baggage propagation from manual `InvokeAgentScope` to set these. Since we use the auto-instrumentor (no manual Scope, no baggage), we register **one extra processor** seeded with our static identity:

```python
from microsoft.opentelemetry.a365.core.exporters.span_processor import A365SpanProcessor
_provider.add_span_processor(
    A365SpanProcessor(tenant_id=TENANT_ID, agent_id=AGENT_ID),
)
```

It only sets the two keys when they are absent (`existing` check), so it composes cleanly with the distro's own processor.

That is the entire integration surface. The base agent in `agent_basic.py` is the standard LangChain `AgentExecutor` pattern, untouched.

## Files that differ from `BasicAgent365/`

`agent_basic.py` and `agent_a365.py` are completely rewritten for the LangChain stack. The rest is verbatim copies (token cache, observability service, import script, `.env.example`, `.gitignore`) so the folder is fully self-contained and you can copy it into another repo without dragging `BasicAgent365/` along.

## Anchors

Every Agent-365-specific line in `agent_a365.py` is prefixed with `# [A365] ...`. Search for that marker to enumerate the entire integration surface:

```bash
grep -n "# \[A365\]" agent_a365.py
```

## Expected output

A successful run of `python agent_a365.py` on a licensed tenant logs roughly:

```
INFO agent_a365: Acquiring Agent 365 observability token via 3-hop FMI chain...
INFO agent_a365: Observability token cached successfully.
==================================================
LANGCHAIN AGENT — Agent 365 integrated
==================================================
You: What time is it in Madrid?
> Entering new AgentExecutor chain...
... LangChain tool-calling output ...
> Finished chain.

Agent: It's 16:14 (4:14 PM) in Madrid right now.
```

You can confirm the spans are actually leaving the process by running with `LOGLEVEL=DEBUG`:

```bash
LOGLEVEL=DEBUG python agent_a365.py
```

Look for `microsoft.opentelemetry.a365.core.exporters.agent365_exporter` lines reporting HTTP 200 from the ingest endpoint.

> **Reminder — `tenant_not_licensed`:** on a tenant without a Microsoft Agent 365 SKU you will still see HTTP 200, but the response body shows `"sinks":{"flashpoint":"rejected","sentinel":"rejected","esp":"rejected","reason":"tenant_not_licensed"}` and nothing appears in the admin center. This is not a code bug. See `BasicAgent365/docs/INTEGRATION_GUIDE.md` § 15.

## Next steps

- [`../LangGraphAgent365/`](../LangGraphAgent365/) — same idea but the agent is built as an **explicit `StateGraph`** (no prebuilt loop, no `AgentExecutor`). Showcases LangGraph's state-machine primitives. The A365 wiring is identical (LangGraph nodes are LangChain Runnables, so the same auto-instrumentor traces them).
- [`../BasicAgent365/docs/INTEGRATION_GUIDE.md`](../BasicAgent365/docs/INTEGRATION_GUIDE.md) — the long-form rationale: threat model, FMI identity model, three-hop token chain, gotchas.
