# BasicAgent365

> **Reference implementation** of integrating a **BYO (Bring Your Own) Python agent** into **Microsoft Agent 365** with the smallest possible code surface — additive only, no rewrite of the agent's business logic.
>
> The agent is a minimal Azure OpenAI Responses API loop with one local Python tool. The integration adds: a 3-hop FMI token chain, the Microsoft OpenTelemetry distro, three nested A365 audit scopes per turn, and a force-flush on exit. That's it.

---

## Layout

| Path | Purpose |
|------|---------|
| [agent_basic.py](agent_basic.py) | The **unmodified** starting agent. Azure OpenAI Responses API + 1 local tool. **No A365 code.** |
| [agent_a365.py](agent_a365.py) | The integrated version. Same business logic, wrapped in A365 scopes. Diff vs `agent_basic.py` is intentionally minimal — every change is anchored with `# [A365] ...`. |
| [token_cache.py](token_cache.py) | Thread-safe in-memory cache for the observability bearer (55-min TTL). |
| [observability_token_service.py](observability_token_service.py) | The 3-hop FMI/FIC token chain implementation. |
| [import_a365_config.py](import_a365_config.py) | Helper to turn `a365.generated.config.json` (produced by the CLI on a Windows host) into a populated `.env` inside the container. |
| [requirements.txt](requirements.txt) | Deps for `agent_basic.py` only. |
| [requirements-a365.txt](requirements-a365.txt) | Extra deps for `agent_a365.py` (`microsoft-opentelemetry`, `msal`). |
| [.env.example](.env.example) | Template for both base + A365 settings. |
| **[docs/INTEGRATION_GUIDE.md](docs/INTEGRATION_GUIDE.md)** | **Conceptual deep-dive** (architecture, identity model, threat model, SDK semantics, full step-by-step plan). Read this for the *why*; read this README for the *how*. |

---

## Reading order

1. **This README** — full reproduction recipe for someone cloning the repo.
2. [agent_basic.py](agent_basic.py) — the starting point (intentionally untouched).
3. [agent_a365.py](agent_a365.py) — read top-to-bottom, follow the `# [A365] ...` anchors.
4. [docs/INTEGRATION_GUIDE.md](docs/INTEGRATION_GUIDE.md) — for architecture / security / production hardening conversations.

---

## End-to-end reproduction

This recipe assumes you are cloning a fresh checkout of the repo and want to take it from zero to "spans visible in M365 admin center" on your own tenant.

There are **two phases**: a *one-off* tenant provisioning (Phase 1) and the *recurring* dev loop (Phase 2).

### Pre-flight: which machine runs what?

| Step | Why it matters | Where to run |
|------|----------------|--------------|
| `a365 setup all` (provisioning) | Uses MSAL interactive browser auth that redirects to `http://localhost:RANDOM_PORT/`. In a VS Code **dev container** this random port is not pre-forwarded, so the OAuth callback never reaches the CLI listener and the command hangs forever. | **Host machine** (Windows / macOS / native Linux). |
| Granting the **Application** `OtelWrite` app role | `a365 setup all --authmode s2s` currently only grants the *Delegated* version. Even as Global Admin, the *Application* grant must be done with a manual `Connect-MgGraph` snippet. | **Host machine** with `Microsoft.Graph.*` PowerShell modules. |
| Everything else (`pip install`, run the agent, edit code) | Standard Python dev workflow. | **Anywhere**. The dev container is fine. |

> If your dev box and your provisioning box are the same machine (e.g. you work on Windows natively), do everything in one place. If you work in a dev container, do Phase 1 on the host and bring the resulting `a365.generated.config.json` into the container with the import script (Phase 2.4).

---

## Phase 1 — One-off tenant provisioning (host machine)

This phase produces:

- 1 **Agent Blueprint** (an Entra app registration with a client secret)
- 1 **Agent Identity** (an SP carrying the Entra Agent ID + the `OtelWrite` app role)
- 1 **Federated Identity Credential** linking them (`fmi_path` validation)
- 1 **Agent Registration** in the M365 admin center
- 1 file `a365.generated.config.json` containing the IDs + the DPAPI-protected secret

### 1.1. Install prerequisites on the host

