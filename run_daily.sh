#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

LOCK_DIR="$ROOT_DIR/data/.auto_run_lock"
STAMP_FILE="$ROOT_DIR/data/.last_auto_run_date"
TODAY="$(date +%F)"

mkdir -p "$ROOT_DIR/data"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  # Another run is in progress. Skip to avoid concurrent heavy jobs.
  exit 0
fi
trap 'rmdir "$LOCK_DIR" >/dev/null 2>&1 || true' EXIT

if [[ -f "$STAMP_FILE" ]]; then
  LAST_RUN_DATE="$(cat "$STAMP_FILE" 2>/dev/null || true)"
  if [[ "$LAST_RUN_DATE" == "$TODAY" ]]; then
    exit 0
  fi
fi

if python3 paper_agent.py update --root "$ROOT_DIR" --config config.json --notify; then
  mkdir -p "$(dirname "$STAMP_FILE")"
  printf '%s\n' "$TODAY" > "$STAMP_FILE"

  # Auto commit and push to GitHub
  cd "$ROOT_DIR"
  git add -A data/ reports/
  if ! git diff --cached --quiet; then
    PAPER_COUNT=$(git diff --cached --stat | grep -c "notes/" || echo "0")
    git commit -m "📄 Daily paper update ${TODAY} (+${PAPER_COUNT} papers)"
    git push origin main 2>&1 || echo "[WARN] git push failed"
  fi
fi
