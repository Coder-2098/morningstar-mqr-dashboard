#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
DOMICILE="${1:-France}"
shift || true

echo "Continuing Morningstar MQR pipeline for domicile: $DOMICILE"
echo "Paste a fresh Analytics Lab token. It will be used only for this terminal session."
read -s -p "MD_AUTH_TOKEN: " TOKEN
echo
TOKEN="$(echo "$TOKEN" | xargs)"

if [ -z "$TOKEN" ]; then
  echo "ERROR: Token is empty."
  exit 1
fi

DOT_COUNT=$(echo "$TOKEN" | awk -F'.' '{print NF}')
if [ "$DOT_COUNT" -ne 3 ]; then
  echo "ERROR: Token does not look like header.payload.signature."
  exit 1
fi

export MD_AUTH_TOKEN="$TOKEN"

python3 run_mqr.py \
  --domicile "$DOMICILE" \
  --no-token-prompt \
  "$@"
