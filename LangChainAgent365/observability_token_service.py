"""
observability_token_service.py
==============================
Mint the Agent 365 observability bearer via the **3-hop FMI/FIC** chain.

Why this file exists
--------------------
The Agent 365 ingestion endpoint (``api://9b975845-388f-4429-889e-eab1ef63949c``)
will not accept a token obtained with a direct ``client_credentials`` flow
from either the Blueprint app or the Agent Identity — Entra rejects it with
``AADSTS82001`` ("Agentic apps cannot acquire tokens directly").

The supported S2S flow is a chain of three OAuth hops:

::

    Hop 1 + 2  Blueprint (client_secret in dev / Managed Identity in prod)
               -> acquire_token_for_client(
                      scopes   = ["api://AzureADTokenExchange/.default"],
                      fmi_path = AGENT_ID,
                  )
               => T1, a federated assertion targeting the Agent Identity.

    Hop 3      Agent Identity (authenticated with T1 as client_assertion)
               -> acquire_token_for_client(
                      scopes = ["api://9b975845-388f-4429-889e-eab1ef63949c/.default"],
                  )
               => T2, the observability bearer (~60 min lifetime).

This module only ships the **client_secret** variant of hop 1+2 (local-dev).
The Managed-Identity variant for production exists in the Microsoft sample
and only changes the credential type passed to MSAL on hop 1+2 — the rest of
the chain is identical.

The minted token is dropped into the in-memory ``token_cache`` so the
synchronous ``a365_token_resolver`` callback wired into the Microsoft
OpenTelemetry distro can return it instantly on every export batch.

Reference
---------
https://github.com/microsoft/Agent365-Samples/blob/main/python/autonomous/github-trending/observability_token_service.py
"""

from __future__ import annotations

import logging
from datetime import timedelta

import msal

import token_cache

logger = logging.getLogger(__name__)

# Hop 1+2 target — Entra's federated identity exchange resource.
# Constant for every tenant.
_FMI_SCOPES = ["api://AzureADTokenExchange/.default"]

# Hop 3 target — the Agent 365 observability service app registration.
# Constant for every tenant.
_OBSERVABILITY_SCOPES = ["api://9b975845-388f-4429-889e-eab1ef63949c/.default"]


def acquire_observability_token(
    tenant_id: str,
    agent_id: str,
    blueprint_client_id: str,
    blueprint_client_secret: str,
) -> None:
    """Run the 3-hop FMI chain and cache the resulting observability bearer.

    Parameters
    ----------
    tenant_id
        Entra tenant ID hosting both the Blueprint app and the Agent Identity.
    agent_id
        Entra Agent ID of the Agent Identity (the principal that holds the
        ``Agent365.Observability.OtelWrite`` application role).
    blueprint_client_id
        Client ID of the Blueprint app (the "agentic application").
    blueprint_client_secret
        Client secret of the Blueprint app. **Local-dev only.** In production
        the Blueprint should authenticate with a User-Assigned Managed Identity
        and this argument disappears.

    Side effects
    ------------
    On success, populates ``token_cache`` with a token valid for ~55 minutes
    keyed by ``(agent_id, tenant_id)``. Raises ``RuntimeError`` on any hop
    failure with the Entra error description attached.
    """
    authority = f"https://login.microsoftonline.com/{tenant_id}"

    # -- Hop 1+2: Blueprint mints T1 -- federated assertion for the Agent Identity --
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
            "FMI hop 1+2 (Blueprint -> Agent Identity assertion) failed: "
            f"{t1_result.get('error_description', t1_result)}"
        )
    t1_token = t1_result["access_token"]
    logger.info("FMI hop 1+2 succeeded (Blueprint -> Agent Identity assertion).")

    # -- Hop 3: Agent Identity authenticates with T1 -> observability bearer --
    identity_app = msal.ConfidentialClientApplication(
        client_id=agent_id,
        client_credential={"client_assertion": t1_token},
        authority=authority,
    )
    obs_result = identity_app.acquire_token_for_client(scopes=_OBSERVABILITY_SCOPES)
    if "access_token" not in obs_result:
        raise RuntimeError(
            "FMI hop 3 (Agent Identity -> observability bearer) failed: "
            f"{obs_result.get('error_description', obs_result)}"
        )

    # Entra tokens default to ~60 min. Cache 55 min to leave a safety margin
    # (the cache itself also applies a 5-min eviction buffer on read).
    token_cache.cache_token(
        agent_id=agent_id,
        tenant_id=tenant_id,
        token=obs_result["access_token"],
        expires_in=timedelta(minutes=55),
    )
    logger.info("FMI hop 3 succeeded; observability bearer cached.")
