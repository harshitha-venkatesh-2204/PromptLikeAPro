"""One-time (per day) AWS Bedrock credential setup for the gateway.

    python -m gateway.bedrock_login

What it does:
  1. Prompts for your AWS Access Key ID and Secret Access Key (secret is
     hidden as you type; nothing is echoed or logged).
  2. Calls STS ``get_session_token`` for temporary credentials valid 24 hours
     (86400 s) — the same flow as the manual snippet, so your long-term key
     never sits in the .env file. If STS minting is not permitted (e.g. root
     credentials cap at 1 hour), it falls back to writing the keys directly
     after asking you.
  3. Writes BEDROCK_ACCESS_KEY / BEDROCK_SECRET_KEY / BEDROCK_SESSION_TOKEN,
     GATEWAY_PROVIDER=bedrock, BEDROCK_REGION, and the Haiku model id into
     ``gateway/.env`` (updating lines in place; everything else is preserved).
  4. Runs a "Say hello" smoke test against Bedrock so you know it works
     before starting the gateway.

Re-run this each booth day: session tokens expire after 24 hours.
"""
from __future__ import annotations

import getpass
import json
import pathlib
import sys
import warnings

warnings.filterwarnings("ignore")

REGION_DEFAULT = "us-east-1"
MODEL_ID_DEFAULT = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
SESSION_SECONDS = 86400  # 24 hours

ENV_PATH = pathlib.Path(__file__).with_name(".env")


def _update_env(path: pathlib.Path, updates: dict[str, str]) -> None:
    """Set key=value lines in the .env, replacing existing keys in place and
    appending any that are missing. All other lines/comments are preserved."""
    lines: list[str] = []
    if path.exists():
        lines = path.read_text(encoding="utf-8").splitlines()

    remaining = dict(updates)
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped and not stripped.startswith("#") else None
        if key in remaining:
            out.append(f"{key}={remaining.pop(key)}")
        else:
            out.append(line)

    if remaining:
        out.append("")
        out.append("# --- AWS Bedrock (written by gateway/bedrock_login.py) ---------------------")
        for k, v in remaining.items():
            out.append(f"{k}={v}")

    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def main() -> int:
    try:
        import boto3
    except ImportError:
        print("boto3 is not installed. Run:  pip install boto3")
        return 1

    print("=== Prompt Like A PRO — AWS Bedrock setup ===")
    access_key = input("AWS Access Key ID: ").strip()
    secret_key = getpass.getpass("AWS Secret Access Key (hidden): ").strip()
    if not access_key or not secret_key:
        print("Both keys are required. Nothing was written.")
        return 1

    region = input(f"Region [{REGION_DEFAULT}]: ").strip() or REGION_DEFAULT
    model_id = input(f"Model id [{MODEL_ID_DEFAULT}]: ").strip() or MODEL_ID_DEFAULT

    # --- Mint 24h temporary credentials via STS (preferred) -----------------
    creds = {
        "AccessKeyId": access_key,
        "SecretAccessKey": secret_key,
        "SessionToken": "",
    }
    try:
        sts = boto3.client(
            "sts",
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        tok = sts.get_session_token(DurationSeconds=SESSION_SECONDS)["Credentials"]
        creds = {
            "AccessKeyId": tok["AccessKeyId"],
            "SecretAccessKey": tok["SecretAccessKey"],
            "SessionToken": tok["SessionToken"],
        }
        print(f"OK: 24h session token minted (expires {tok['Expiration']}).")
    except Exception as exc:  # noqa: BLE001 - show the reason, then offer fallback
        print(f"Could not mint an STS session token: {exc}")
        answer = input("Write the long-term keys directly instead? [y/N]: ").strip().lower()
        if answer != "y":
            print("Nothing was written.")
            return 1

    # --- Write gateway/.env --------------------------------------------------
    _update_env(
        ENV_PATH,
        {
            "GATEWAY_PROVIDER": "bedrock",
            "BEDROCK_REGION": region,
            "BEDROCK_ACCESS_KEY": creds["AccessKeyId"],
            "BEDROCK_SECRET_KEY": creds["SecretAccessKey"],
            "BEDROCK_SESSION_TOKEN": creds["SessionToken"],
            "GATEWAY_MODEL": model_id,
            # Haiku 4.5 list pricing per MTok, for the gateway's cost estimates.
            "GATEWAY_PRICE_INPUT_PER_MTOK": "1.0",
            "GATEWAY_PRICE_OUTPUT_PER_MTOK": "5.0",
        },
    )
    print(f"Wrote credentials + provider settings to {ENV_PATH}")

    # --- Smoke test ----------------------------------------------------------
    print("Running smoke test (invoke_model: 'Say hello') ...")
    try:
        client = boto3.client(
            "bedrock-runtime",
            region_name=region,
            aws_access_key_id=creds["AccessKeyId"],
            aws_secret_access_key=creds["SecretAccessKey"],
            **({"aws_session_token": creds["SessionToken"]} if creds["SessionToken"] else {}),
        )
        response = client.invoke_model(
            modelId=model_id,
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 100,
                    "messages": [{"role": "user", "content": "Say hello"}],
                }
            ),
        )
        text = json.loads(response["body"].read())["content"][0]["text"]
        print(f"Bedrock replied: {text}")
        print("\nAll set. Start the gateway as usual (python -m gateway.main).")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"Smoke test FAILED: {exc}")
        print("Credentials were still written to .env; fix the issue and re-run.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
