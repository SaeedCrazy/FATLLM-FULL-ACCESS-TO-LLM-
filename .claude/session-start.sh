#!/usr/bin/env bash
# SessionStart hook: put srv on PATH and tell Claude whether the server key works,
# so a session on this repo is connected to the owner's server from the first message.
DIR="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "$0")/.." && pwd)}"
chmod +x "$DIR/srv" 2>/dev/null || true
ln -sf "$DIR/srv" /usr/local/bin/srv 2>/dev/null || true

if [ -z "${FATLLM_KEY:-}" ] && [ ! -s "$HOME/.fatllm-key" ]; then
  echo "FATLLM: no server key yet. When the user pastes one (a line with https://.../fat_...), run: ./srv key '<that line>'"
  exit 0
fi
if out="$("$DIR/srv" info 2>&1)"; then
  echo "FATLLM: connected to the owner's server. Run commands with ./srv (see CLAUDE.md). Server facts:"
  printf '%s\n' "$out" | head -20
else
  echo "FATLLM: a key is set but the server did not answer:"
  printf '%s\n' "$out" | tail -3
  echo "A proxy 403 means the cloud environment's network policy blocks the host (tell the user to set Network access to Full)."
  echo "Otherwise ask the user to run 'fatllm status' on the server."
fi
exit 0
