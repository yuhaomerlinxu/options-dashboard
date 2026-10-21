#!/bin/sh
# Writes Longbridge CLI OAuth state from env vars (base64) at container start. No secrets in the image.
set -eu
: "${DASH_TOKEN:?DASH_TOKEN required}"; : "${USSTOCK_MCP_URL:?}"; : "${USSTOCK_MCP_KEY:?}"
: "${LONGBRIDGE_CLI_AUTH_B64:?}"; : "${LONGBRIDGE_CLI_REGISTRATION_B64:?}"
D="$HOME/.longbridge/openapi"; mkdir -p "$D"; chmod 700 "$HOME/.longbridge" "$D"
umask 077
printf '%s' "$LONGBRIDGE_CLI_AUTH_B64" | base64 -d > "$D/cli-auth"
printf '%s' "$LONGBRIDGE_CLI_REGISTRATION_B64" | base64 -d > "$D/cli-registration"
[ -n "${LONGBRIDGE_DEVICE_ID:-}" ] && printf '%s' "$LONGBRIDGE_DEVICE_ID" > "$D/device-id"
unset LONGBRIDGE_CLI_AUTH_B64 LONGBRIDGE_CLI_REGISTRATION_B64
export LONGBRIDGE_NO_ANALYTICS=1
exec python3 -u server.py
