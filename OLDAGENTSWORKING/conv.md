
You said:
Necesito que investigues cómo se haría para integrar en Agent 365 un agente de IA hecho en un stak y un framework que un cliente ha desarrollado por si mismo....  ​‌

Lo que hacen es que han hecho su propio framework como quien dice y sólo consumen ahora los LLM de los hiperscalers como Microsoft, pero no usan ningun SDK de Microsoft ni de nadie, sabes???​‌

Agent 365 es un producto super nuevo, tendrás que investigarlo en profundidad antes de poder contestar. Tienes docs Microsoft Agent 365 documentation | Microsoft Learn(se abre en una nueva ventana) pero hay que explorar toda esa documentación y otros ejemplos

Proceso de razonamiento

Proceso de razonamiento

Copilot said:
Voy a investigar a fondo Microsoft Agent 365 y las opciones de integración para agentes externos construidos sin SDKs de Microsoft.





Investigación profunda Agent 365
Busqué "Agent 365" "A2A" "Agent2Agent" protocol interoperability Microsoft en la Web; 5 resultados
You said:
En la documentación oficial, sólo dice esto:

Microsoft Agent 365: The Control Plane for Agents(se abre en una nueva ventana)

Any agent published through Microsoft 365 channels and registered with an Entra Agent ID will automatically appear in the Agent 365 inventory. Agents built outside these environments will require additional steps.​‌

Pero yo no he podido encontrar los "cómo"

Proceso de razonamiento

Proceso de razonamiento

Leer /mnt/workspace/output/microsoft-agent-365-integration-report.md
Copilot said:
Tienes toda la razón, Kiko — esa frase en la web pública ("Agents built outside these environments will require additional steps") es la pieza de marketing, pero los "cómo" están escondidos en otra parte del árbol de Microsoft Learn (no en la home de marketing, sino en 
...
 y en el manifest de Teams/M365 v1.27). Por eso no los encontrabas. Te lo bajo a tierra:

El "cómo" en cuatro pasos concretos
Los "additional steps" que la página no detalla son, en realidad, esta secuencia:

Paso 1 — Crear un Entra Agent ID blueprint para el agente del cliente
Es la identidad que Agent 365 usa para gobernar al agente. NO necesitas el CLI a365 ni el SDK; se puede crear con un POST a Microsoft Graph beta:

Endpoint: 
agentIdentityBlueprints
Permisos: AgentIdentityBlueprint.Create, AgentIdentityBlueprint.ReadWrite.All
Resultado: un service principal de tipo agente con credenciales (managed identity FIC, secret o cert)
Estado: preview a día de hoy
Esto es lo que materialmente "registra" el agente en el plano de control. Ejemplo verificado (PowerShell puro contra Graph, sin SDK Microsoft) en el post de Will Velida (MVP)(se abre en una nueva ventana).

Paso 2 — Exponer el agente del cliente bajo un contrato HTTP abierto
Aquí está la decisión arquitectónica clave. Hay dos contratos públicos que Agent 365 entiende y ninguno requiere SDK:

Contrato	Qué expone el cliente	Para qué sirve
MCP remote server (JSON-RPC sobre HTTPS/SSE)	Un endpoint mcpServerUrl que implementa 
list
, 
call
, initialize	Las capacidades del agente como herramientas
Activity Protocol (Bot Framework activities, JSON)	
messages
 que recibe/responde Activities y valida JWT de Entra	El agente como entidad conversable (Custom Engine Agent)
Ambos son schemas JSON públicos. El cliente los implementa en su framework (Python, Node, Go, Java, lo que sea) llamando a su LLM hyperscaler por dentro. Sin SDK.

Paso 3 — Empaquetar un manifest M365 v1.27 (.zip) que apunte a esos endpoints
Aquí es donde la docu oficial pública no te lleva. La referencia real está en:

Agent connectors documentation(se abre en una nueva ventana)

Fragmento canónico (esto es lo que el cliente necesita producir):

json

