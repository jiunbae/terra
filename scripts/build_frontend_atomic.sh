#!/bin/zsh
set -euo pipefail

# 프론트엔드를 새 릴리스 디렉터리에 빌드한 뒤, dist 심링크를 그 릴리스로 원자적으로
# 재지정한다. 디렉터리는 원자적으로 교체할 수 없어(두 번의 mv 사이에 dist가 사라지는
# 창이 생긴다) 심링크를 쓴다. 빌드가 실패하면 현재 심링크는 그대로라 서비스 중인
# dist가 손상되지 않는다.

ROOT="${0:A:h:h}"
FRONTEND="$ROOT/frontend"
LINK="$FRONTEND/dist"                 # 서비스 경로 — 활성 릴리스를 가리키는 심링크
RELEASES="$FRONTEND/dist.releases"
KEEP=3                                # 롤백용으로 최근 N개 릴리스를 보존

mkdir -p "$RELEASES"

STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
REL_NAME="dist.releases/$STAMP"       # frontend 기준 상대 경로 — 심링크 타깃으로 사용
TARGET="$FRONTEND/$REL_NAME"

rm -rf "$TARGET"
(
  cd "$FRONTEND"
  TERRA_BUILD_OUT_DIR="$REL_NAME" npm run build
)
if [[ ! -f "$TARGET/index.html" ]]; then
  print -u2 "build did not produce $TARGET/index.html"
  rm -rf "$TARGET"
  exit 1
fi

# 원자적 활성화: 새 릴리스를 가리키는 임시 심링크를 만든 뒤 rename(2)으로 dist를
# 덮어쓴다. rename은 원자적이고 대상 심링크를 역참조하지 않으므로 dist가 사라지는
# 창이 없다. (macOS에는 `mv -T`가 없어 python os.replace로 rename을 직접 호출한다.)
if [[ -d "$LINK" && ! -L "$LINK" ]]; then
  # 과거 스킴이 남긴 실제 dist 디렉터리에서의 일회성 전환(디렉터리는 심링크로 rename 불가).
  rm -rf "$LINK"
fi
TMPLINK="$FRONTEND/.dist.$STAMP.lnk"
python3 - "$REL_NAME" "$TMPLINK" "$LINK" <<'PY'
import os, sys
rel, tmp, link = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    os.remove(tmp)
except FileNotFoundError:
    pass
os.symlink(rel, tmp)
os.replace(tmp, link)  # rename(2): 원자적, 대상 심링크를 따라가지 않고 교체
PY

# 최근 KEEP개 릴리스만 남긴다(방금 활성화한 릴리스는 최신이라 항상 보존된다).
# 롤백은 dist 심링크를 원하는 dist.releases/<타임스탬프>로 다시 걸면 된다.
# (@) 플래그가 없으면 zsh가 배열 슬라이스를 한 단어로 합쳐 rm이 아무것도 못 지운다.
releases=("$RELEASES"/*(/Nom))
if (( ${#releases} > KEEP )); then
  rm -rf "${(@)releases[KEEP+1,-1]}"
fi

print "Frontend build activated: dist -> $REL_NAME"
