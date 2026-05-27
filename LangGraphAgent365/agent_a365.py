"""
agent_a365.py
=============
The Agent 365-integrated version of `agent_basic.py` (LangGraph flavour).

LangGraph reuses LangChain's callback / runnable infrastructure, so the
**same native LangChain auto-instrumentation** that we enable for
``AgentExecutor`` in ``LangChainAgent365/`` also traces ``StateGraph``
nodes. We just enable it via
``use_microsoft_opentelemetry(instrumentation_options={"langchain": {...}})``
and invoke the compiled graph normally — no manual scope wrap, no
callback handler.

Everything else mirrors ``LangChainAgent365/agent_a365.py``: statsbeat
off, distro init with ``a365_enable_observability_exporter=True``,
3-hop FMI token mint, force-flush on exit. Only the agent loop changes:
from ``AgentExecutor`` to an explicit ``StateGraph``
(``agent → tools → agent → END``).

Anchors ``# [A365] ...`` tag every Agent-365-specific line so you can
grep them out and see the integration surface at a glance.
"""

# [A365] Disable internal statsbeat BEFORE importing the distro.
import os
os.environ.setdefault("MICROSOFT_OTEL_SDKSTATS_DISABLED", "true")

import logging
from datetime import datetime
from typing import Annotated, TypedDict
from zoneinfo import ZoneInfo

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.tools import tool
from langchain_openai import AzureChatOpenAI

from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode

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

# [A365] Strip empty Azure SP env vars so DefaultAzureCredential falls
# through cleanly to AzureCliCredential (see BasicAgent365/INTEGRATION_GUIDE § 15).
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
AGENT_NAME    = os.getenv("AGENT365_AGENT_NAME", "inditex-langgraph-A365")
AGENT_DESC    = os.getenv(
    "AGENT365_AGENT_DESCRIPTION",
    "LangGraph StateGraph agent integrated with Agent 365 observability.",
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
# LLM + GRAPH (same as agent_basic.py)
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
llm_with_tools = llm.bind_tools(TOOLS)


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]


SYSTEM_PROMPT = "You are a helpful, minimalist agent. You can call tools when needed."


def call_model(state: AgentState) -> dict:
    response = llm_with_tools.invoke([("system", SYSTEM_PROMPT), *state["messages"]])
    return {"messages": [response]}


def route_after_agent(state: AgentState) -> str:
    last = state["messages"][-1]
    if isinstance(last, AIMessage) and last.tool_calls:
        return "tools"
    return END


graph = StateGraph(AgentState)
graph.add_node("agent", call_model)
graph.add_node("tools", ToolNode(TOOLS))
graph.add_edge(START, "agent")
graph.add_conditional_edges("agent", route_after_agent, ["tools", END])
graph.add_edge("tools", "agent")
agent = graph.compile()


# =========================================================
# [A365] WIRE THE MICROSOFT OPENTELEMETRY DISTRO
# =========================================================

def _token_resolver(req_agent_id: str, req_tenant_id: str) -> str | None:
    return token_cache.get_cached_token(req_agent_id, req_tenant_id)


# [A365] Native LangChain auto-instrumentation. LangGraph nodes are
# LangChain Runnables, so the same instrumentor that traces
# ``AgentExecutor`` also traces ``StateGraph.compile().invoke(...)``.
# Pattern parity with the Node.js sample:
#   useMicrosoftOpenTelemetry({ instrumentationOptions: { langchain: {} } })
use_microsoft_opentelemetry(
    enable_a365                         = A365_ENABLED,
    enable_azure_monitor                = False,
    a365_use_s2s_endpoint               = True,
    a365_enable_observability_exporter  = A365_ENABLED,
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

    The LangChain auto-instrumentation traces every Runnable executed by
    the compiled graph and emits the full A365 span tree
    (invoke_agent → chat → execute_tool → chat) automatically.
    """
    state = agent.invoke({"messages": [HumanMessage(content=user_input)]})
    return str(state["messages"][-1].content)


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
    print("LANGGRAPH AGENT — Agent 365 integrated")
    print("=" * 50)
    print("Type 'exit' to quit.\n")

    try:
        while (msg := input("You: ")).lower() != "exit":
            print(f"\nAgent: {run_agent(msg)}\n")
    finally:
        _flush_spans_on_exit()
