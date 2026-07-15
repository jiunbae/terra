#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h:h}"
FRONTEND="$ROOT/frontend"
CURRENT="$FRONTEND/dist"
NEXT="$FRONTEND/dist.next"
PREVIOUS="$FRONTEND/dist.previous"

# 실패한 과거 staging만 지운다. 현재 서비스 중인 dist는 빌드 성공 전까지
# 그대로 두므로 tsc/vite 오류가 발생해도 배포 파일이 손상되지 않는다.
rm -rf "$NEXT"
(
  cd "$FRONTEND"
  TERRA_BUILD_OUT_DIR=dist.next npm run build
)

rm -rf "$PREVIOUS"
if [[ -e "$CURRENT" ]]; then
  mv "$CURRENT" "$PREVIOUS"
fi

if ! mv "$NEXT" "$CURRENT"; then
  if [[ -e "$PREVIOUS" && ! -e "$CURRENT" ]]; then
    mv "$PREVIOUS" "$CURRENT"
  fi
  exit 1
fi

# 직전 산출물은 즉시 롤백할 수 있도록 dist.previous에 한 세대 보존한다.
print "Frontend build activated: $CURRENT"
