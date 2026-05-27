"""
agent_basic.py
==============
The unmodified LangChain starting agent — no Agent 365 telemetry.

Pattern: LangChain `AgentExecutor` driven by `create_tool_calling_agent`,
backed by Azure OpenAI via `AzureChatOpenAI` with Entra ID auth (no API key).

This file is intentionally minimal and reads top-to-bottom in one screen.
The integrated version lives in `agent_a365.py` — diff the two to see the
exact A365 surface area.
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from dotenv import load_dotenv

from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.tools import tool
from langchain_openai import AzureChatOpenAI

# As of LangChain 1.0 the classic `AgentExecutor` + `create_tool_calling_agent`
# helpers live in the separate `langchain-classic` package.
from langchain_classic.agents import AgentExecutor, create_tool_calling_agent


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
    # NB: we deliberately do NOT use isoformat() — when this tool runs under
    # the Agent 365-integrated version, ISO 8601 strings inside tool outputs
    # trigger an OTLP attribute-validation failure server-side.
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


# =========================================================================
# PROMPT + AGENT
# =========================================================================

prompt = ChatPromptTemplate.from_messages([
    ("system", "You are a helpful, minimalist agent. You can call tools when needed."),
    MessagesPlaceholder("chat_history", optional=True),
    ("human", "{input}"),
    MessagesPlaceholder("agent_scratchpad"),
])

agent          = create_tool_calling_agent(llm, TOOLS, prompt)
agent_executor = AgentExecutor(agent=agent, tools=TOOLS, verbose=True)


# =========================================================================
# MAIN
# =========================================================================

def run_agent(user_input: str) -> str:
    result = agent_executor.invoke({"input": user_input})
    return result["output"]


if __name__ == "__main__":
    print("=" * 50)
    print("LANGCHAIN AGENT — base (no Agent 365 telemetry)")
    print("=" * 50)
    print("Type 'exit' to quit.\n")

    while (msg := input("You: ")).lower() != "exit":
        print(f"\nAgent: {run_agent(msg)}\n")
