"""Create or update the Vapi assistant from code, so the voice agent is versioned with the backend.

Usage:
  python scripts/vapi_setup.py --server-url https://your-app.onrender.com --export-only   # writes vapi/assistant.json
  python scripts/vapi_setup.py --server-url https://your-app.onrender.com                 # creates/updates on Vapi

Env: VAPI_API_KEY (private key), VAPI_SECRET (same as the backend), optional VAPI_ASSISTANT_ID to update,
VAPI_CREDENTIAL_ID (Bearer credential made in the Vapi dashboard; preferred over inline headers),
CLINIC_NAME, CLINIC_TIMEZONE, CLINIC_PHONE (transfer target), LLM_MODEL.
"""

import argparse
import json
import os
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.tools import TOOL_SCHEMAS  # noqa: E402

TOOL_MESSAGES = {
    "check_availability": "One moment, let me check the schedule.",
    "book_appointment": "Great, booking that for you now.",
    "reschedule_appointment": "Okay, moving your appointment.",
    "cancel_appointment": "Okay, one second.",
    "find_appointment": "Let me pull up your appointment.",
}


def build_assistant(server_url: str) -> dict:
    s = get_settings()
    secret = os.getenv("VAPI_SECRET", s.vapi_secret)
    credential_id = os.getenv("VAPI_CREDENTIAL_ID")
    webhook = server_url.rstrip("/") + "/vapi/webhook"
    server = {"url": webhook, "timeoutSeconds": 20}
    if credential_id:
        server["credentialId"] = credential_id
    elif secret:
        server["headers"] = {"Authorization": f"Bearer {secret}"}

    prompt = (ROOT / "vapi" / "system_prompt.md").read_text()
    prompt = prompt.replace("{{clinic_name}}", s.clinic_name).replace("{{timezone}}", s.clinic_timezone)

    tools = []
    for schema in TOOL_SCHEMAS:
        tool = {"type": "function", "function": schema, "server": server}
        if schema["name"] in TOOL_MESSAGES:
            tool["messages"] = [{"type": "request-start", "content": TOOL_MESSAGES[schema["name"]]}]
        tools.append(tool)
    tools.append(
        {
            "type": "transferCall",
            "destinations": [{"type": "number", "number": s.clinic_phone, "message": "Sure, connecting you to the front desk now."}],
        }
    )

    return {
        "name": f"{s.clinic_name} Receptionist",
        "firstMessage": f"Thanks for calling {s.clinic_name}, this is Ava. How can I help you today?",
        "model": {
            "provider": "openai",
            "model": os.getenv("LLM_MODEL", "gpt-4.1"),
            "temperature": 0.3,
            "messages": [{"role": "system", "content": prompt}],
            "tools": tools,
        },
        "voice": {"provider": "11labs", "voiceId": os.getenv("VOICE_ID", "21m00Tcm4TlvDq8ikWAM")},
        "transcriber": {"provider": "deepgram", "model": "nova-3", "language": "en"},
        "server": server,
        "serverMessages": ["tool-calls", "end-of-call-report"],
        "endCallFunctionEnabled": True,
        "maxDurationSeconds": 600,
        "silenceTimeoutSeconds": 30,
        "backgroundDenoisingEnabled": True,
        "analysisPlan": {
            "summaryPlan": {
                "messages": [
                    {"role": "system", "content": "Summarise this dental clinic call in one sentence: who called, what they wanted, and the outcome."},
                    {"role": "user", "content": "Transcript:\n{{transcript}}"},
                ]
            }
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-url", required=True, help="Public base URL of the deployed backend")
    parser.add_argument("--export-only", action="store_true")
    args = parser.parse_args()

    assistant = build_assistant(args.server_url)
    out = ROOT / "vapi" / "assistant.json"
    exported = json.loads(json.dumps(assistant))
    if "headers" in exported["server"]:  # never write the secret to a file that may be committed
        redacted = {"Authorization": "Bearer <VAPI_SECRET>"}
        exported["server"]["headers"] = redacted
        for tool in exported["model"]["tools"]:
            if "server" in tool:
                tool["server"]["headers"] = redacted
    out.write_text(json.dumps(exported, indent=2))
    print(f"Wrote {out}")
    if args.export_only:
        return

    key = os.environ["VAPI_API_KEY"]
    headers = {"Authorization": f"Bearer {key}"}
    assistant_id = os.getenv("VAPI_ASSISTANT_ID")
    if assistant_id:
        r = httpx.patch(f"https://api.vapi.ai/assistant/{assistant_id}", json=assistant, headers=headers, timeout=30)
    else:
        r = httpx.post("https://api.vapi.ai/assistant", json=assistant, headers=headers, timeout=30)
    if r.status_code >= 300:
        sys.exit(f"Vapi error {r.status_code}: {r.text}")
    print(f"Assistant ready: {r.json()['id']}  (set VAPI_ASSISTANT_ID to update it next time)")


if __name__ == "__main__":
    main()
