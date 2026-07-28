#!/usr/bin/env bash
# Prompt Like A PRO - starts BOTH the backend LLM gateway and the game app.
# First run creates the gateway virtualenv automatically (needs internet once).
cd "$(dirname "$0")"
PLAP_OPEN=1 python3 run_stack.py
