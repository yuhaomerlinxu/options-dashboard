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

| `NTFY_TOPIC` | optional: ntfy.sh topic for QQQ 1/2/3m signal pushes |
| `QQQ_WEBHOOK_URL` / `QQQ_WEBHOOK_KEY` | optional: webhook POST for QQQ signals |

Optional: `CHAIN_PROVIDER` (default `longbridge`), `DATA_PROVIDER`.

## Longbridge credentials
Longbridge CLI v0.30 uses OAuth (`longbridge auth login`), not APP_KEY/SECRET env vars. `entrypoint.sh` decodes the env vars into `~/.longbridge/openapi/` at container start, then unsets them.
The CLI may refresh/rotate the token at runtime; Render's filesystem is ephemeral, so if quotes start failing after a restart, re-run `longbridge auth login` locally and update the two `_B64` vars.

Market data only: `providers.lb()` whitelists `quote`, `option chain`, `option quote`; the QQQ pages call the CLI only through `/app/lbsafe`, which allows `quote`, `kline` (incl. `kline history`), `option chain`, `option quote`, `finance-calendar report`, `trading days` and exits 13 for anything else. No order/trade/alert commands are ever called (the original monitor's `longbridge alert add` phone push is disabled; use `NTFY_TOPIC`).

## Routes (all behind DASH_TOKEN except /healthz)
- `/` options dashboard
- `/qqq` QQQ real-time quote + 1m chart (EMA/VWAP) + 1/2/3m signal alerts; APIs `/qqq/api`, `/qqq/kline`
- `/qqq/chart?s=QQQ&p=5m` (alias `/levels`) multi-symbol K-line with key levels (call/put wall, max pain, gamma flip, IV ±1/2σ) and spread signals; APIs `/qqq/chartdata|candles|last|levels|signals|spreads?s=`
- `/qqq/radar` (alias `/radar`) options radar (scheduled 09:35 ET then every 30 min, 08:00 ET premarket; `?run=1` manual); API `/qqq/radar/api`

Background threads in the container: radar scheduler, chart/spread prewarm, and `qqq/monitor.py` (supervised subprocess, writes `qqq/alerts.jsonl`). Radar output lives in `qqq/radar_data/` (ephemeral on Render).

Free plan sleeps after inactivity; first request after idle takes ~1 min.
