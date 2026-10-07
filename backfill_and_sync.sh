#!/bin/bash
set -u

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 daily|dividends [backfill options]" >&2
  exit 2
fi

kind="$1"
shift
case "$kind" in
  daily) script="backfill_market_data.py" ;;
  dividends) script="backfill_dividends.py" ;;
  *) echo "First argument must be 'daily' or 'dividends'" >&2; exit 2 ;;
esac

if [[ ! -x .venv/bin/python ]]; then
  echo "Project virtualenv .venv/bin/python is missing" >&2
  exit 2
fi

set +e
./.venv/bin/python "$script" "$@"
backfill_status=$?
./sync_ww_cache.sh
sync_status=$?
set -e

if [[ $backfill_status -ne 0 ]]; then
  echo "Backfill exited with status ${backfill_status}; available checkpoints were synced." >&2
  exit "$backfill_status"
fi
exit "$sync_status"
