#!/bin/bash
set -euo pipefail

REMOTE="${WW_CACHE_SSH_TARGET:-ww-tencent-cache}"
REMOTE_ROOT="${WW_CACHE_REMOTE_ROOT:-/home/ubuntu/ww-cache}"
SSH_KEY="${WW_CACHE_SSH_KEY:-$HOME/.ssh/id_ed25519_ww_tencent}"

if ! command -v rsync >/dev/null 2>&1; then
  echo "rsync is required but was not found" >&2
  exit 127
fi
if [[ ! -d proxy_cache ]]; then
  echo "Run this script from the WW strategy project directory" >&2
  exit 2
fi

SSH_OPTS="ssh -i ${SSH_KEY} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=yes -o ConnectTimeout=10"
ssh -i "$SSH_KEY" -o IdentitiesOnly=yes -o BatchMode=yes \
  -o StrictHostKeyChecking=yes -o ConnectTimeout=10 "$REMOTE" \
  "mkdir -p '${REMOTE_ROOT}/proxy_cache' '${REMOTE_ROOT}/mainline_pit'"

rsync -az --partial -e "$SSH_OPTS" proxy_cache/ \
  "${REMOTE}:${REMOTE_ROOT}/proxy_cache/"

if [[ -d data/mainline_pit ]]; then
  rsync -az --partial -e "$SSH_OPTS" data/mainline_pit/ \
    "${REMOTE}:${REMOTE_ROOT}/mainline_pit/"
fi

echo "WW data cache sync completed: ${REMOTE}:${REMOTE_ROOT}"
