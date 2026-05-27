"""
agent_a365.py
=============
The Agent 365-integrated version of `agent_basic.py`.

Same business logic. Same LangChain `AgentExecutor`. Same single tool.
What this file adds, and *only* what it adds:

    1. Disable the distro's internal statsbeat telemetry (avoids noisy
       SSL warnings in dev containers without the right CA bundle).
    2. Wire the Microsoft OpenTelemetry Distro to the Agent 365 S2S
       exporter and **enable the native LangChain auto-instrumentation**
       via ``instrumentation_options={"langchain": {...}}``.
       This emits the `invoke_agent`, `chat` and `execute_tool` spans
       automatically — no callback handler, no manual scope wrap.
    3. Mint the observability bearer at startup via the 3-hop FMI/FIC
       chain (`observability_token_service.acquire_observability_token`).
    4. Force-flush the BatchSpanProcessor before exit so the last spans
       are not lost.

Pattern parity: this matches the Node.js LangChain sample shipped by
Microsoft, which also relies on
``useMicrosoftOpenTelemetry({ instrumentationOptions: { langchain: {} } })``
and does NOT manually open ``InvokeAgentScope`` / ``ExecuteToolScope`` /
``InferenceScope``.

Anchors in the file are tagged `# [A365] ...` for an at-a-glance diff
against `agent_basic.py`. Open both files side by side or run::

    diff agent_basic.py agent_a365.py

to see exactly what the integration cost in code.

If the AGENT365_* env vars are not yet populated (i.e. `a365 setup all`
has not been run), the SDK is initialised in no-op mode and the agent's
business logic still works — telemetry simply isn't exported.

See ../BasicAgent365/docs/INTEGRATION_GUIDE.md for the full rationale,
identity model and threat-model justification of the 3-hop chain.
"""

# [A365] Disable internal statsbeat BEFORE importing the distro.
import os
os.environ.setdefault("MICROSOFT_OTEL_SDKSTATS_DISABLED", "true")

import logging
from datetime import datetime
from zoneinfo import ZoneInfo

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
from langchain_openai import AzureChatOpenAI

# As of LangChain 1.0 the classic AgentExecutor + create_tool_calling_agent
# helpers live in the separate `langchain-classic` package.
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent

# [A365] Microsoft OpenTelemetry Distro + the A365 span processor.
# We DO NOT import any a365.core Scope classes (InvokeAgentScope,
# InferenceScope, ExecuteToolScope) because the native LangChain
# instrumentor emits those spans for us. We DO need A365SpanProcessor
# to stamp `microsoft.tenant.id` and `gen_ai.agent.id` on every
# auto-instrumented span — without those two attributes, the A365
# exporter's eligibility filter drops the spans (see
# `microsoft.opentelemetry.a365.core.exporters.utils.filter_and_partition_by_identity`).
from opentelemetry import trace as _otel_trace
from microsoft.opentelemetry import use_microsoft_opentelemetry
from microsoft.opentelemetry.a365.core.exporters.span_processor import A365SpanProcessor

import token_cache
from observability_token_service import acquire_observability_token


# =========================================================
# ENV LOADING (+ defensive cleanup)
# =========================================================

load_dotenv()

# [A365] python-dotenv loads missing-but-declared vars as "", which makes
# EnvironmentCredential attempt a service-principal flow and fail loudly.
# Strip empties so DefaultAzureCredential falls through to AzureCliCredential.
for _v in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
    if os.environ.get(_v, "") == "":
        os.environ.pop(_v, None)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("agent_a365")


# =========================================================
# AZURE OPENAI CONFIG
# =========================================================

AOAI_ENDPOINT    = os.environ["AZURE_OPENAI_ENDPOINT"]
AOAI_API_VERSION = os.environ["AZURE_OPENAI_API_VERSION"]
MODEL            = os.environ["AZURE_OPENAI_DEPLOYMENT"]


# =========================================================
# AGENT 365 CONFIG
# =========================================================

TENANT_ID     = os.getenv("AGENT365_TENANT_ID", "")
AGENT_ID      = os.getenv("AGENT365_AGENT_ID", "")
BLUEPRINT_ID  = os.getenv("AGENT365_BLUEPRINT_ID", "")
CLIENT_ID     = os.getenv("AGENT365_CLIENT_ID", "")
CLIENT_SECRET = os.getenv("AGENT365_CLIENT_SECRET", "")
AGENT_NAME    = os.getenv("AGENT365_AGENT_NAME", "inditex-langchain-A365")
AGENT_DESC    = os.getenv(
    "AGENT365_AGENT_DESCRIPTION",
    "LangChain AgentExecutor agent integrated with Agent 365 observability.",
)


def _has_a365_credentials() -> bool:
    return all(
        v and not v.startswith("<<")
        for v in (TENANT_ID, AGENT_ID, CLIENT_ID, CLIENT_SECRET)
    )


