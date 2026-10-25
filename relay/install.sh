#!/usr/bin/env bash
# Install lllm-relay for the current user on the machine that runs the LLM.
set -euo pipefail

SRC="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/lllm_relay.py"
DEST="$HOME/.local/bin/lllm-relay"

command -v python3 >/dev/null || { echo "python3 is required" >&2; exit 1; }

mkdir -p "$(dirname "$DEST")"
install -m 755 "$SRC" "$DEST"
echo "installed $DEST"

"$DEST" init-config || true   # keeps an existing config
echo
"$DEST" doctor || echo "(fix the backend settings in ~/.config/lllm/config.json, then run: $DEST doctor)"

cat <<EOF

Next:
  * try it here:      $DEST chat
  * in the app, set the relay command to:  $DEST serve
  * optional, recommended: give the phone its own key and lock it to the relay by
    adding this line to ~/.ssh/authorized_keys (paste the phone's public key at the end):

      command="$DEST serve",restrict ssh-ed25519 AAAA...phone-key... phone
EOF
