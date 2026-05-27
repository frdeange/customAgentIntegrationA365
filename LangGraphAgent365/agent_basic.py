"""
agent_basic.py
==============
The unmodified LangGraph starting agent — no Agent 365 telemetry.

Pattern: an **explicit `StateGraph`** that wires together two nodes
(`call_model` and a `ToolNode`) into the canonical ReAct loop:

    START
      v
    [agent (LLM)] --no-tool-calls--> END
      v
      tool-calls
      v
    [tools (ToolNode)] ----> back to agent

This is the LangGraph counterpart to LangChain's `AgentExecutor`. They
produce identical event sequences (`on_chat_model_start/end`,
`on_tool_start/end`); the difference is structural — you can edit the
graph topology to add parallel branches, conditional routing,
human-in-the-loop interrupts, or persistent memory, which `AgentExecutor`
cannot express.
"""

import os
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


# =========================================================================
# ENV
# =========================================================================

load_dotenv()

AOAI_ENDPOINT    = os.environ["AZURE_OPENAI_ENDPOINT"]
AOAI_API_VERSION = os.environ["AZURE_OPENAI_API_VERSION"]
MODEL            = os.environ["AZURE_OPENAI_DEPLOYMENT"]


# =========================================================================
# TOOLS
# =========================================================================

@tool
def get_current_time() -> str:
    """Returns the current local time in Madrid as a human-readable string."""
    now = datetime.now(ZoneInfo("Europe/Madrid"))
    # NB: never use isoformat() — under the A365-integrated version, ISO 8601
    # strings inside tool outputs trigger an OTLP attribute-validation failure
    # server-side.
    return now.strftime("%H:%M:%S %Z")


TOOLS = [get_current_time]


# =========================================================================
# LLM (Entra ID auth — no API key)
# =========================================================================

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


# =========================================================================
# GRAPH — the LangGraph way
# =========================================================================

class AgentState(TypedDict):
    """Graph state: a growing list of messages, merged with `add_messages`."""
    messages: Annotated[list[BaseMessage], add_messages]


SYSTEM_PROMPT = "You are a helpful, minimalist agent. You can call tools when needed."


def call_model(state: AgentState) -> dict:
    """Single LLM node: invoke the model on the full message history."""
    messages = state["messages"]
    response = llm_with_tools.invoke(
        [("system", SYSTEM_PROMPT), *messages]
    )
    return {"messages": [response]}


def route_after_agent(state: AgentState) -> str:
    """Conditional edge: if the LLM emitted tool_calls, run them; else stop."""
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


# =========================================================================
# MAIN
# =========================================================================

def run_agent(user_input: str) -> str:
    state = agent.invoke({"messages": [HumanMessage(content=user_input)]})
    # Final assistant message is the last one in the accumulated state.
    return state["messages"][-1].content


if __name__ == "__main__":
    print("=" * 50)
    print("LANGGRAPH AGENT — base (no Agent 365 telemetry)")
    print("=" * 50)
    print("Type 'exit' to quit.\n")

    while (msg := input("You: ")).lower() != "exit":
        print(f"\nAgent: {run_agent(msg)}\n")
