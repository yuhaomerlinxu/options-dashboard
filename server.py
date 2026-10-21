import json, threading, time, datetime, os, re, queue
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from mcp_client import MCP
from providers import McpProvider, LongbridgeProvider
import sys; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "qqq")); import qqqapp
BASE = os.path.dirname(os.path.abspath(__file__))
BJ = datetime.timezone(datetime.timedelta(hours=8))
now_bj = lambda: datetime.datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S")
TICKERS = ["META", "TSLA", "LRCX", "TXN", "IBM"]
CHAIN_TTL, QUOTE_TTL, FEED_POLL, WR_TTL = 60, 30, 5, 600
mcp = MCP(); prov = LongbridgeProvider() if os.environ.get("CHAIN_PROVIDER", "longbridge") == "longbridge" else McpProvider(mcp)
lock = threading.Lock()
chains, quotes, status = {}, {}, {"mcp": "connecting", "last_update": None, "errors": {}, "backoff": 0}
winrate = {"data": None, "ts": None}
msgs, seen, subs = [], set(), []
CH = {}
try:
    for c in json.load(open("/workspace/agent-tools/f00c5a0e-557f-4b28-93d8-2371cac9df10.txt"))["channels"]:
        if "期权" in (c.get("category_name") or "") and c.get("channel_type") == 0: CH[c["channel_id"]] = c["channel_name"].strip("·｜ ")
except Exception as e: print("channel list error", e)
OPT = re.compile(r"\$?\b([A-Z]{1,5}(?:/[A-Z])?)\s+\$?(\d+(?:\.\d+)?)\s*(CALLS?|PUTS?|C|P)\b(?:\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?))?(?:\s*@\s*\$?(\d+(?:\.\d+)?))?", re.I)
TKR = re.compile(r"\$([A-Z]{1,5})\b")
def parse(text):
    m = OPT.search(text or "")
    if m and m.group(1).upper() not in ("PT", "AT", "THE", "SL", "TP"):
        return {"ticker": m.group(1).upper(), "option": " ".join(x for x in [m.group(1).upper(), m.group(2), m.group(3).upper(), m.group(4) or "", ("@" + m.group(5)) if m.group(5) else ""] if x)}
    t = TKR.search(text or "")
    return {"ticker": t.group(1), "option": None} if t else {"ticker": None, "option": None}
def ok(key):
    status["errors"].pop(key, None); status["mcp"] = "connected"; status["last_update"] = now_bj(); status["backoff"] = 0
def err(key, e):
    status["errors"][key] = f"{now_bj()} {e}"[:600]
    if "403" in str(e) or "URLError" in type(e).__name__ or "timed out" in str(e): status["mcp"] = "error"
    if "429" in str(e) or "rate" in str(e).lower(): status["backoff"] = min(120, (status["backoff"] or 5) * 2)
START = time.time()
def push(m, live=False):
    text = m.get("text") or m.get("content") or ""
    if m.get("card_text"): text = (text + "\n" + m["card_text"]).strip()
    ts = int(float(m.get("message_timestamp") or 0))
    item = dict(id=m["message_id"], channel_id=m["channel_id"], channel=CH.get(m["channel_id"], m.get("channel_name")),
                author=m.get("author_name"), ts=ts, time_bj=datetime.datetime.fromtimestamp(ts, BJ).strftime("%m-%d %H:%M:%S"),
                text=text[:2000], url=m.get("jump_url"), **parse(text))
    with lock:
        if item["id"] in seen: return False
        seen.add(item["id"]); msgs.append(item); msgs.sort(key=lambda x: x["ts"], reverse=True); del msgs[300:]
        if live and ts > START - 300:
            for q in list(subs): q.put(item)
    return True
def ingest(r, live):
    n = 0
    for m in (r or {}).get("messages", []):
        if m.get("channel_id") in CH:
            if push(m, live): n += 1
    return n
def feed_loop():
    for cid in list(CH):  # backfill latest 3 per options channel
        try: ingest(mcp.call("get_latest_messages", {"channel_id": cid, "limit": 3}), False); ok("feed")
        except Exception as e: err("feed", e)
    ids = list(CH); i = 0
    while True:
        try:
            ingest(mcp.call("get_latest_messages", {"limit": 100}), True)  # global firehose, filtered to options channels
            for cid in ids[i:i + 2]: ingest(mcp.call("get_latest_messages", {"channel_id": cid, "limit": 5}), True)  # round-robin safety net
            i = (i + 2) % len(ids); ok("feed")
        except Exception as e: err("feed", e)
        time.sleep(FEED_POLL + status["backoff"])
def market_loop():
    last_chain = {}
    while True:
        for t in list(TICKERS):
            try:
                q = prov.quote(t); q["fetched_bj"] = now_bj()
                with lock: quotes[t] = q
                ok("quote")
            except Exception as e: err("quote:" + t, e)
            if time.time() - last_chain.get(t, 0) > CHAIN_TTL:
                try:
                    d = prov.chain(t); d["fetched_bj"] = now_bj(); last_chain[t] = time.time()
                    with lock: chains[t] = d
                except Exception as e: err("chain:" + t, e)
        time.sleep(QUOTE_TTL + status["backoff"])
