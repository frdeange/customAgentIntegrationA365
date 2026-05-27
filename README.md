# Inditex A365 — Microsoft Agent 365 PoC

Three self-contained Python samples that show **the same agent** (an Azure OpenAI tool-calling agent with a single `get_current_time` tool) wired into Microsoft Agent 365 observability through three different agent-loop stacks. Each folder is independently runnable and ships with its own README, requirements files, `.env.example`, and a fully gitignored `.env` flow.

The point of the repo is to **isolate the Agent 365 integration surface** so engineers can copy whichever pattern matches their stack into a real product without dragging the others along.

## Pick your stack

| Folder | Agent loop | Best for | Lines of A365-specific code |
|--------|------------|----------|-----------------------------|
| [`BasicAgent365/`](./BasicAgent365/) | Hand-rolled loop on the raw **OpenAI Responses API** | Reference / minimum-viable wire-up. Read this first. | ~50 (anchored with `# [A365]`) |
| [`LangChainAgent365/`](./LangChainAgent365/) | Classic **LangChain `AgentExecutor`** (via [`langchain-classic`](https://pypi.org/project/langchain-classic/)) | Teams already on LangChain 0.x patterns; the most familiar idiom. | ~10 (native `instrumentation_options` + one `A365SpanProcessor`) |
| [`LangGraphAgent365/`](./LangGraphAgent365/) | Explicit **LangGraph `StateGraph`** (`agent → tools → agent → END`) | Teams that need conditional routing, branches, checkpointed memory, or human-in-the-loop. | Identical wiring — graph topology is the only delta |

All three emit the **same span shape** to the Agent 365 ingest endpoint (OTel GenAI semantic conventions):

```
invoke_agent <agent-name>           (gen_ai.operation.name=invoke_agent)
├── chat <model>                     (gen_ai.operation.name=chat,         SpanKind.CLIENT)
├── execute_tool <tool-name>         (gen_ai.operation.name=execute_tool, SpanKind.INTERNAL)
└── chat <model>                     (gen_ai.operation.name=chat,         SpanKind.CLIENT)
```

The LangChain / LangGraph samples use the **native LangChain auto-instrumentation** shipped in `microsoft-opentelemetry` 1.2.0 (`instrumentation_options={"langchain": {...}}`) — exact pattern parity with Microsoft's official Node.js LangChain sample. The Basic sample uses the manual `InvokeAgentScope` / `InferenceScope` / `ExecuteToolScope` classes because it doesn't have a LangChain runtime to hook into.

The downstream security/compliance consumer in the Microsoft 365 admin center cannot tell which framework produced the spans.

## Repo layout

```text
inditexA365/
├── BasicAgent365/                # Reference (raw Responses API) — start here
│   ├── agent_basic.py            # Base agent — no telemetry
│   ├── agent_a365.py             # Same agent + Agent 365 instrumentation
│   ├── token_cache.py
│   ├── observability_token_service.py
│   ├── import_a365_config.py     # Bridges a365.generated.config.json → .env
│   ├── requirements.txt
│   ├── requirements-a365.txt
│   ├── .env.example
│   ├── README.md
│   └── docs/
│       └── INTEGRATION_GUIDE.md  # Long-form rationale: identity, FMI, threat model
├── LangChainAgent365/            # Classic AgentExecutor pattern
│   ├── agent_basic.py
│   ├── agent_a365.py             # uses instrumentation_options={"langchain": {...}}
│   ├── (shared modules + reqs + env)
│   └── README.md
├── LangGraphAgent365/            # Explicit StateGraph pattern
│   ├── agent_basic.py
│   ├── agent_a365.py             # same auto-instrumentation, different graph
│   ├── (shared modules + reqs + env)
│   └── README.md
└── OLDAGENTSWORKING/             # Earlier PoC iteration, kept for reference
```

## Recommended reading order

1. [`BasicAgent365/README.md`](./BasicAgent365/README.md) — full **2-phase reproduction** (Phase 1 on a host machine for `a365 setup all` + OtelWrite grant; Phase 2 on any machine to run the agent).
2. [`BasicAgent365/docs/INTEGRATION_GUIDE.md`](./BasicAgent365/docs/INTEGRATION_GUIDE.md) — the **long-form rationale**: Federated Managed Identity, the 3-hop FMI/FIC token chain, scope schema, threat model, troubleshooting.
3. Then pick *either* [`LangChainAgent365/README.md`](./LangChainAgent365/README.md) *or* [`LangGraphAgent365/README.md`](./LangGraphAgent365/README.md) depending on your target stack. Both rely on the native `LangChainInstrumentor` shipped in `microsoft-opentelemetry` 1.2.0, plus one extra `A365SpanProcessor` to seed `microsoft.tenant.id` and `gen_ai.agent.id` on every span.

## Provisioning at a glance

Microsoft Agent 365 has a **two-machine workflow** that this repo accounts for explicitly:

| Phase | Where | What | Output |
|-------|-------|------|--------|
| 1 | **Host** (Windows / macOS / native Linux) — *not* the dev container | `a365 setup all --agent-name <name> --authmode s2s`, then a manual `Connect-MgGraph` snippet to grant `Agent365.Observability.OtelWrite` (Application) | `a365.generated.config.json` + a one-time-printed plaintext Blueprint secret |
| 2 | **Any machine** (dev container fine here) | `python import_a365_config.py` to bridge the JSON into a local `.env`, then `python agent_a365.py` | Spans on the wire (`HTTP 200` from ingest); admin-center visibility requires a licensed tenant |

Why two machines? The `a365` CLI does an interactive browser OAuth callback to `http://localhost:<random-port>/`, and VS Code Remote Containers don't pre-forward random high ports → the callback never reaches the listener and the CLI hangs. See [`BasicAgent365/docs/INTEGRATION_GUIDE.md` § 8](./BasicAgent365/docs/INTEGRATION_GUIDE.md).

## Verified status (smoke-test results, May 2026)

All three samples produce the expected span sequence end-to-end:

- ✅ 3-hop FMI/FIC chain mints a valid Agent 365 observability bearer (verified by decoding `roles: ['Agent365.Observability.OtelWrite']`).
- ✅ Spans hit the ingest endpoint with `HTTP 200`.
- ✅ The LangChain / LangGraph samples produce **6 A365-eligible spans** per turn (3× `invoke_agent`, 2× `chat`, 1× `execute_tool`) via the native `LangChainInstrumentor`. The 13 LangChain-internal helper spans (Runnable / ChatPromptTemplate / etc.) are filtered out by the exporter as expected.
- ⚠️ The test tenant is **not licensed for Microsoft Agent 365**. The ingest endpoint accepts the request but the downstream sinks (flashpoint / sentinel / esp) reject it with `tenant_not_licensed`, so nothing appears in the admin center. The code is correct; this is a licensing gate that lives outside the integration code. See the `tenant_not_licensed` row in [`BasicAgent365/docs/INTEGRATION_GUIDE.md` § 15](./BasicAgent365/docs/INTEGRATION_GUIDE.md).

## Tooling assumptions

| Tool | Version | Notes |
|------|---------|-------|
| Python | 3.10+ | Tested on 3.13 (dev container default) |
| `a365` CLI | `1.1.184+` | `dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli --prerelease` |
| `azure-cli` | recent | `az login --allow-no-subscriptions` before any `a365` command |
| PowerShell 7+ + `Microsoft.Graph.Authentication` + `Microsoft.Graph.Applications` | latest | Only required on the host machine for the manual `OtelWrite` grant |
| `microsoft-opentelemetry` | `>= 1.2.0` | **Requires** `a365_enable_observability_exporter=True` (or the equivalent env var) at SDK init — otherwise spans are dropped silently |

## Top-level files we deliberately don't commit

- `.env` in any subfolder (secrets) — `.env.example` is committed instead.
- `a365.generated.config.json` in any subfolder (contains the Blueprint client secret in DPAPI form on Windows) — regenerate with `a365 setup all`.

Each subfolder's `.gitignore` enforces this.

## License & ownership

Internal Inditex PoC. Not for external redistribution.