You need **.NET 8** (or 9), **Azure CLI**, and **PowerShell 7+** with the `Microsoft.Graph.*` modules.

**Windows (Admin PowerShell):**
```powershell
# .NET 8 (skip if already installed)
winget install Microsoft.DotNet.SDK.8

# Azure CLI (skip if already installed)
winget install Microsoft.AzureCLI

# Microsoft Graph PowerShell modules (needed for the manual OtelWrite grant in 1.5)
Install-Module Microsoft.Graph.Authentication, Microsoft.Graph.Applications `
  -Scope CurrentUser -Force -AllowClobber
```

**macOS / Linux:** install `dotnet-sdk-8`, `azure-cli`, and `pwsh` from your package manager, then run the same `Install-Module` line inside `pwsh`.

### 1.2. Install the `a365` CLI

```powershell
dotnet tool install --global Microsoft.Agents.A365.DevTools.Cli --prerelease
# Make sure the .NET tools folder is on PATH for the current shell, e.g.:
# Windows: $env:PATH += ";$env:USERPROFILE\.dotnet\tools"
# Bash:    export PATH="$PATH:$HOME/.dotnet/tools"
a365 --version
```

You should see something like `1.1.184+...`.

### 1.3. Sign in to Azure

```powershell
az login --allow-no-subscriptions
az account show --query "{tenantId:tenantId, user:user.name}" -o table
```

Confirm the tenant ID is the one you want the Agent 365 artefacts in.

### 1.4. Run `a365 setup all`

Pick a unique agent name (it becomes both the Entra display name and the M365 admin-center entry):

```powershell
mkdir $env:TEMP\a365-setup    # or any working directory
cd $env:TEMP\a365-setup
a365 setup all `
  --agent-name inditex-test-A365 `
  --authmode s2s
```

You will be prompted for browser sign-in (your **Global Admin** account in the target tenant). The CLI then:

1. Creates the Blueprint app and prints its **client secret to the console — exactly once**. **Copy it to a secure location now**, e.g. paste it into a password manager. You will need it again in Phase 2.4.
2. Creates the Agent Identity SP and the FIC.
3. Writes `a365.generated.config.json` next to the current directory.
4. Attempts to grant the `OtelWrite` app role. **This step usually fails** — see 1.5.

> **DPAPI gotcha.** On Windows the value of `agentBlueprintClientSecret` in the JSON is **DPAPI-encrypted** and only decryptable by the same Windows user account that created it. Linux containers cannot decrypt it. That is exactly why you copied the plaintext secret from the console in step 1.4.1 above.

### 1.5. Manual OtelWrite grant

The CLI prints a remediation snippet at the end of its output. The reliable version is:

```powershell
Connect-MgGraph -TenantId '<your-tenant-id>' `
  -Scopes 'AppRoleAssignment.ReadWrite.All','Directory.Read.All'

$cfg       = Get-Content .\a365.generated.config.json | ConvertFrom-Json
# The SP object id of the Agent IDENTITY (not the Blueprint). The exact JSON key
# may vary between CLI versions; check the file with `Get-Content .\a365.generated.config.json`.
$agentSpId = $cfg.agentBlueprintServicePrincipalObjectId

$obs = Get-MgServicePrincipal -Filter "appId eq '9b975845-388f-4429-889e-eab1ef63949c'"
$rid = ($obs.AppRoles | Where-Object { $_.Value -eq 'Agent365.Observability.OtelWrite' }).Id

New-MgServicePrincipalAppRoleAssignment `
  -ServicePrincipalId $agentSpId `
  -PrincipalId        $agentSpId `
  -ResourceId         $obs.Id `
  -AppRoleId          $rid
```

The successful response is a JSON object containing `AppRoleId`, `PrincipalId`, `ResourceId`, and `CreatedDateTime`. If you get `Insufficient privileges`, the signed-in account is not a Global Admin in that tenant.

### 1.6. Verify in Entra

`https://entra.microsoft.com` → **App registrations** → search `inditex-test-A365`. You should see:

- `inditex-test-A365 Blueprint` (the app)
- `inditex-test-A365 Identity` (the SP)
- The Identity SP has, under **Permissions → API permissions**, an **Application** permission `Agent365.Observability.OtelWrite` with status `Granted for <tenant>` (green check).

