import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo
from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI
from dotenv import load_dotenv

load_dotenv()


# =========================================================
# AZURE OPENAI CLIENT (Entra ID auth)
# =========================================================

client = AzureOpenAI(
    azure_endpoint=os.getenv("AZURE_OPENAI_ENDPOINT"),
    api_version=os.getenv("AZURE_OPENAI_API_VERSION"),
    azure_ad_token_provider=get_bearer_token_provider(
        DefaultAzureCredential(),
        "https://cognitiveservices.azure.com/.default",
    ),
)
MODEL = os.getenv("AZURE_OPENAI_DEPLOYMENT")


# =========================================================
# TOOLS
# =========================================================

def get_current_time():
    now = datetime.now(ZoneInfo("Europe/Madrid"))
    return {"current_time": now.strftime("%H:%M:%S %Z")}


TOOLS = {"get_current_time": get_current_time}

TOOL_DEFS = [
    {
        "type": "function",
        "name": "get_current_time",
        "description": "Obtiene la hora actual",
        "parameters": {"type": "object", "properties": {}},
    }
]


# =========================================================
# MEMORY
# =========================================================

conversation = [
    {
        "role": "system",
        "content": (
            "Eres un agente útil y minimalista. "
            "Puedes usar herramientas cuando sea necesario."
        ),
    }
]


# =========================================================
# AGENT LOOP
# =========================================================

def run_agent(user_input: str):
    conversation.append({"role": "user", "content": user_input})

    while True:
        response = client.responses.create(
            model=MODEL,
            input=conversation,
            tools=TOOL_DEFS,
        )

        # Volcamos a la conversación los items devueltos (mensajes + function_calls).
        # La Responses API stateless exige que el function_call esté presente
        # cuando enviemos el function_call_output en la siguiente vuelta.
        conversation.extend(item.model_dump() for item in response.output)

        calls = [i for i in response.output if i.type == "function_call"]

        if not calls:
            return response.output_text

        for call in calls:
            print(f"\n[TOOL CALL] {call.name}")
            result = TOOLS[call.name](**json.loads(call.arguments or "{}"))
            conversation.append({
                "type": "function_call_output",
                "call_id": call.call_id,
                "output": json.dumps(result),
            })


# =========================================================
# MAIN
# =========================================================

if __name__ == "__main__":
    print("=" * 50)
    print("AZURE OPENAI RESPONSES AGENT")
    print("=" * 50)
    print("Escribe 'exit' para salir\n")

    while (msg := input("Tú: ")).lower() != "exit":
        print(f"\nAgente: {run_agent(msg)}\n")
