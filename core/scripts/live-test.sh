#!/usr/bin/env bash
# Read-only live tests for accounts A and B.
# Credentials from .env (copy .env.example). Without .env, prompts for account A
# so nothing lands in shell history.
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f .env && -z "${CHOMIK_A_USER:-}" ]]; then
  read -rp "Chomikuj user (account A): " CHOMIK_A_USER
  read -rsp "Password: " CHOMIK_A_PASSWORD; echo
  export CHOMIK_A_USER CHOMIK_A_PASSWORD
fi
exec .venv/bin/pytest -m live -v --tb=short -o log_cli=true -o log_cli_level=INFO "$@"