---

## Phase 2 — Dev loop (any machine)

This phase is the one a developer iterates on every time they pull a fresh checkout or change agent code.

### 2.1. Clone & enter the project

```bash
git clone <repo>
cd <repo>/BasicAgent365
```

### 2.2. Optional: dev container

If the repo ships a `.devcontainer/`, open the folder in VS Code → "Reopen in Container". Otherwise a regular Python 3.10+ virtualenv on your host is fine.

### 2.3. Install Python deps + sign in to Azure for AOAI

```bash
pip install --pre -r requirements-a365.txt
az login                       # required: agent_a365.py uses DefaultAzureCredential to call AOAI
```

### 2.4. Bring the provisioning artefacts into the project

You need two things from Phase 1:

1. The file `a365.generated.config.json` (copy/upload it into `BasicAgent365/`)
2. The **plaintext** Blueprint client secret you copied in 1.4.1

If you are working in VS Code with a dev container, simply drag-drop the file from your host File Explorer onto the `BasicAgent365/` folder in the VS Code Explorer.

Then run the import script:

```bash
python import_a365_config.py
```

It will prompt for:

| Prompt | Answer |
|--------|--------|
| `AGENT365_TENANT_ID [default: …]` | press **Enter** (auto-detected from `az account`) |
| `AGENT365_CLIENT_SECRET (hidden)` | **paste** the plaintext secret from Phase 1.4.1 |
| `AGENT365_AGENT_NAME [default: inditex-test-A365]` | **Enter** (or override) |
| `AGENT365_AGENT_DESCRIPTION` | **Enter** |
| `AZURE_OPENAI_ENDPOINT` | your AOAI resource URL |
| `AZURE_OPENAI_DEPLOYMENT` | your model deployment name (e.g. `gpt-4o`) |
| `AZURE_OPENAI_API_VERSION [default: …]` | **Enter** |

The script writes `.env` with `chmod 600`. Both `.env` and `a365.generated.config.json` are already in `.gitignore`.

### 2.5. Smoke test: the 3-hop token chain

```bash
python - <<'PY'
import os; from dotenv import load_dotenv; load_dotenv()
from observability_token_service import acquire_observability_token
import token_cache, logging; logging.basicConfig(level=logging.INFO)
acquire_observability_token(
    tenant_id               = os.environ['AGENT365_TENANT_ID'],
    agent_id                = os.environ['AGENT365_AGENT_ID'],
    blueprint_client_id     = os.environ['AGENT365_CLIENT_ID'],
    blueprint_client_secret = os.environ['AGENT365_CLIENT_SECRET'],
)
tok = token_cache.get_cached_token(os.environ['AGENT365_AGENT_ID'], os.environ['AGENT365_TENANT_ID'])
print('Token cached:', len(tok), 'chars')
PY
```

Expected: two `INFO ... FMI hop` lines and `Token cached: ~1676 chars`. If you see `AADSTS70021: No matching federated identity record found`, Phase 1.4 didn't finish writing the FIC — re-run it.

### 2.6. Run the agent

```bash
python agent_a365.py
```

You should see:

```
INFO agent_a365: Acquiring Agent 365 observability token via 3-hop FMI chain...
INFO observability_token_service: FMI hop 1+2 succeeded (Blueprint -> Agent Identity assertion).
INFO observability_token_service: FMI hop 3 succeeded; observability bearer cached.
INFO microsoft.opentelemetry.a365.core.opentelemetry_scope: Span started: 'invoke_agent inditex-test-A365' ...
...
You: what time is it in madrid?
```

Type `exit` to quit; the force-flush will run on shutdown.

### 2.7. Verify in M365 admin center

`https://admin.cloud.microsoft/#/agents/all` → find `inditex-test-A365` → click → **Activity** tab. Spans should appear within ~30 s of an agent run.

> **If you see nothing**, the most likely cause is that the tenant **does not have a Microsoft Agent 365 SKU**. The ingest API will return `HTTP 200` but include `"sinks": {... "reason": "tenant_not_licensed"}` in the body. Re-run with `logging.DEBUG` and inspect for `agent365_exporter` lines to confirm. Code is correct; this is a tenant-licensing matter.