A365_ENABLED = _has_a365_credentials()


# =========================================================
# TOOLS (same as agent_basic.py)
# =========================================================

@tool
def get_current_time() -> str:
    """Returns the current local time in Madrid as a human-readable string."""
    now = datetime.now(ZoneInfo("Europe/Madrid"))
    return now.strftime("%H:%M:%S %Z")


TOOLS = [get_current_time]


# =========================================================
# LLM + AGENT (same as agent_basic.py)
# =========================================================

llm = AzureChatOpenAI(
    azure_endpoint        = AOAI_ENDPOINT,
    api_version           = AOAI_API_VERSION,
    azure_deployment      = MODEL,
    azure_ad_token_provider = get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    ),
)

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful, minimalist agent. You can call tools when needed."),
    MessagesPlaceholder("chat_history", optional=True),
    ("human", "{input}"),
    MessagesPlaceholder("agent_scratchpad"),
])

agent          = create_tool_calling_agent(llm, TOOLS, prompt)
agent_executor = AgentExecutor(agent=agent, tools=TOOLS, verbose=True)


# =========================================================
# [A365] WIRE THE MICROSOFT OPENTELEMETRY DISTRO
# =========================================================
# The distro calls this resolver on every BatchSpanProcessor flush. It
# must be synchronous and fast — hence the in-memory `token_cache`.

def _token_resolver(req_agent_id: str, req_tenant_id: str) -> str | None:
    return token_cache.get_cached_token(req_agent_id, req_tenant_id)


# [A365] Native LangChain auto-instrumentation. The distro forwards the
# kwargs under `instrumentation_options["langchain"]` to
# LangChainInstrumentor.instrument(...) which patches LangChain's
# Runnable / Tool / ChatModel callbacks at import time. This emits:
#
#   - `invoke_agent <AGENT_NAME>` (gen_ai.operation.name=invoke_agent)
#   - `chat <MODEL>`              (gen_ai.operation.name=chat)
#   - `execute_tool <tool_name>`  (gen_ai.operation.name=execute_tool)
#
# Pattern parity with the Node.js sample:
#   useMicrosoftOpenTelemetry({ instrumentationOptions: { langchain: {} } })
use_microsoft_opentelemetry(
    enable_a365                         = A365_ENABLED,
    enable_azure_monitor                = False,
    a365_use_s2s_endpoint               = True,         # autonomous worker → S2S
    a365_enable_observability_exporter  = A365_ENABLED, # MANDATORY in distro >= 1.2.0
    a365_token_resolver                 = _token_resolver,
    instrumentation_options = {
        "langchain": {
            "enabled":           True,
            "agent_id":          AGENT_ID or "local-dev-agent",
            "agent_name":        AGENT_NAME,
            "agent_description": AGENT_DESC,
        },
    },
)

# [A365] Stamp `microsoft.tenant.id` + `gen_ai.agent.id` on every span
# the LangChain instrumentor produces. The distro registers an
# A365SpanProcessor() with no IDs (it relies on baggage from manual
# Scope classes); since we use the auto-instrumentor, no baggage is
# propagated and we must seed identity statically. The processor only
# sets attributes if they are absent (never overwrites).
if A365_ENABLED:
    _provider = _otel_trace.get_tracer_provider()
    if hasattr(_provider, "add_span_processor"):
        _provider.add_span_processor(
            A365SpanProcessor(tenant_id=TENANT_ID, agent_id=AGENT_ID),
        )


# =========================================================
# AGENT INVOCATION — no manual scope wrap; auto-instrumentation does it.
# =========================================================

def run_agent(user_input: str) -> str:
    """Run one user turn.

    The LangChain auto-instrumentation emits the full A365 span tree
    (invoke_agent → chat → execute_tool → chat) automatically. No
    callback handler, no manual ``InvokeAgentScope`` / ``InferenceScope``
    / ``ExecuteToolScope`` is needed.
    """
    result = agent_executor.invoke({"input": user_input})
    return str(result["output"])


# =========================================================
# [A365] STARTUP + SHUTDOWN
# =========================================================

def _bootstrap_a365_token() -> None:
    if not A365_ENABLED:
        logger.warning(
            "Agent 365 credentials not configured — SDK running in no-op mode. "
            "Run `a365 setup all --agent-name <name>` on a host machine, then "
            "`python import_a365_config.py` here to populate .env."
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
            "FMI token mint failed — the exporter will drop batches gracefully "
            "(token resolver returns None). Agent business logic is unaffected."
        )


def _flush_spans_on_exit() -> None:
    """Force the BatchSpanProcessor to flush before the process exits."""
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
    print("LANGCHAIN AGENT — Agent 365 integrated")
    print("=" * 50)
    print("Type 'exit' to quit.\n")

    try:
        while (msg := input("You: ")).lower() != "exit":
            print(f"\nAgent: {run_agent(msg)}\n")
    finally:
        _flush_spans_on_exit()
