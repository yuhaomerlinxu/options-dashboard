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
# CLI token is encrypted with a key derived from machine-id; reuse the machine-id it was created on
if [ -n "${LONGBRIDGE_MACHINE_ID:-}" ]; then for f in /etc/machine-id /var/lib/dbus/machine-id; do [ -w "$f" ] && printf '%s\n' "$LONGBRIDGE_MACHINE_ID" > "$f" || true; done; fi
unset LONGBRIDGE_MACHINE_ID LONGBRIDGE_CLI_AUTH_B64 LONGBRIDGE_CLI_REGISTRATION_B64
export LONGBRIDGE_NO_ANALYTICS=1
echo "lb selftest: HOME=$HOME user=$(id -u)"; rc=0; /app/lbsafe quote QQQ.US --format json >/tmp/lbt.out 2>/tmp/lbt.err || rc=$?; echo "lb selftest rc=$rc out=$(head -c 200 /tmp/lbt.out | tr "\n" " ") err=$(head -c 600 /tmp/lbt.err | tr "\n" " ")"
exec python3 -u server.py