---

## What `agent_a365.py` actually does differently from `agent_basic.py`

Open both files side by side. The integration is **8 anchor blocks** marked with `# [A365] ...`. They are:

| Anchor | What it does |
|--------|--------------|
| 1. Statsbeat off | Sets `MICROSOFT_OTEL_SDKSTATS_DISABLED=true` before any import. |
| 2. SDK imports | Imports `use_microsoft_opentelemetry` + the scope/details classes. |
| 3. Env cleanup | Strips empty `AZURE_*` env vars to avoid `DefaultAzureCredential` confusion. |
| 4. Token resolver | A synchronous closure over `token_cache.get_cached_token(...)` for the exporter. |
| 5. SDK init | One call to `use_microsoft_opentelemetry(enable_a365=True, a365_use_s2s_endpoint=True, a365_enable_observability_exporter=True, a365_token_resolver=...)`. |
| 6. Scope wrap | The `run_agent` loop is wrapped in `InvokeAgentScope` → `InferenceScope` per LLM call → `ExecuteToolScope` per tool call. |
| 7. Token bootstrap | At `__main__` entry, call `acquire_observability_token(...)` once to populate the cache. |
| 8. Force-flush | A `finally` block that calls `tracer_provider.force_flush() + shutdown()` so the last spans are not lost. |

Everything else — the Responses API call, the tool dispatch, the conversation history — is **identical**.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---------|-------|-----|
| `AADSTS70021: No matching federated identity record found` | `a365 setup all` did not finish creating the FIC | Re-run Phase 1.4. |
| `AADSTS82001: Agentic apps cannot acquire tokens directly` | Code tried `acquire_token_for_client` on the Blueprint against the observability resource directly (skipping the FMI chain) | You're using the wrong sample; ensure `observability_token_service.py` is on `sys.path`. |
| `HTTP 403 insufficient_scope` on export | The **Application** `OtelWrite` role was never granted (only the Delegated one) | Run the Phase 1.5 snippet. |
| `HTTP 200` but `"reason": "tenant_not_licensed"` in body | Tenant has no Agent 365 SKU | License the tenant or test on a Frontier-program tenant. |
| Spans created locally but log says `A365 observability exporter not enabled` | Missing kwarg `a365_enable_observability_exporter=True` | Already set in this repo's `agent_a365.py`; check you haven't reverted it. |
| `EnvironmentCredential` errors at startup, complaining about a missing client secret | `python-dotenv` loaded an empty `AZURE_CLIENT_ID=""` | The anchor 3 cleanup handles this; check you didn't delete it. |
| Last 1–2 spans of a short-lived run are missing in the admin center | `BatchSpanProcessor` not flushed | Make sure the `finally` block calls `force_flush() + shutdown()`. |
| `a365 setup all` hangs forever on "Opening browser…" | You are running it inside a VS Code dev container — random callback port not pre-forwarded | Run on the host. See pre-flight table above. |
| Tool result contains an ISO 8601 timestamp and ingest returns `400 Unexpected JSON token "Date"` | A365 ingest auto-parses ISO strings as Date | Return human-readable timestamps from tools (see `get_current_time` in `agent_a365.py`). |

For the full conceptual background of any of these, see [docs/INTEGRATION_GUIDE.md §15](docs/INTEGRATION_GUIDE.md#15-verification-troubleshooting-and-the-gotchas-we-already-paid-for).

---

## Files generated at runtime (gitignored)

- `.env` — your real credentials. Never commit.
- `a365.generated.config.json` — Microsoft's IDs + DPAPI-protected secret (Windows only).
- `.env.bak` — backup written by `import_a365_config.py` before overwriting `.env`.
- `__pycache__/` — Python bytecode.

---

## Next steps

- Read [docs/INTEGRATION_GUIDE.md](docs/INTEGRATION_GUIDE.md) for the conceptual deep-dive (identity, threat model, security considerations, production hardening).
- See the sibling folders `LangChainAgent365/` and `LangGraphAgent365/` for the same A365 integration applied to LangChain `AgentExecutor` and LangGraph `create_react_agent` patterns respectively.
