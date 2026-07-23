#!/usr/bin/env bash
set -euo pipefail

env_path="${1:-.env.local}"
if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required to generate local development secrets." >&2
  exit 1
fi

umask 077
if [[ ! -e "$env_path" ]]; then
  cp .env.local.example "$env_path"
fi
chmod 600 "$env_path"

ensure_value() {
  local key="$1"
  local value="$2"
  local existing
  existing=$(awk -F= -v key="$key" '$1 == key && length(substr($0, length(key) + 2)) > 0 { print; exit }' "$env_path")
  if [[ -n "$existing" ]]; then
    value="${existing#*=}"
  fi

  local temp_path
  temp_path=$(mktemp "${env_path}.tmp.XXXXXX")
  awk -v key="$key" -v value="$value" '
    BEGIN { written = 0 }
    $0 ~ ("^" key "=") {
      if (!written) { print key "=" value; written = 1 }
      next
    }
    { print }
    END { if (!written) print key "=" value }
  ' "$env_path" >"$temp_path"
  mv "$temp_path" "$env_path"
}

ensure_value POSTGRES_PASSWORD "$(openssl rand -hex 24)"
ensure_value MINIO_ROOT_USER media-engine
ensure_value MINIO_ROOT_PASSWORD "$(openssl rand -hex 32)"
ensure_value MEDIA_ENGINE_WORKER_API_TOKEN "$(openssl rand -hex 40)"
ensure_value MEDIA_ENGINE_ADMIN_USERNAME admin
ensure_value MEDIA_ENGINE_ADMIN_PASSWORD "$(openssl rand -base64 36 | tr -d '\n')"
ensure_value MEDIA_ENGINE_ADMIN_SESSION_SECRET "$(openssl rand -base64 48 | tr -d '\n')"
ensure_value MEDIA_ENGINE_CREDENTIAL_ENCRYPTION_KEY "$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '\n')"

echo "Ensured required local credentials in $env_path. Existing values were preserved."
