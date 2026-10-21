# Deploy to Render (Docker web service)

Blueprint: `render.yaml` (runtime docker, free plan, health check `/healthz`). Render injects `PORT`; the server binds `0.0.0.0:$PORT`.

## Env vars to set in Render (values never committed)
| Name | What |
|---|---|
| `DASH_TOKEN` | Dashboard access token (>=16 chars). Open `https://<app>.onrender.com/?key=<DASH_TOKEN>` once; sets an HttpOnly Secure cookie. Basic auth (any user / token as password) also works. |
| `USSTOCK_MCP_URL` | VIP MCP endpoint URL |
| `USSTOCK_MCP_KEY` | VIP MCP key |
| `LONGBRIDGE_CLI_AUTH_B64` | `base64 -w0 ~/.longbridge/openapi/cli-auth` |
| `LONGBRIDGE_CLI_REGISTRATION_B64` | `base64 -w0 ~/.longbridge/openapi/cli-registration` |
| `LONGBRIDGE_DEVICE_ID` | contents of `~/.longbridge/openapi/device-id` (optional but recommended) |

Optional: `CHAIN_PROVIDER` (default `longbridge`), `DATA_PROVIDER`.

## Longbridge credentials
Longbridge CLI v0.30 uses OAuth (`longbridge auth login`), not APP_KEY/SECRET env vars. `entrypoint.sh` decodes the env vars into `~/.longbridge/openapi/` at container start, then unsets them.
The CLI may refresh/rotate the token at runtime; Render's filesystem is ephemeral, so if quotes start failing after a restart, re-run `longbridge auth login` locally and update the two `_B64` vars.

Market data only: `providers.lb()` whitelists `quote`, `option chain`, `option quote`; any other subcommand raises PermissionError. No order/trade commands are ever called.

Free plan sleeps after inactivity; first request after idle takes ~1 min.
