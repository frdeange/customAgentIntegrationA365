"""
import_a365_config.py — Translate the artifacts produced by `a365 setup all`
into BasicAgent365/.env, ready for `python agent_a365.py`.

Typical workflow
----------------
1. On a workstation with a browser, run:

       a365 setup all --agent-name inditex-test-A365 --authmode s2s --verbose

   The CLI prints the blueprint client secret ONCE on the console and writes
   `a365.generated.config.json` to the current directory.

2. Copy `a365.generated.config.json` into this folder (BasicAgent365/).

3. From this folder, run:

       python import_a365_config.py

   The script reads the JSON, asks for the plaintext secret (because the
   JSON value is DPAPI-protected when produced on Windows), and writes a
   correctly populated `.env` (chmod 600). Any existing `.env` is backed up
   to `.env.bak` first.

This script never prints the secret to stdout.
"""

from __future__ import annotations

import getpass
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG_FILE = HERE / "a365.generated.config.json"
ENV_FILE = HERE / ".env"
ENV_BACKUP = HERE / ".env.bak"
ENV_EXAMPLE = HERE / ".env.example"


def fail(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def detect_az_tenant() -> str | None:
    try:
        out = subprocess.run(
            ["az", "account", "show", "--query", "tenantId", "-o", "tsv"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def parse_dotenv(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def ask(prompt: str, default: str = "", *, secret: bool = False) -> str:
    if secret:
        value = getpass.getpass(f"{prompt} (hidden): ")
        return value
    suffix = f" [{default}]" if default else ""
    value = input(f"{prompt}{suffix}: ").strip()
    return value or default


def main() -> int:
    if not CONFIG_FILE.exists():
        fail(
            f"{CONFIG_FILE.name} not found in {HERE}.\n"
            "Run `a365 setup all` on a workstation that can open a browser, "
            "then drop the generated file into this directory and re-run."
        )

    try:
        config = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        fail(f"{CONFIG_FILE.name} is not valid JSON: {exc}")

    blueprint_id = config.get("agentBlueprintId")
    agent_id = config.get("agenticAppId")
    protected = bool(config.get("agentBlueprintClientSecretProtected"))
    raw_secret = config.get("agentBlueprintClientSecret", "")

    if not blueprint_id or not agent_id:
        fail(
            "Required fields missing in the generated config "
            "(expected agentBlueprintId and agenticAppId)."
        )

    print("=" * 72)
    print("Agent 365 config import")
    print("=" * 72)
    print(f"  agentBlueprintId        : {blueprint_id}")
    print(f"  agenticAppId            : {agent_id}")
    print(f"  secret DPAPI-protected  : {protected}")
    print(f"  cliVersion              : {config.get('cliVersion', '<unknown>')}")
    print()

    existing = parse_dotenv(ENV_FILE)
    example_defaults = parse_dotenv(ENV_EXAMPLE)

    def previous(key: str) -> str:
        return existing.get(key) or example_defaults.get(key, "")

    # Tenant ID
    detected = detect_az_tenant()
    tenant_default = existing.get("AGENT365_TENANT_ID") or detected or ""
    tenant_id = ask("AGENT365_TENANT_ID", default=tenant_default)
    if not tenant_id:
        fail("Tenant ID is required.")

    # Client secret — DPAPI blob from the JSON is useless on Linux, prompt for plaintext
    if protected:
        print()
        print("The blueprint client secret in the JSON is DPAPI-protected "
              "(Windows-only encryption).")
        print("Paste the PLAINTEXT value that `a365 setup all` printed to console.")
        secret = ask("AGENT365_CLIENT_SECRET", secret=True)
    else:
        secret = raw_secret
        if not secret:
            secret = ask("AGENT365_CLIENT_SECRET", secret=True)
        else:
            print(f"Using plaintext secret read from {CONFIG_FILE.name}.")

    if not secret:
        fail("Client secret is required.")

    # Agent metadata
    agent_name = ask(
        "AGENT365_AGENT_NAME",
        default=existing.get("AGENT365_AGENT_NAME") or "inditex-test-A365",
    )
    agent_desc = ask(
        "AGENT365_AGENT_DESCRIPTION",
        default=(
            existing.get("AGENT365_AGENT_DESCRIPTION")
            or "Inditex PoC: minimalist Azure OpenAI agent integrated with Agent 365."
        ),
    )

    # Azure OpenAI — preserve existing values, allow overrides
    print()
    print("Azure OpenAI — press ENTER to keep existing values:")
    aoai_endpoint = ask("AZURE_OPENAI_ENDPOINT", default=previous("AZURE_OPENAI_ENDPOINT"))
    aoai_deployment = ask("AZURE_OPENAI_DEPLOYMENT", default=previous("AZURE_OPENAI_DEPLOYMENT"))
    aoai_api_version = ask(
        "AZURE_OPENAI_API_VERSION",
        default=previous("AZURE_OPENAI_API_VERSION") or "2024-10-21",
    )

    lines = [
        "# Generated by import_a365_config.py — do not commit",
        "",
        "# --- Azure OpenAI ---",
        f"AZURE_OPENAI_ENDPOINT={aoai_endpoint}",
        f"AZURE_OPENAI_DEPLOYMENT={aoai_deployment}",
        f"AZURE_OPENAI_API_VERSION={aoai_api_version}",
        "",
        "# --- Entra ID for AOAI (leave empty to use `az login` session) ---",
        f"AZURE_TENANT_ID={existing.get('AZURE_TENANT_ID', '')}",
        f"AZURE_CLIENT_ID={existing.get('AZURE_CLIENT_ID', '')}",
        f"AZURE_CLIENT_SECRET={existing.get('AZURE_CLIENT_SECRET', '')}",
        "",
        "# --- Agent 365 (from a365.generated.config.json + setup-time secret) ---",
        f"AGENT365_TENANT_ID={tenant_id}",
        f"AGENT365_AGENT_ID={agent_id}",
        f"AGENT365_BLUEPRINT_ID={blueprint_id}",
        f"AGENT365_CLIENT_ID={blueprint_id}",
        f"AGENT365_CLIENT_SECRET={secret}",
        f"AGENT365_AGENT_NAME={agent_name}",
        f"AGENT365_AGENT_DESCRIPTION={agent_desc}",
        "",
    ]

    if ENV_FILE.exists():
        shutil.copy2(ENV_FILE, ENV_BACKUP)
        print(f"Existing .env backed up to {ENV_BACKUP.name}.")

    ENV_FILE.write_text("\n".join(lines), encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        # Best-effort on non-POSIX filesystems
        pass

    print()
    print(f"Wrote {ENV_FILE} (chmod 600 if supported).")
    print()
    print("Next steps:")
    print("  1. Verify with: grep -v CLIENT_SECRET .env")
    print("  2. Install deps: pip install --pre -r requirements-a365.txt")
    print("  3. Smoke test:   python agent_a365.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