def wr_loop():
    while True:
        try:
            r = mcp.call("win_rate_get_summary", {"asset_type": "option", "range": "30d"}, timeout=90)
            s = r.get("summary", {}); rows = []
            for b in s.get("blogger_rankings", []):
                if b.get("channel_id") in CH or "期权" in (b.get("channel_name") or ""):
                    rows.append({k: b.get(k) for k in ("channel_id", "channel_name", "total_signals", "settled_signals", "win_count", "loss_count",
                        "open_count", "win_rate", "average_return", "profit_factor", "current_streak", "current_streak_outcome", "value_score", "rating_tags")})
            winrate.update(data={"overall": {k: s.get(k) for k in ("total_signals", "settled_signals", "win_count", "loss_count", "win_rate", "average_return")}, "rows": rows}, ts=now_bj())
            ok("winrate")
        except Exception as e: err("winrate", e)
        time.sleep(WR_TTL)
import secrets, base64, hmac
from urllib.parse import urlsplit, parse_qs, urlencode
TOKEN = os.environ.get("DASH_TOKEN", "").strip()
assert TOKEN and len(TOKEN) >= 16
class H(qqqapp.Mixin, SimpleHTTPRequestHandler):
    def auth(s):
        u = urlsplit(s.path); qs = parse_qs(u.query)
        k = (qs.get("key") or [""])[0]
        if k and hmac.compare_digest(k, TOKEN):
            rest = {a: b for a, b in qs.items() if a != "key"}
            s.send_response(302); s.send_header("Location", u.path + ("?" + urlencode(rest, doseq=True) if rest else ""))
            s.send_header("Set-Cookie", f"dash_key={TOKEN}; HttpOnly; Secure; SameSite=Lax; Path=/; Max-Age=31536000"); s.end_headers(); return False
        for c in (s.headers.get("Cookie") or "").split(";"):
            a, _, b = c.strip().partition("=")
            if a == "dash_key" and hmac.compare_digest(b, TOKEN): return True
        h = s.headers.get("Authorization") or ""
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode().split(":", 1)[-1]
                if hmac.compare_digest(pw, TOKEN): return True
            except Exception: pass
        b = b"401 Unauthorized"; s.send_response(401); s.send_header("WWW-Authenticate", 'Basic realm="dashboard"')
        s.send_header("Content-Length", len(b)); s.end_headers(); s.wfile.write(b); return False
    def __init__(s, *a, **k): super().__init__(*a, directory=os.path.join(BASE, "static"), **k)
    def j(s, o, c=200):
        b = json.dumps(o, ensure_ascii=False).encode(); s.send_response(c); s.send_header("Content-Type", "application/json; charset=utf-8")
        s.send_header("Content-Length", len(b)); s.send_header("Cache-Control", "no-store"); s.end_headers(); s.wfile.write(b)
    def do_HEAD(s):
        if s.auth(): super().do_HEAD()
    def do_GET(s):
        if s.path.split("?")[0] == "/healthz":
            b = b"ok"; s.send_response(200); s.send_header("Content-Length", len(b)); s.end_headers(); s.wfile.write(b); return
        if not s.auth(): return
        p = s.path.split("?")[0]
        if p in ("/levels", "/radar"):
            s.send_response(302); s.send_header("Location", "/qqq/chart?s=QQQ" if p == "/levels" else "/qqq/radar"); s.end_headers(); return
        if p == "/qqq" or p.startswith("/qqq/"): return s.qqq_get()
        if p == "/api/tickers": return s.j(TICKERS)
        if p.startswith("/api/add/"):
            t = re.sub(r"[^A-Z.]", "", p.rsplit("/", 1)[1].upper())[:6]
            if t and t not in TICKERS and len(TICKERS) < 15: TICKERS.append(t)
            return s.j(TICKERS)
        if p.startswith("/api/remove/"):
            t = p.rsplit("/", 1)[1].upper()
            if t in TICKERS and len(TICKERS) > 1: TICKERS.remove(t)
            return s.j(TICKERS)
        if p.startswith("/api/chain/"):
            t = p.rsplit("/", 1)[1].upper()
            with lock: d = chains.get(t)
            return s.j(d) if d else s.j({"error": "loading"}, 503)
        if p == "/api/quotes":
            with lock: return s.j(quotes)
        if p == "/api/messages":
            with lock: return s.j(msgs[:200])
        if p == "/api/channels": return s.j(CH)
        if p == "/api/winrate": return s.j(winrate)
        if p == "/api/status": return s.j({**status, "now_bj": now_bj(), "session": bool(mcp.sid), "options_channels": len(CH), "messages_cached": len(msgs), "chain_entitlement": prov.last_error})
        if p == "/api/stream":
            s.send_response(200); s.send_header("Content-Type", "text/event-stream"); s.send_header("Cache-Control", "no-cache, no-transform"); s.send_header("X-Accel-Buffering", "no"); s.end_headers(); s.wfile.write(b": open\n\n"); s.wfile.flush()
            q = queue.Queue(); subs.append(q)
            try:
                while True:
                    try: item = q.get(timeout=10); s.wfile.write(f"event: msg\ndata: {json.dumps(item, ensure_ascii=False)}\n\n".encode())
                    except queue.Empty: s.wfile.write(f"event: ping\ndata: {json.dumps({'status': status['mcp'], 'last_update': status['last_update']})}\n\n".encode())
                    s.wfile.flush()
            except Exception: pass
            finally: subs.remove(q)
            return
        return super().do_GET()
    def log_message(s, *a): pass
if __name__ == "__main__":
    for f in (feed_loop, market_loop, wr_loop): threading.Thread(target=f, daemon=True).start()
    qqqapp.start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), H).serve_forever()
