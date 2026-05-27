# Inditex — Microsoft Agent 365 PoC ✅ FUNCIONANDO

PoC end-to-end de cómo integrar un **agente "BYO" autónomo** (no conversacional — dispara procesos en tiendas y backoffice) con Microsoft Agent 365 para obtener gobierno, identidad federada y observabilidad de uso/acceso.

**Estado**: Spans `invoke_agent`, `Chat` (inference) y `execute_tool` se exportan al endpoint de Agent 365 con `HTTP 200, rejectedSpans: 0`.

## Lo que demuestra esta PoC

1. **Identidad federada Blueprint + Agent Identity** vía `a365 setup all` (sin client secret en producción — solo Managed Identity).
2. **3-hop FMI token chain** mintando el bearer para la API de observabilidad, sin necesidad de `TurnContext` ni de bot conversacional.
3. **Instrumentación OTEL** (`InvokeAgentScope` → `InferenceScope` → `ExecuteToolScope`) sobre un agente que sigue siendo 100% código propio (raw `requests` a Azure OpenAI, sin SDK de OpenAI).
4. **Export real** al servicio Agent 365 de Microsoft (`agent365.svc.cloud.microsoft/observabilityService/...`).

## Ficheros del repo

```
inditexA365/
├── .env                              # secrets (gitignored). Lo rellena a365 setup all + ajustes manuales.
├── .env.example                      # plantilla con todos los AGENT365_* documentados
├── .gitignore
├── README.md                         # este fichero
├── requirements.txt                  # deps del agente BYO básico (requests, azure-identity, dotenv)
├── requirements-a365.txt             # deps del agente con A365 (microsoft-opentelemetry, msal, ...)
├── agent_basic.py                    # Paso 1: agente BYO sin ningún SDK
├── agent_a365.py                     # Paso 2: el MISMO agente instrumentado con A365 ✅
├── observability_token_service.py    # 3-hop FMI chain (MSAL, sync, client_secret)
├── token_cache.py                    # (agent_id, tenant_id) → bearer + expiry, in-memory
├── grant_app_role.ps1                # workaround: asigna el app role OtelWrite al agent identity
├── a365.generated.config.json        # output de `a365 setup all` (gitignored)
└── .venv/                            # venv (gitignored)
```

## Cómo correr la PoC desde cero

```powershell
cd C:\repos\inditexA365
.\.venv\Scripts\Activate.ps1

# 1) Provisión Entra (crea Blueprint + Agent Identity + FIC + Managed Identity)
a365 setup all --agent-name "inditex-demo-agent" --tenant-id "<tenantId>"
# Te imprime el blueprint client secret UNA VEZ → guárdalo en .env como AGENT365_CLIENT_SECRET

# 2) Workaround obligatorio: asigna el app role Agent365.Observability.OtelWrite
.\grant_app_role.ps1

# 3) Ejecuta el agente
az login                  # para el credential de Azure OpenAI (DefaultAzureCredential)
python agent_a365.py
```

Verás:
```
FMI hop 1+2 succeeded ...
FMI hop 3 succeeded; observability token cached.
Span started/ended: invoke_agent / Chat / execute_tool / Chat
AGENT response: Current UTC time: ...
[con DEBUG] Sending chunk 1 of 1 (4 spans, ~6KB) → HTTP 200 success
```

## Lecciones aprendidas (importantes para repetir esto en cliente)

### 1. Hay DOS identidades Entra, no una

`a365 setup all` crea como apps separadas:

- **Blueprint** (`AGENT365_BLUEPRINT_ID` / `AGENT365_CLIENT_ID`): autentica con client secret en dev / Managed Identity en prod. Tiene una **Federated Identity Credential (FIC)** apuntando a la siguiente.
- **Agent Identity** (`AGENT365_AGENT_ID`): NUNCA se autentica directamente. Solo por `client_assertion = T1` emitido por el Blueprint. Es la que tiene el app role `Agent365.Observability.OtelWrite` sobre el recurso de observabilidad.

