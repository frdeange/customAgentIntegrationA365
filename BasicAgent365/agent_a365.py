"""
agent_a365.py
=============
The Agent 365-integrated version of `agent_basic.py`.

Same business logic. Same Azure OpenAI Responses API loop. Same single tool.
What this file adds, and *only* what it adds:

    1. Disable the distro's internal statsbeat telemetry (avoids noisy SSL
       warnings in dev containers without the right CA bundle).
    2. Wire the Microsoft OpenTelemetry Distro to the Agent 365 S2S exporter
       and feed it a synchronous token resolver backed by `token_cache`.
    3. Wrap the agent invocation in `InvokeAgentScope`, each LLM call in
       `InferenceScope`, and each tool execution in `ExecuteToolScope` so
       Agent 365 receives the three mandatory audit spans per turn.
    4. Mint the observability bearer at startup via the 3-hop FMI/FIC chain
       (`observability_token_service.acquire_observability_token`).
    5. Force-flush the BatchSpanProcessor before exit so the last spans are
       not lost.

Anchors in the file are tagged `# [A365] ...` for an at-a-glance diff against
`agent_basic.py`. Open both files side by side or run::

    diff agent_basic.py agent_a365.py

to see exactly what the integration cost in code.

If the AGENT365_* env vars are not yet populated (i.e. `a365 setup all` has
not been run), the SDK is initialised in no-op mode and the agent's
business logic still works — telemetry simply isn't exported.

See `docs/INTEGRATION_GUIDE.md` for the full rationale, identity model and
threat-model justification of the 3-hop chain.
"""

# [A365] Disable internal statsbeat BEFORE importing the distro. Without this,
# environments lacking the Microsoft public-cert trust chain log a noisy
# `SSL: CERTIFICATE_VERIFY_FAILED` from Application Insights statsbeat.
import os
os.environ.setdefault("MICROSOFT_OTEL_SDKSTATS_DISABLED", "true")

import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv
from openai import AzureOpenAI

# [A365] Microsoft OpenTelemetry Distro — replaces the older fragmented
# `microsoft-agents-a365-*` packages with a single unified entry point.
from microsoft.opentelemetry import use_microsoft_opentelemetry
from microsoft.opentelemetry.a365.core import (
    AgentDetails,
    CallerDetails,
    Channel,
    ExecuteToolScope,
    InferenceCallDetails,
    InferenceOperationType,
    InferenceScope,
    InvokeAgentScope,
    InvokeAgentScopeDetails,
    Request,
    ServiceEndpoint,
    ToolCallDetails,
    UserDetails,
)

import token_cache
from observability_token_service import acquire_observability_token


# =========================================================
# ENV LOADING (+ defensive cleanup)
# =========================================================

load_dotenv()

# python-dotenv loads missing-but-declared variables as the empty string,
# which makes EnvironmentCredential attempt a service-principal flow and
# fail loudly. Strip empties so DefaultAzureCredential falls through to
# AzureCliCredential cleanly.
for _v in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
    if os.environ.get(_v, "") == "":
        os.environ.pop(_v, None)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("agent_a365")


# =========================================================
# AZURE OPENAI CONFIG (same as agent_basic.py)
# =========================================================

AOAI_ENDPOINT    = os.getenv("AZURE_OPENAI_ENDPOINT")
AOAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION")
MODEL            = os.getenv("AZURE_OPENAI_DEPLOYMENT")


# =========================================================
# AGENT 365 CONFIG (populated by `a365 setup all`)
# =========================================================

TENANT_ID     = os.getenv("AGENT365_TENANT_ID", "")
AGENT_ID      = os.getenv("AGENT365_AGENT_ID", "")
BLUEPRINT_ID  = os.getenv("AGENT365_BLUEPRINT_ID", "")
CLIENT_ID     = os.getenv("AGENT365_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AGENT365_CLIENT_SECRET", "")
AGENT_NAME    = os.getenv("AGENT365_AGENT_NAME", "inditex-demo-agent")
AGENT_DESC    = os.getenv(
    "AGENT365_AGENT_DESCRIPTION",
    "BYO autonomous worker agent — Azure OpenAI Responses API + one local tool.",
)