{
  "$schema": "https://developer.microsoft.com/json-schemas/teams/v1.27/MicrosoftTeams.schema.json",
  "manifestVersion": "1.27",
  "agentConnectors": [{
    "id": "client-agent-mcp",
    "displayName": "Agente del Cliente",
    "toolSource": {
      "remoteMcpServer": {
        "mcpServerUrl": "https://agent.cliente.example.com/mcp",
        "authorization": { "type": "OAuthPluginVault", "referenceId": "cliente-oauth" }
      }
    }
  }]
}
Si además quieres agente conversable, añades el bloque copilotAgents.customEngineAgents (manifest v1.22+) apuntando al bot ID.

Paso 4 — Publicar al org catalog (no al Marketplace público)
Sube el .zip vía Agent Builder → Submit to your org catalog, o
Directamente en M365 Admin Center → Agents → All agents → Requests
Un Global/Teams Admin lo aprueba y aparece en el Agent Store del tenant + en el Registry de Agent 365
La incertidumbre honesta que debes trasladar al cliente
Hay tres huecos que la documentación pública no cierra y conviene que el cliente los conozca antes de comprometerse:

A2A no es vía de registro en el control plane de Agent 365 a día de hoy — solo es protocolo de comunicación agente-a-agente en Foundry/Copilot Studio. Si el cliente esperaba "publicar por A2A", hoy no existe ese camino documentado.
El auto-registro tipo Vertex/Bedrock (que Microsoft sí soporta nativamente para esos dos) no está disponible como API pública para que terceros lo implementen. No hay "endpoint genérico de provider".
Validar el JWT de Entra para el Messaging Bot API sin SDK es factible (es OAuth 2.0 + JWKS estándar) pero Microsoft no publica un tutorial paso a paso sin SDK. El cliente tendrá que armarlo siguiendo specs OAuth genéricas.
Mi recomendación pragmática
Para minimizar riesgo y esfuerzo: Opción A (MCP server) primero, complementada con un Declarative Agent ligero para la UX en Copilot. Mantiene 100% el framework del cliente, mínima superficie de contrato, y deja la puerta abierta a añadir CEA después si quieren presencia conversacional propia (mailbox, Teams).

Tienes el informe completo con tabla comparativa A/B/C/D, requisitos técnicos detallados, citas y banderas de incertidumbre en 

microsoft-agent-365-integration-report.md
. ¿Quieres que profundice en alguno de los pasos — por ejemplo, el detalle del flujo OAuth para el MCP server o un ejemplo de Activity Protocol mínimo?




You said:
Ok, interesante lectura.... Pero cómo hago para tener la capa de Observabilidad de estos agentes???? Con eso que me dices no es suficiente entiendo, porque lo único que pasaría es que hago que mi organización "se entere" de que existe un agente porque le he dado una identidad en el entorno, pero nada más, no sé que pasa por ahí ni con quien habla ni quien lo usa ni nada no? Para eso yo entendía que estaba el SDK pero no veo como se hace esa implementación sabes???

Proceso de razonamiento

Proceso de razonamiento

Copilot said:
Excelente pregunta — pegas en el punto exacto donde el SDK aporta valor real. Tener identidad + manifest = "el tenant sabe que el agente existe", pero observabilidad (qué hace, con quién habla, qué datos toca, errores, latencia, coste) es una capa distinta. Déjame investigar específicamente cómo funciona esa capa y si se puede alimentar sin SDK.


microsoft-agent-365-integration-report.md
Markdown document
Open in OneDrive




Observabilidad Agent 365 sin SDK
You said:
OJo, que yo no te he pedido que se tenga que ahcer sin el SDK....​‌

A mi entender, lo que había que hacer sobre cualquier agente "custom" es que hay que meterles, precisamente, el SDK para que puedan ser gestionados. Pero es que no he encontrado en ningún lado esto que te digo, sólo lo he intuido....​‌

Si se puede hacer sin SDK, pues mejor, pero yo asumo que será necesario tener el SDK