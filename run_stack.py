#!/usr/bin/env python3
"""One-command launcher: starts the LLM gateway (backend API) and the booth app
(frontend) together. Used by run_windows.bat and run_mac_linux.sh.

    python run_stack.py

What it does:
  1. Creates the gateway virtualenv (.venv-gateway) and installs
     gateway/requirements.txt on first run. On every run it VERIFIES the
     dependencies actually import (fastapi, uvicorn, httpx) and repairs the
     environment automatically if a previous install failed or was interrupted.
  2. Reads gateway/.env itself (no extra packages needed) and passes the values
     to the gateway process. Secrets stay in gateway/.env; nothing is printed.
  3. Starts the gateway on GATEWAY_HOST:GATEWAY_PORT (default 127.0.0.1:8100)
     and waits until /healthz answers.
  4. Starts the booth (app.py) on BOOTH_HOST:BOOTH_PORT (default 0.0.0.0:8000)
     wired to the gateway with the system-1 server token.
  5. Ctrl+C stops both.

Optional env vars: BOOTH_HOST, BOOTH_PORT, PLAP_OPEN=1 (open the browser).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENV_FILE = ROOT / "gateway" / ".env"
VENV_DIR = ROOT / ".venv-gateway"
REQS = ROOT / "gateway" / "requirements.txt"
IS_WIN = os.name == "nt"
VENV_PY = VENV_DIR / ("Scripts/python.exe" if IS_WIN else "bin/python")
REQUIRED_IMPORTS = "fastapi, uvicorn, httpx"
MIN_PY = (3, 9)


def say(msg: str) -> None:
    print("[run_stack] " + msg, flush=True)


def fail(msg: str) -> None:
    say("ERROR: " + msg)
    if IS_WIN:
        try:
            input("Press Enter to close...")
        except EOFError:
            pass
    sys.exit(1)


def parse_env_file(path: Path) -> dict:
    """Minimal .env parser: KEY=VALUE lines, surrounding quotes stripped."""
    env: dict = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            env[key] = value
    return env


def deps_ok() -> bool:
    """True if the venv python exists AND the gateway deps actually import."""
    if not VENV_PY.exists():
        return False
    try:
        subprocess.check_call(
            [str(VENV_PY), "-c", "import " + REQUIRED_IMPORTS],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True
    except (subprocess.CalledProcessError, OSError):
        return False


def create_venv() -> None:
    if VENV_DIR.exists():
        say("Removing the broken virtualenv so it can be rebuilt...")
        shutil.rmtree(VENV_DIR, ignore_errors=True)
    say("Creating the gateway virtualenv...")
    try:
        subprocess.check_call([sys.executable, "-m", "venv", str(VENV_DIR)])
    except subprocess.CalledProcessError:
        fail("Could not create the virtualenv. On Ubuntu/Debian install it "
             "first with: sudo apt install python3-venv")
    if not VENV_PY.exists():
        fail("The virtualenv was created but its python executable is missing "
             "at " + str(VENV_PY) + ". Delete the .venv-gateway folder and "
             "run this again.")


def install_deps() -> bool:
    """Install gateway requirements into the venv. Returns True on success.
    Output is NOT silenced so pip errors (network, proxy, SSL) are visible."""
    say("Installing gateway dependencies (fastapi, uvicorn, httpx)...")
    try:
        subprocess.check_call([str(VENV_PY), "-m", "pip", "install",
                               "--disable-pip-version-check", "-r", str(REQS)])
        return True
    except subprocess.CalledProcessError:
        return False


def ensure_env() -> None:
    """Guarantee a working venv with importable deps, repairing as needed.

    Handles every known first-run failure mode:
      - venv never created                     -> create + install
      - venv created but install interrupted   -> install into existing venv
      - venv corrupted (python won't run)      -> rebuild from scratch
      - pip install fails                      -> rebuild once, then clear error
    """
    if deps_ok():
        return

    if VENV_PY.exists():
        say("Gateway dependencies are missing or broken in .venv-gateway. "
            "Repairing (this can happen if the first install was "
            "interrupted)...")
    else:
        say("First run: setting up the gateway environment (needs internet "
            "once)...")
        create_venv()

    if install_deps() and deps_ok():
        say("Gateway environment ready.")
        return

    # One full rebuild in case the venv itself is corrupted.
    say("Install did not succeed. Rebuilding the environment from scratch...")
    create_venv()
    if install_deps() and deps_ok():
        say("Gateway environment ready.")
        return

    fail("Could not install the gateway dependencies (fastapi, uvicorn, "
         "httpx). Check the pip output above. Common causes: no internet "
         "connection, or a corporate proxy/firewall blocking pypi.org. "
         "Fix the connection, delete the .venv-gateway folder, and run "
         "this again.")


def wait_http(url: str, proc: subprocess.Popen | None = None,
              tries: int = 60, delay: float = 0.5) -> bool:
    """Poll a URL until it answers. If proc dies while waiting, stop early."""
    for _ in range(tries):
        if proc is not None and proc.poll() is not None:
            return False
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if 200 <= resp.status < 500:
                    return True
        except Exception:
            time.sleep(delay)
    return False


def main() -> None:
    os.chdir(ROOT)

    if sys.version_info < MIN_PY:
        fail("Python %d.%d or newer is required, but this is Python %d.%d. "
             "Install a current Python from python.org and run again."
             % (MIN_PY[0], MIN_PY[1], sys.version_info[0], sys.version_info[1]))

    if not ENV_FILE.exists():
        fail("gateway/.env not found. Copy gateway/.env.example to gateway/.env "
             "and fill in the Anthropic keys, server tokens, and salt first.")
    env_values = parse_env_file(ENV_FILE)
    if any("PASTE-REAL" in v for v in env_values.values()):
        fail("gateway/.env still contains placeholder keys. Replace every "
             "PASTE-REAL-... line with a real Anthropic API key.")

    tokens_raw = env_values.get("GATEWAY_SERVER_TOKENS", "")
    booth_token = next((part.split(":", 1)[1] for part in tokens_raw.split(",")
                        if part.strip().startswith("system-1:")), "")
    if not booth_token:
        fail("GATEWAY_SERVER_TOKENS in gateway/.env has no system-1 token.")

    ensure_env()

    gw_host = env_values.get("GATEWAY_HOST", "127.0.0.1")
    gw_port = env_values.get("GATEWAY_PORT", "8100")
    booth_host = os.environ.get("BOOTH_HOST", "0.0.0.0")
    booth_port = os.environ.get("BOOTH_PORT", "8000")
    gateway_url = f"http://{'127.0.0.1' if gw_host in ('0.0.0.0', '') else gw_host}:{gw_port}"

    procs = []
    try:
        say(f"Starting the LLM gateway on {gw_host}:{gw_port} ...")
        gw_env = {**os.environ, **env_values}
        gw = subprocess.Popen([str(VENV_PY), "-m", "gateway.main"], env=gw_env, cwd=str(ROOT))
        procs.append(gw)

        if not wait_http(gateway_url + "/healthz", proc=gw):
            if gw.poll() is not None:
                fail("The gateway process exited immediately (exit code "
                     + str(gw.returncode) + "). Read the error printed above "
                     "this line. If it mentions a missing module, delete the "
                     ".venv-gateway folder and run this again. If port "
                     + str(gw_port) + " is already in use, close the other "
                     "program or change GATEWAY_PORT in gateway/.env.")
            fail("The gateway did not come up on " + gateway_url +
                 " - check gateway/.env and try again.")
        try:
            with urllib.request.urlopen(gateway_url + "/healthz", timeout=3) as resp:
                keys = json.load(resp).get("keys", {})
            say(f"Gateway is up ({keys.get('active_available', '?')} key(s) available, "
                f"8 concurrent slots).")
        except Exception:
            say("Gateway is up.")

        say(f"Starting the booth app on {booth_host}:{booth_port} ...")
        booth_env = {**os.environ, "GATEWAY_URL": gateway_url, "GATEWAY_TOKEN": booth_token}
        booth_cmd = [sys.executable, "app.py", "--host", booth_host, "--port", str(booth_port)]
        if os.environ.get("PLAP_OPEN", "").strip() in ("1", "true", "yes"):
            booth_cmd.append("--open")
        booth = subprocess.Popen(booth_cmd, env=booth_env, cwd=str(ROOT))
        procs.append(booth)

        if not wait_http(f"http://127.0.0.1:{booth_port}/api/health", proc=booth):
            if booth.poll() is not None:
                fail("The booth app exited immediately (exit code "
                     + str(booth.returncode) + "). Read the error printed "
                     "above this line. If port " + str(booth_port) + " is "
                     "already in use, close the other program or set "
                     "BOOTH_PORT to a free port.")
            fail("The booth app did not come up on port " + str(booth_port))

        say("")
        say("Everything is running:")
        say(f"  Player app  -> http://localhost:{booth_port}/ (leaderboard is the Leaderboard tab)")
        say(f"  Gateway     -> {gateway_url}/healthz (internal; AI scoring)")
        say("Press Ctrl+C to stop both.")

        # Stay up while both children run; if either dies, shut down cleanly.
        while True:
            for p in procs:
                if p.poll() is not None:
                    raise KeyboardInterrupt
            time.sleep(1)
    except KeyboardInterrupt:
        say("Stopping...")
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        deadline = time.time() + 5
        for p in procs:
            while p.poll() is None and time.time() < deadline:
                time.sleep(0.1)
            if p.poll() is None:
                p.kill()
        say("Stopped.")


if __name__ == "__main__":
    main()
