#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
MODE="${1:---dev}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

if [[ -f "$HOME/.bw_session" ]]; then
  BW_SESSION="$(tr -d '\n' < "$HOME/.bw_session")"
  export BW_SESSION
fi

if [[ -z "${GEMINI_API_KEYS:-}" && -z "${GEMINI_API_KEY:-}" ]] && command -v vault-get >/dev/null 2>&1; then
  GEMINI_API_KEYS="$(vault-get 'Gemini API Keys' GEMINI_API_KEYS 2>/dev/null || true)"
  export GEMINI_API_KEYS
fi

if [[ "$MODE" == "--production" ]]; then
  cd "$ROOT/frontend"
  npm run build
  cd "$ROOT/backend"
  exec uv run uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8787}"
fi

cleanup() {
  trap - INT TERM EXIT
  kill 0 2>/dev/null || true
}
trap cleanup INT TERM EXIT

(cd "$ROOT/backend" && uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8787}" --reload) &
(cd "$ROOT/frontend" && npm run dev -- --host 127.0.0.1) &
wait
