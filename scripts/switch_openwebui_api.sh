#!/usr/bin/env sh
set -eu

OPENWEBUI_ENV="${OPENWEBUI_ENV:-/root/docker/open-webui/.env}"
API_URL="${1:-http://host.docker.internal:18082/v1}"
DEFAULT_MODEL="${2:-grok-4.20-beta}"

if [ ! -f "$OPENWEBUI_ENV" ]; then
  echo "Open WebUI env file not found: $OPENWEBUI_ENV" >&2
  exit 1
fi

BACKUP_PATH="${OPENWEBUI_ENV}.bak.$(date +%Y%m%d%H%M%S)"
cp "$OPENWEBUI_ENV" "$BACKUP_PATH"

tmp_file="$(mktemp)"
trap 'rm -f "$tmp_file"' EXIT

awk -v api_url="$API_URL" -v default_model="$DEFAULT_MODEL" '
  BEGIN {
    api_written = 0
    model_written = 0
  }
  /^OPENAI_API_BASE_URL=/ {
    print "OPENAI_API_BASE_URL=" api_url
    api_written = 1
    next
  }
  /^DEFAULT_MODELS=/ {
    print "DEFAULT_MODELS=" default_model
    model_written = 1
    next
  }
  { print }
  END {
    if (!api_written) {
      print "OPENAI_API_BASE_URL=" api_url
    }
    if (!model_written) {
      print "DEFAULT_MODELS=" default_model
    }
  }
' "$OPENWEBUI_ENV" > "$tmp_file"

mv "$tmp_file" "$OPENWEBUI_ENV"

echo "Updated: $OPENWEBUI_ENV"
echo "Backup:  $BACKUP_PATH"
echo "API URL: $API_URL"
echo "Model:   $DEFAULT_MODEL"
echo
echo "Next:"
echo "  cd /root/docker/open-webui && docker compose up -d"