def _has_a365_credentials() -> bool:
    """Return True iff every value needed by the 3-hop FMI flow is set."""
    return all(
        v and not v.startswith("<<")
        for v in (TENANT_ID, AGENT_ID, CLIENT_ID, CLIENT_SECRET)
    )


A365_ENABLED = _has_a365_credentials()


# =========================================================
# AZURE OPENAI CLIENT (Entra ID auth — same as agent_basic.py)
# =========================================================

client = AzureOpenAI(
    azure_endpoint=AOAI_ENDPOINT,
    api_version=AOAI_API_VERSION,
    azure_ad_token_provider=get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    ),
)


# =========================================================
# TOOLS (same as agent_basic.py)
# =========================================================

def get_current_time():
    """Return the current local time in Madrid.

    NOTE: the output is a deliberately human-readable string (no ISO 8601).
    The Agent 365 ingest endpoint auto-parses ISO timestamps into Date
    objects and then fails OTLP attribute-value validation; sticking to a
    plain ``HH:MM:SS TZ`` form sidesteps that.
    """
    now = datetime.now(ZoneInfo("Europe/Madrid"))
    return {"current_time": now.strftime("%H:%M:%S %Z")}


TOOLS = {"get_current_time": get_current_time}

TOOL_DEFS = [
    {
        "type": "function",
        "name": "get_current_time",
        "description": "Returns the current local time.",
        "parameters": {"type": "object", "properties": {}},
    }
]


# =========================================================
# MEMORY (same as agent_basic.py)
# =========================================================

conversation = [
    {
        "role": "system",
        "content": (
            "You are a helpful, minimalist agent. "
            "You can call tools when needed."
        ),
    }
]


# =========================================================
# [A365] WIRE THE MICROSOFT OPENTELEMETRY DISTRO
# =========================================================
# The distro calls this resolver on every BatchSpanProcessor flush. It must
# be synchronous and fast — hence the in-memory `token_cache`. The actual
# token minting (3-hop FMI) happens once at startup in `_bootstrap_a365_token`.

def _token_resolver(req_agent_id: str, req_tenant_id: str) -> str | None:
    return token_cache.get_cached_token(req_agent_id, req_tenant_id)


use_microsoft_opentelemetry(
    enable_a365                         = A365_ENABLED,
    enable_azure_monitor                = False,
    a365_use_s2s_endpoint               = True,           # autonomous worker → S2S, not OBO
    a365_enable_observability_exporter  = A365_ENABLED,   # actually ship spans to A365 ingest
    a365_token_resolver                 = _token_resolver,
)


# =========================================================
# A365 METADATA HELPERS
# =========================================================

def _build_agent_details() -> AgentDetails:
    """Identity + descriptive metadata about the running agent.

    Every span MUST carry `tenant_id` and `agent_id` or it is silently
    dropped server-side. `AgentDetails` propagates them automatically.
    """
    return AgentDetails(
        agent_id           = AGENT_ID or "local-dev-agent",
        agent_name         = AGENT_NAME,
        agent_description  = AGENT_DESC,
        agent_blueprint_id = BLUEPRINT_ID,
        tenant_id          = TENANT_ID or "local-dev-tenant",
    )


def _build_caller_details() -> CallerDetails:
    """The principal that triggered this run.

    For an interactive CLI this is just "local developer". For a real
    autonomous worker it would be the scheduler / event source / store
    process that fired the run.
    """
    return CallerDetails(
        user_details = UserDetails(
            user_id    = "local-dev",
            user_email = "dev@inditex.example",
            user_name  = "Local developer",
        ),
    )


# =========================================================
# AGENT LOOP — Responses API + three nested A365 scopes
# =========================================================

