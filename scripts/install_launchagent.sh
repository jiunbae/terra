#!/bin/zsh
set -euo pipefail

if [[ "$EUID" == "0" ]]; then
  print -u2 "Install Terra as the user that will run the MLX process, not as root."
  exit 1
fi

ROOT="${0:A:h:h}"
TEMPLATE="$ROOT/deploy/com.jiun.terra.plist.template"
AGENTS_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="${TERRA_LOG_DIR:-$HOME/Library/Logs/Terra}"
TARGET="$AGENTS_DIR/com.jiun.terra.plist"
LAUNCHD_PATH="${TERRA_LAUNCHD_PATH:-/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin}"

mkdir -p "$AGENTS_DIR" "$LOG_DIR"
chmod 700 "$AGENTS_DIR" "$LOG_DIR"

temporary="$(mktemp "$TARGET.XXXXXX")"
cleanup() {
  rm -f "$temporary"
}
trap cleanup EXIT

cp "$TEMPLATE" "$temporary"
/usr/libexec/PlistBuddy -c "Set :ProgramArguments:1 $ROOT/start.sh" "$temporary"
/usr/libexec/PlistBuddy -c "Set :WorkingDirectory $ROOT" "$temporary"
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:HOME $HOME" "$temporary"
/usr/libexec/PlistBuddy -c "Set :EnvironmentVariables:PATH $LAUNCHD_PATH" "$temporary"
/usr/libexec/PlistBuddy -c "Set :StandardOutPath $LOG_DIR/terra.stdout.log" "$temporary"
/usr/libexec/PlistBuddy -c "Set :StandardErrorPath $LOG_DIR/terra.stderr.log" "$temporary"

if rg -q '__[A-Z_]+__' "$temporary"; then
  print -u2 "LaunchAgent template still contains unresolved placeholders."
  exit 1
fi
/usr/bin/plutil -lint "$temporary"
chmod 600 "$temporary"
mv "$temporary" "$TARGET"
trap - EXIT

print "Installed validated LaunchAgent configuration: $TARGET"
print "Build and test the release before bootstrapping it: make test"
print "Then run: launchctl bootstrap gui/$(id -u) $TARGET"
