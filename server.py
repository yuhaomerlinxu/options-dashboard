import json, threading, time, datetime, os, re, queue
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from mcp_client import MCP
import wsfeed
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
msgs, seen, subs = [], set(), []
CH = {}
def load_channels(chs):
    out = {c["channel_id"]: c["channel_name"].strip("·｜ ") for c in chs if "期权" in (c.get("category_name") or "") and c.get("channel_type") == 0}
    if out: CH.clear(); CH.update(out)
    return len(out)
try: load_channels(json.load(open(os.path.join(BASE, "channels.json")))["channels"])  # bundled snapshot (non-secret ids/names)
except Exception as e: print("bundled channel list error", e)
try: print("channels refreshed via MCP:", load_channels(mcp.call("list_channels", {}).get("channels", [])))
except Exception as e: print("channel refresh failed, using bundled list:", e)
OPT = re.compile(r"\$?\b([A-Z]{1,5}(?:/[A-Z])?)\s+\$?(\d+(?:\.\d+)?)\s*(CALLS?|PUTS?|C|P)\b(?:\s+(?:exp\.?\s*)?(\d{1,2}/\d{1,2}(?:/\d{2,4})?))?(?:[^@\n]{0,40}?@\s*\$?(\d*\.?\d+))?", re.I)
OPT2 = re.compile(r"\$?\b([A-Z]{1,5})\s+(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s+\$?(\d+(?:\.\d+)?)\s*(CALLS?|PUTS?|C|P)\b(?:[^@\n]{0,40}?(?:@|\bat\b)\s*\$?(\d*\.?\d+))?", re.I)
TKR = re.compile(r"\$([A-Z]{1,5})\b")
STOP_TK = {"INTO", "PUT", "CALL", "SET", "BTO", "STC", "DTE", "ITM", "OTM", "ATM", "PT", "AT", "THE", "SL", "TP", "I", "A", "MNQ", "ES", "NQ", "IN", "ON", "TO", "OF", "UP"}
ACTS = [("止损", re.compile(r"\bSTC\b.*\b(?:stop|SL)\b|stop(?:ped)?\s*out|hit (?:my )?stop|\bSL hit|止损", re.I)),
        ("减仓", re.compile(r"✂️|\btrim(?:med|ming)?\b|tak(?:e|ing|en) (?:some |more )?(?:profit|off|\d{1,3}\s*%)|sold (?:half|some|\d)|减仓|部分止盈", re.I)),
        ("卖出", re.compile(r"\bSTC\b|\ball out\b|\bclos(?:e|ed|ing)\b|\bsold\b|\bsell(?:ing)?\b|\bexit(?:ed|ing)?\b|\bout of\b|清仓|卖出|平仓", re.I)),
        ("买入", re.compile(r"\bBTO\b|\bbought\b|I'm Entering|\bLOTTO\b|\bbuy(?:ing)?\b|\bentry\b|\bentered\b|\bopen(?:ed|ing)?\b|\bswing\b|\blotto\b|\bstarter\b|\badd(?:ed|ing)?\b|买入|开仓|进场|加仓", re.I))]
def parse(text):
    t = text or ""
    out = {"ticker": None, "option": None, "contract": None, "action": None}
    m = OPT.search(t); c = None
    if m and m.group(1).upper() not in STOP_TK:
        c = dict(ticker=m.group(1).upper(), strike=m.group(2), cp=m.group(3)[0].upper(), exp=m.group(4), price=m.group(5))
    else:
        m = OPT2.search(t)
        if m and m.group(1).upper() not in STOP_TK:
            c = dict(ticker=m.group(1).upper(), strike=m.group(3), cp=m.group(4)[0].upper(), exp=m.group(2), price=m.group(5))
    if c and not c["price"]:
        pm = re.search(r"(?:Entry|Price|Fill(?:ed)?|Exit)\**:?\**\s*\$?(\d*\.?\d+)", t, re.I)
        if pm: c["price"] = pm.group(1)
    if c:
        out.update(ticker=c["ticker"], contract=c, option=f'{c["ticker"]} {c["strike"]}{c["cp"]}' + (f' {c["exp"]}' if c["exp"] else "") + (f' @ {c["price"]}' if c["price"] else ""))
    else:
        tk = TKR.search(t)
        if tk: out["ticker"] = tk.group(1)
    for name, rx in ACTS:
        if rx.search(t):
            if name == "买入" and not c: continue
            out["action"] = name; break
    return out
def trader_of(ch):
    ch = (ch or "").strip("-·｜ ")
    i = ch.find("-期权")
    if i < 0: return ch, ""
    return ch[:i].strip("- "), re.sub(r"-p\d$", "", ch[i + 1:])
def cjk_ratio(s):
    s = re.sub(r"\s", "", s or "");
    return sum(1 for ch in s if "\u4e00" <= ch <= "\u9fff") / max(1, len(s))
def numsig(s):
    return tuple(sorted(re.findall(r"\d+(?:\.\d+)?", re.sub(r"\d+\s*%", "", s or ""))))
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
    item["trader"], item["tag"] = trader_of(item["channel"])
    cr = cjk_ratio(text); zh = cr > 0.15; sig = numsig(text)
    with lock:
        if item["id"] in seen: return False
        pair = [x for x in msgs if x["channel_id"] == item["channel_id"] and abs(x["ts"] - ts) <= 300 and x.get("_zh") != zh and (x.get("_sig") == sig or
                (abs(x["ts"] - ts) <= 60 and min(x.get("_cr", 0), cr) < 0.05 and 0.25 < len(x["text"]) / max(1, len(text)) < 4))]
        if pair:
            seen.add(item["id"])
            if zh: return False  # Chinese translation of an English original already shown
            for x in pair: msgs.remove(x)  # English original arrived after its translation: replace
        item["_zh"], item["_sig"], item["_cr"] = zh, sig, cr
        seen.add(item["id"]); msgs.append(item); msgs.sort(key=lambda x: x["ts"], reverse=True); del msgs[300:]
        if live and ts > START - 300:
            for q in list(subs): q.put(item)
    return True
def ws_push(m):
    if m.get("channel_id") in CH: push(m, True)
ws = wsfeed.Feed(ws_push)
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
        if p == "/api/status": return s.j({**status, "now_bj": now_bj(), "session": bool(mcp.sid), "options_channels": len(CH), "messages_cached": len(msgs), "chain_entitlement": prov.last_error, **ws.state})
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
    for f in (feed_loop, market_loop): threading.Thread(target=f, daemon=True).start()
    ws.start()
    qqqapp.start()
    ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8080"))), H).serve_forever()