def run_agent(user_input: str) -> str:
    agent_details = _build_agent_details()
    caller        = _build_caller_details()
    request       = Request(
        content         = user_input,
        session_id      = "interactive-cli-session",
        conversation_id = "interactive-cli-conv",
        channel         = Channel(name="interactive-cli"),
    )

    # [A365] Wrap the full agent invocation (one user turn).
    with InvokeAgentScope.start(
        request       = request,
        scope_details = InvokeAgentScopeDetails(
                          endpoint=ServiceEndpoint(hostname="localhost", port=0),
                        ),
        agent_details = agent_details,
        caller_details= caller,
    ) as agent_scope:
        agent_scope.record_input_messages([user_input])

        conversation.append({"role": "user", "content": user_input})

        while True:
            # [A365] Wrap a single LLM call (one Responses API turn).
            with InferenceScope.start(
                request       = request,
                details       = InferenceCallDetails(
                                  operationName = InferenceOperationType.CHAT,
                                  model         = MODEL,
                                  providerName  = "azure-openai",
                                ),
                agent_details = agent_details,
            ) as inf_scope:
                response = client.responses.create(
                    model=MODEL,
                    input=conversation,
                    tools=TOOL_DEFS,
                )
                usage = getattr(response, "usage", None)
                if usage is not None:
                    inf_scope.record_input_tokens(getattr(usage, "input_tokens", 0))
                    inf_scope.record_output_tokens(getattr(usage, "output_tokens", 0))

            # Flush the returned items into history. The stateless Responses
            # API requires the original `function_call` to be present when we
            # send the matching `function_call_output` on the next turn.
            conversation.extend(item.model_dump() for item in response.output)

            calls = [i for i in response.output if i.type == "function_call"]
            if not calls:
                final = response.output_text
                agent_scope.record_output_messages([final])
                return final

            for call in calls:
                print(f"\n[TOOL CALL] {call.name}")

                # [A365] Wrap one tool execution.
                with ExecuteToolScope.start(
                    request       = request,
                    details       = ToolCallDetails(
                                      tool_name    = call.name,
                                      tool_type    = "function",
                                      tool_call_id = call.call_id,
                                      arguments    = call.arguments or "{}",
                                      description  = "Local Python tool.",
                                    ),
                    agent_details = agent_details,
                ) as tool_scope:
                    result = TOOLS[call.name](**json.loads(call.arguments or "{}"))
                    tool_scope.record_response(json.dumps(result))

                conversation.append({
                    "type":    "function_call_output",
                    "call_id": call.call_id,
                    "output":  json.dumps(result),
                })


# =========================================================
# [A365] STARTUP + SHUTDOWN
# =========================================================

def _bootstrap_a365_token() -> None:
    """Mint the observability bearer via the 3-hop FMI/FIC chain once."""
    if not A365_ENABLED:
        logger.warning(
            "Agent 365 credentials not configured — SDK running in no-op mode. "
            "Run `a365 setup all --agent-name <name>` and fill in the "
            "AGENT365_* values in .env to enable real telemetry export."
        )
        return

    logger.info("Acquiring Agent 365 observability token via 3-hop FMI chain...")
    try:
        acquire_observability_token(
            tenant_id               = TENANT_ID,
            agent_id                = AGENT_ID,
            blueprint_client_id     = CLIENT_ID,
            blueprint_client_secret = CLIENT_SECRET,
        )
        logger.info("Observability token cached successfully.")
    except Exception:
        logger.exception(
            "FMI token mint failed — the exporter will drop batches "
            "gracefully (token resolver returns None). Agent business logic "
            "is unaffected."
        )


def _flush_spans_on_exit() -> None:
    """[A365] Force the BatchSpanProcessor to flush before the process exits.

    Without this the last few spans of a short-lived run are lost — the
    processor batches in memory and only auto-flushes every few seconds.
    """
    try:
        from opentelemetry import trace
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=10000)
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:
        logger.exception("Failed to flush tracer provider on shutdown.")


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    _bootstrap_a365_token()

    print("=" * 50)
    print("AZURE OPENAI RESPONSES AGENT — Agent 365 integrated")
    print("=" * 50)
    print("Type 'exit' to quit.\n")

    try:
        while (msg := input("You: ")).lower() != "exit":
            print(f"\nAgent: {run_agent(msg)}\n")
    finally:
        _flush_spans_on_exit()