### 2. El SDK correcto NO es `microsoft-agents-a365-observability-core`

Ese paquete está pensado para bots hosted con `TurnContext`. Para un worker autónomo (BYO) hay que usar la **Microsoft OpenTelemetry Distro**:

```python
from microsoft.opentelemetry import use_microsoft_opentelemetry
use_microsoft_opentelemetry(
    enable_a365=True,
    a365_use_s2s_endpoint=True,
    a365_token_resolver=lambda agent_id, tenant_id: my_cache.get(...),
)
```

El `a365_token_resolver` admite cualquier callable, sin dependencia de un framework de hosting.

### 3. El 3-hop FMI token flow (MSAL, sync)

```python
# Hop 1+2 — Blueprint pide un token de tipo "exchange" a nombre del agent
t1 = blueprint.acquire_token_for_client(
    scopes=["api://AzureADTokenExchange/.default"],
    fmi_path=AGENT_ID,                        # ← clave: el fmi_path
)["access_token"]

# Hop 3 — Agent identity mintea el token real usando T1 como assertion
agent_app = ConfidentialClientApplication(
    client_id=AGENT_ID,
    client_credential={"client_assertion": t1},
    authority=f"https://login.microsoftonline.com/{TENANT_ID}",
)
obs_token = agent_app.acquire_token_for_client(
    scopes=["api://9b975845-388f-4429-889e-eab1ef63949c/.default"]
)["access_token"]
```

Esto **funciona sin que Entra responda `AADSTS82001`** porque la agent identity no pide app-only directamente — entra vía assertion federada del blueprint.

### 4. El `a365 setup all` deja DOS permisos pendientes que él mismo no puede otorgar

- ❌ `Agent365.Observability.OtelWrite` como **app role** sobre la API de observabilidad — su intento es `oauth2PermissionGrant` (delegated), pero el flow es app-only → necesita `appRoleAssignment`. Lo arreglamos con `grant_app_role.ps1`.
- ❌ `Connectivity.Connections.Read` en Power Platform — no necesario para observabilidad. Skip.

### 5. Bug del servidor con timestamps ISO 8601

El ingest endpoint de Agent 365 **auto-parsea strings ISO 8601 como tipos Date** y luego falla validación OTLP con:

```
HTTP 400 — Unexpected JSON token "Date" at path "['gen_ai.tool.call.result']"
```

Workaround: devolver timestamps en formato humano (`"15 May 2026, 01:25:52 (UTC)"`), no `datetime.isoformat()`.

### 6. Statsbeat ruido

La distro envía telemetría interna a Application Insights aunque pongas `enable_azure_monitor=False`. En entornos sin trust del cert raíz aparece un `SSL: CERTIFICATE_VERIFY_FAILED`. Solución: setear `MICROSOFT_OTEL_SDKSTATS_DISABLED=true` (ya hecho en `agent_a365.py`).

## Para el cliente (Inditex)

La PoC valida que sí pueden meter sus agentes autónomos custom en Agent 365:

- Sin acoplarse al SDK conversacional de M365 Agents.
- Sin pasar por Copilot / Teams / Outlook como front.
- Conservando 100% de su código de orquestación.

A cambio obtienen:
- **Inventario** central de agentes y su identidad federada.
- **Audit logs** end-to-end (`invoke_agent`, `inference`, `execute_tool`) por agente y por usuario que disparó la ejecución (capturable vía `CallerDetails`).
- **Posibilidad futura** de gobierno (políticas, suspensión, DLP) sobre esas identidades sin re-arquitectar los agentes.

## Referencias

- Sample oficial: https://github.com/microsoft/Agent365-Samples/tree/main/python/autonomous/github-trending
- Microsoft OpenTelemetry Distro: https://learn.microsoft.com/en-us/microsoft-agent-365/developer/microsoft-opentelemetry
- Agent 365 observability: https://learn.microsoft.com/en-us/microsoft-agent-365/developer/observability

