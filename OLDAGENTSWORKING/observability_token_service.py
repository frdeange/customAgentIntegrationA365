"""3-hop FMI/FIC token chain for the Agent 365 observability endpoint.

The autonomous Agent 365 pattern requires a Federated Managed Identity (FMI) chain
to obtain an observability ingestion token. Direct client-credentials on the
blueprint app is rejected by Entra with AADSTS82001 for "agentic" apps, so the
real flow is:

    Hop 1+2:  Blueprint authenticates with itself (client_secret in local dev,
              Azure Managed Identity in production).
              Then calls `acquire_token_for_client(scopes=[FMI_SCOPE], fmi_path=<agent_id>)`
              -> returns T1, a federated assertion targeting the Agent Identity app.

    Hop 3:    Agent Identity app authenticates using T1 as a `client_assertion`.
              Then calls `acquire_token_for_client(scopes=[OBSERVABILITY_SCOPE])`
              -> returns the final observability bearer token.

For this PoC we ship ONLY the client-secret variant (local dev). The MSI variant
exists in the Microsoft sample for production. Both end up at the same `obs_token`.

Reference:
https://github.com/microsoft/Agent365-Samples/blob/main/python/autonomous/github-trending/observability_token_service.py
"""

from __future__ import annotations

import logging
from datetime import timedelta

import msal

import OLDAGENTSWORKING.token_cache as token_cache

logger = logging.getLogger(__name__)

# Hop 1+2 target — federated identity exchange resource. Constant for all tenants.
_FMI_SCOPES = ["api://AzureADTokenExchange/.default"]

# Hop 3 target — the Agent 365 observability service app registration. Constant.
_OBSERVABILITY_SCOPES = ["api://9b975845-388f-4429-889e-eab1ef63949c/.default"]


def acquire_observability_token(
    tenant_id: str,
    agent_id: str,
    blueprint_client_id: str,
    blueprint_client_secret: str,
) -> None:
    """Run the 3-hop FMI chain and cache the resulting observability token.

    After this call returns, `token_cache.get_cached_token(agent_id, tenant_id)`
    returns a non-None Bearer suitable for the Agent 365 ingest endpoint.
    """
    authority = f"https://login.microsoftonline.com/{tenant_id}"

    # ---- Hop 1+2: blueprint -> T1 (FMI assertion targeting the agent identity)
    blueprint_app = msal.ConfidentialClientApplication(
        client_id=blueprint_client_id,
        client_credential=blueprint_client_secret,
        authority=authority,
    )
    t1_result = blueprint_app.acquire_token_for_client(
        scopes=_FMI_SCOPES,
        fmi_path=agent_id,
    )
    if "access_token" not in t1_result:
        raise RuntimeError(
            f"FMI T1 acquisition failed: "
            f"{t1_result.get('error_description', t1_result)}"
        )
    t1_token = t1_result["access_token"]
    logger.info("FMI hop 1+2 succeeded (blueprint -> agent identity assertion).")

    # ---- Hop 3: agent identity (authenticated via T1) -> observability token
    identity_app = msal.ConfidentialClientApplication(
        client_id=agent_id,
        client_credential={"client_assertion": t1_token},
        authority=authority,
    )
    obs_result = identity_app.acquire_token_for_client(scopes=_OBSERVABILITY_SCOPES)
    if "access_token" not in obs_result:
        raise RuntimeError(
            f"Observability token acquisition failed: "
            f"{obs_result.get('error_description', obs_result)}"
        )

    # Tokens default to ~1h. Cache with a 55-minute TTL to be safe.
    token_cache.cache_token(
        agent_id=agent_id,
        tenant_id=tenant_id,
        token=obs_result["access_token"],
        expires_in=timedelta(minutes=55),
    )
    logger.info("FMI hop 3 succeeded; observability token cached.")
