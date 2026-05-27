"""
agent_a365.py
=============
Autonomous-pattern Agent 365 PoC. Same agent logic as `agent_basic.py` (BYO
worker — raw requests to Azure OpenAI, one `get_current_time` tool), wrapped
with the **Microsoft OpenTelemetry Distro** (`microsoft-opentelemetry`) and
the A365 observability scopes.

What is different from `agent_basic.py`
---------------------------------------
1. `use_microsoft_opentelemetry(enable_a365=True, a365_token_resolver=...)`
   is called once at startup. It wires the OpenTelemetry TracerProvider and
   the Agent 365 exporter.

2. Before the agent runs, the **3-hop FMI/FIC chain** mints an observability
   token and caches it (see `observability_token_service.py`). The
   `a365_token_resolver` callback then reads from that cache on every export.

3. The agent body is wrapped in three nested scopes
   (`InvokeAgentScope` -> `InferenceScope` -> `ExecuteToolScope`) so Agent 365
   captures `AIInvokeAgent`, `AIInferenceCall` and `AIExecuteTool` audit events.

Two gates to a fully working end-to-end export
----------------------------------------------
Gate A — token minting (your code): SOLVED by this file via the 3-hop FMI flow.
Gate B — tenant onboarded to Agent 365 (Microsoft-side): If your tenant is not
licensed/onboarded yet, the FMI flow still works (you see a Bearer minted) but
the ingestion endpoint returns `HTTP 400 EndpointInvalid: TenantIdInvalid`.
That is the expected demo state until Microsoft enables the tenant.

References
----------
- Microsoft sample:                  https://github.com/microsoft/Agent365-Samples/tree/main/python/autonomous/github-trending
- A365 observability docs:           https://learn.microsoft.com/en-us/microsoft-agent-365/developer/observability
- Microsoft OpenTelemetry Distro:    https://learn.microsoft.com/en-us/microsoft-agent-365/developer/microsoft-opentelemetry
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

# Disable the SDK's internal statsbeat telemetry (causes a noisy SSL warning
# in some local environments and is not needed for our demo). Must be set
# before importing microsoft.opentelemetry.
os.environ.setdefault("MICROSOFT_OTEL_SDKSTATS_DISABLED", "true")

import requests
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv

# --- Microsoft OpenTelemetry Distro + Agent 365 observability --------------
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

import OLDAGENTSWORKING.token_cache as token_cache
from OLDAGENTSWORKING.observability_token_service import acquire_observability_token

# ---------------------------------------------------------------------------
# Env loading + defensive cleanup
# ---------------------------------------------------------------------------
load_dotenv()

# python-dotenv loads empty values as "" which makes EnvironmentCredential try
# (and fail) a service-principal flow. Strip empties so DefaultAzureCredential
# falls through to AzureCliCredential cleanly.
for _v in ("AZURE_TENANT_ID", "AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET"):
    if os.environ.get(_v, None) == "":
        del os.environ[_v]

# Make sure azure-cli is on PATH so AzureCliCredential can find it. The MSI
# installer puts it in Program Files but doesn't always update the per-process
# PATH that Python inherits, so we add it defensively.
_AZ_CLI_PATH = r"C:\Program Files\Microsoft SDKs\Azure\CLI2\wbin"
if os.path.isdir(_AZ_CLI_PATH) and _AZ_CLI_PATH not in os.environ.get("PATH", ""):
    os.environ["PATH"] = _AZ_CLI_PATH + os.pathsep + os.environ.get("PATH", "")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("agent_a365")

# ---------------------------------------------------------------------------
# Azure OpenAI config (same as agent_basic.py)
# ---------------------------------------------------------------------------
AOAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
AOAI_DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]
AOAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-10-21")
AOAI_SCOPE = "https://cognitiveservices.azure.com/.default"

# ---------------------------------------------------------------------------
# Agent 365 identity / metadata (filled in by `a365 setup all`)
# ---------------------------------------------------------------------------
TENANT_ID = os.environ.get("AGENT365_TENANT_ID", "")
AGENT_ID = os.environ.get("AGENT365_AGENT_ID", "")
BLUEPRINT_ID = os.environ.get("AGENT365_BLUEPRINT_ID", "")
CLIENT_ID = os.environ.get("AGENT365_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("AGENT365_CLIENT_SECRET", "")
AGENT_NAME = os.environ.get("AGENT365_AGENT_NAME", "inditex-demo-agent")
AGENT_DESCRIPTION = os.environ.get(
    "AGENT365_AGENT_DESCRIPTION",
    "BYO autonomous worker agent — calls Azure OpenAI via raw HTTP.",
)


def _has_a365_credentials() -> bool:
    """True iff all blueprint + agent identity values are populated."""
    return all(
        v and not v.startswith("<<")
        for v in (TENANT_ID, AGENT_ID, CLIENT_ID, CLIENT_SECRET)
    )


A365_ENABLED = _has_a365_credentials()

# ---------------------------------------------------------------------------
# AOAI credential
# ---------------------------------------------------------------------------
_aoai_credential = DefaultAzureCredential()


def _get_aoai_token() -> str:
    return _aoai_credential.get_token(AOAI_SCOPE).token


# ---------------------------------------------------------------------------
# Configure the OpenTelemetry distro
# ---------------------------------------------------------------------------
# Resolver returns the cached observability bearer, or None if the cache is
# empty / expired. The exporter logs a clear warning when the resolver returns
# None and skips that batch — exactly what we want for graceful degradation.
def _token_resolver(req_agent_id: str, req_tenant_id: str) -> str | None:
    return token_cache.get_cached_token(req_agent_id, req_tenant_id)


use_microsoft_opentelemetry(
    enable_a365=A365_ENABLED,
    enable_azure_monitor=False,
    a365_use_s2s_endpoint=True,
    a365_token_resolver=_token_resolver,
)

# ---------------------------------------------------------------------------
# Tool declaration + implementation (same as agent_basic.py)
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Returns the current UTC time as an ISO 8601 string.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }
]


def get_current_time() -> str:
    # NOTE: returning a strict ISO 8601 string (e.g., datetime.isoformat())
    # tripped a server-side bug in the A365 ingest endpoint: it auto-parses
    # ISO timestamps into Date objects and then fails OTLP attribute-value
    # validation with `Unexpected JSON token "Date"`. Returning a clearly
    # non-ISO, human-readable form sidesteps that.
    return datetime.now(timezone.utc).strftime("%H:%M:%S on %d-%b-%Y (UTC)")


# ---------------------------------------------------------------------------
# Azure OpenAI HTTP call wrapped in InferenceScope
# ---------------------------------------------------------------------------
def _build_aoai_url() -> str:
    base = AOAI_ENDPOINT.rstrip("/")
    if "/openai/v1" in base.lower():
        url = f"{base}/chat/completions"
    else:
        url = f"{base}/openai/deployments/{AOAI_DEPLOYMENT}/chat/completions"
    if AOAI_API_VERSION:
        sep = "&" if "?" in url else "?"
        url += f"{sep}api-version={AOAI_API_VERSION}"
    return url


def _call_aoai_with_telemetry(
    messages: list[dict[str, Any]],
    request: Request,
    agent_details: AgentDetails,
) -> dict[str, Any]:
    inference_details = InferenceCallDetails(
        operationName=InferenceOperationType.CHAT,
        model=AOAI_DEPLOYMENT,
        providerName="azure-openai",
    )

    with InferenceScope.start(
        request=request,
        details=inference_details,
        agent_details=agent_details,
    ) as scope:
        url = _build_aoai_url()
        headers = {
            "Authorization": f"Bearer {_get_aoai_token()}",
            "Content-Type": "application/json",
        }
        body = {
            "model": AOAI_DEPLOYMENT,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
        }
        resp = requests.post(url, headers=headers, json=body, timeout=60)
        if not resp.ok:
            logger.error("AOAI %s: %s", resp.status_code, resp.text)
            resp.raise_for_status()
        data = resp.json()

        usage = data.get("usage", {})
        if usage:
            scope.record_input_tokens(usage.get("prompt_tokens", 0))
            scope.record_output_tokens(usage.get("completion_tokens", 0))

        out_msg = data["choices"][0]["message"]
        scope.record_output_messages([json.dumps(out_msg)])
        return data


# ---------------------------------------------------------------------------
# Agent loop (single autonomous cycle) wrapped in InvokeAgentScope
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = (
    "You are an autonomous worker agent for an Inditex store backoffice process. "
    "If your current task requires knowing the time, you MUST use the "
    "get_current_time tool. Never claim to be an AI assistant — you are a worker."
)


def run_one_cycle(
    *,
    triggering_user_id: str = "store-process-trigger",
    triggering_user_email: str = "store-process@inditex.example",
    triggering_user_name: str = "Store Process",
    session_id: str = "session-autonomous-demo",
    conversation_id: str = "conv-autonomous-demo",
) -> str:
    """Execute one autonomous cycle of the worker.

    The "user" populated in caller_details represents the principal that
    TRIGGERED the worker — for Inditex this would be the store associate /
    manager / system that scheduled or fired the event.
    """
    agent_details = AgentDetails(
        agent_id=AGENT_ID or "local-dev-agent",
        agent_name=AGENT_NAME,
        agent_description=AGENT_DESCRIPTION,
        agent_blueprint_id=BLUEPRINT_ID,
        tenant_id=TENANT_ID or "local-dev-tenant",
    )

    user_prompt = (
        f"It is now {datetime.now(timezone.utc).isoformat()}. "
        "Report the current UTC time in human-readable form so the next batch "
        "step can pick it up."
    )

    request = Request(
        content=user_prompt,
        session_id=session_id,
        conversation_id=conversation_id,
        channel=Channel(name="scheduled"),
    )
    caller_details = CallerDetails(
        user_details=UserDetails(
            user_id=triggering_user_id,
            user_email=triggering_user_email,
            user_name=triggering_user_name,
        ),
    )

    with InvokeAgentScope.start(
        request=request,
        scope_details=InvokeAgentScopeDetails(
            endpoint=ServiceEndpoint(hostname="localhost", port=0),
        ),
        agent_details=agent_details,
        caller_details=caller_details,
    ) as agent_scope:
        agent_scope.record_input_messages([user_prompt])

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

        first = _call_aoai_with_telemetry(messages, request, agent_details)
        choice = first["choices"][0]["message"]
        messages.append(choice)
        tool_calls = choice.get("tool_calls") or []

        if not tool_calls:
            final = choice.get("content") or ""
            agent_scope.record_output_messages([final])
            return final

        for tc in tool_calls:
            name = tc["function"]["name"]
            tool_details = ToolCallDetails(
                tool_name=name,
                tool_type="function",
                tool_call_id=tc["id"],
                arguments=tc["function"].get("arguments", "{}"),
                description="Demo tool — returns UTC time.",
            )
            with ExecuteToolScope.start(
                request=request,
                details=tool_details,
                agent_details=agent_details,
            ) as tool_scope:
                if name == "get_current_time":
                    result = get_current_time()
                else:
                    result = f"Tool '{name}' is not implemented."
                tool_scope.record_response(result)

            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "name": name,
                    "content": result,
                }
            )

        second = _call_aoai_with_telemetry(messages, request, agent_details)
        final = second["choices"][0]["message"].get("content") or ""
        agent_scope.record_output_messages([final])
        return final


def main() -> None:
    if A365_ENABLED:
        logger.info(
            "Acquiring Agent 365 observability token via 3-hop FMI chain..."
        )
        try:
            acquire_observability_token(
                tenant_id=TENANT_ID,
                agent_id=AGENT_ID,
                blueprint_client_id=CLIENT_ID,
                blueprint_client_secret=CLIENT_SECRET,
            )
            logger.info("Observability token cached successfully.")
        except Exception:
            logger.exception(
                "Failed to acquire observability token — continuing without "
                "the A365 exporter. Telemetry will still be emitted but only "
                "to whatever fallback exporter the distro selected."
            )
    else:
        logger.warning(
            "Agent 365 credentials not configured — skipping FMI token mint. "
            "Run `a365 setup all --agent-name <name>` and populate the "
            "AGENT365_* variables in .env to enable real export."
        )

    response = run_one_cycle()
    print(f"\nAGENT response: {response}\n")

    # Flush the OTel BatchSpanProcessor so spans are exported before exit.
    try:
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis=10000)
        if hasattr(provider, "shutdown"):
            provider.shutdown()
    except Exception:
        logger.exception("Failed to flush tracer provider on shutdown.")


if __name__ == "__main__":
    main()
