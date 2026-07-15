#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
MODE="${1:---dev}"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  source "$ROOT/.env"
  set +a
fi

# 커밋하지 않는 호스트별 네트워크/프록시 설정은 기본 비밀 파일을 수정하지
# 않고 .env.local에서 덮어쓸 수 있다.
if [[ -f "$ROOT/.env.local" ]]; then
  set -a
  source "$ROOT/.env.local"
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

# Vault 세션은 Gemini 키를 읽는 순간에만 필요하다. API와 이미지 자식 프로세스에 넘기지 않는다.
unset BW_SESSION

if [[ "$MODE" == "--production" ]]; then
  export TERRA_ENV="${TERRA_ENV:-production}"
  "$ROOT/scripts/build_frontend_atomic.sh"
  cd "$ROOT/backend"
  # 이미지 작업 큐와 MLX 모델 잠금은 프로세스 내부 상태이므로 반드시 단일 worker로 실행한다.
  exec uv run uvicorn app.main:app \
    --host "${TERRA_HOST:-127.0.0.1}" \
    --port "${PORT:-8787}" \
    --workers 1 \
    --no-server-header \
    --forwarded-allow-ips "${TERRA_FORWARDED_ALLOW_IPS:-127.0.0.1}" \
    --limit-concurrency "${TERRA_HTTP_CONCURRENCY:-128}" \
    --backlog "${TERRA_HTTP_BACKLOG:-128}" \
    --timeout-keep-alive 5 \
    --timeout-graceful-shutdown "${TERRA_GRACEFUL_SHUTDOWN_SECONDS:-30}"
fi

cleanup() {
  trap - INT TERM EXIT
  kill 0 2>/dev/null || true
}
trap cleanup INT TERM EXIT

(cd "$ROOT/backend" && uv run uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8787}" --reload) &
(cd "$ROOT/frontend" && npm run dev -- --host 127.0.0.1) &
wait
