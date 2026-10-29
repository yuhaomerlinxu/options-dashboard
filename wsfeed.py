"""Real-time push from the US Stock VIP WebSocket gateway (stdlib-only RFC6455 client)."""
import os, ssl, socket, base64, json, struct, time, datetime, random, threading
from urllib.parse import urlsplit

def _env(k):
    v = os.environ.get(k, "").strip()
    if v: return v
    try:
        for l in open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
            if l.startswith(k + "="): return l.split("=", 1)[1].strip()
    except OSError: pass
    return ""

class WS:
    def __init__(s, url, token):
        u = urlsplit(url); s.host = u.hostname; port = u.port or (443 if u.scheme == "wss" else 80)
        raw = socket.create_connection((s.host, port), timeout=20)
        s.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=s.host) if u.scheme == "wss" else raw
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + ("?" + u.query if u.query else "")
        s.sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {s.host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nAuthorization: Bearer {token}\r\n\r\n").encode())
        hdr = b""
        while b"\r\n\r\n" not in hdr:
            c = s.sock.recv(1)
            if not c: raise ConnectionError("handshake closed")
            hdr += c
        line = hdr.split(b"\r\n", 1)[0].decode(errors="replace")
        if " 101 " not in line + " ": raise ConnectionError("handshake failed: " + line[:80])
        s.sock.settimeout(60); s.lock = threading.Lock()
    def _n(s, n):
        b = b""
        while len(b) < n:
            c = s.sock.recv(n - len(b))
            if not c: raise ConnectionError("socket closed")
            b += c
        return b
    def send(s, data, op=1):
        p = data.encode() if isinstance(data, str) else data; m = os.urandom(4); n = len(p)
        h = bytes([0x80 | op]) + (bytes([0x80 | n]) if n < 126 else bytes([0xFE]) + struct.pack(">H", n) if n < 65536 else bytes([0xFF]) + struct.pack(">Q", n))
        with s.lock: s.sock.sendall(h + m + bytes(c ^ m[i % 4] for i, c in enumerate(p)))
    def recv(s):
        buf = b""
        while True:
            b1, b2 = s._n(2)
            op, n = b1 & 15, b2 & 127
            if n == 126: n = struct.unpack(">H", s._n(2))[0]
            elif n == 127: n = struct.unpack(">Q", s._n(8))[0]
            mk = s._n(4) if b2 & 128 else None
            p = s._n(n)
            if mk: p = bytes(c ^ mk[i % 4] for i, c in enumerate(p))
            if op == 9: s.send(p, 10); continue
            if op == 10: continue
            if op == 8: raise ConnectionError("server closed")
            buf += p
            if b1 & 0x80: return buf.decode(errors="replace")
    def close(s):
        try: s.send(b"", 8); s.sock.close()
        except Exception: pass

def _ts(v):
    if v in (None, ""): return time.time()
    try:
        f = float(v); return f / 1000 if f > 1e11 else f
    except (TypeError, ValueError): pass
    try: return datetime.datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
    except ValueError: return time.time()

def normalize(ev):
    """Map a discord.message.created event onto the MCP get_latest_messages message shape used by push()."""
    m = ev.get("message") or ev.get("data") or ev.get("payload") or ev
    if isinstance(m, dict) and isinstance(m.get("message"), dict): m = {**m, **m["message"]}
    a = m.get("author") if isinstance(m.get("author"), dict) else {}
    mid = m.get("message_id") or m.get("id"); cid = m.get("channel_id") or ev.get("channel_id"); gid = m.get("guild_id") or ev.get("guild_id")
    if not mid or not cid: return None
    text = m.get("text") or m.get("content") or ""
    card = m.get("card_text")
    if not card and m.get("embeds"):
        card = "\n".join(filter(None, (" ".join(filter(None, [e.get("title"), e.get("description")] + [f"{f.get('name','')}: {f.get('value','')}" for f in e.get("fields") or []])) for e in m["embeds"] if isinstance(e, dict))))
    return dict(message_id=str(mid), channel_id=str(cid), channel_name=m.get("channel_name"), text=text, card_text=card,
                author_name=m.get("author_name") or a.get("global_name") or a.get("username") or m.get("author"),
                message_timestamp=_ts(m.get("message_timestamp") or m.get("timestamp") or m.get("created_at")),
                jump_url=m.get("jump_url") or (f"https://discord.com/channels/{gid}/{cid}/{mid}" if gid else None))

class Feed:
    def __init__(s, on_message, log=print):
        s.url, s.token = _env("USSTOCK_WS_URL"), _env("USSTOCK_WS_TOKEN")
        s.on_message, s.log = on_message, log
        s.state = {"ws": "disabled" if not (s.url and s.token) else "connecting", "ws_channels": 0, "ws_connected_since": None,
                   "ws_last_event": None, "ws_messages": 0, "ws_reconnects": 0, "ws_error": None}
    def start(s):
        if s.url and s.token: threading.Thread(target=s.run, daemon=True, name="wsfeed").start()
        return s
    def run(s):
        backoff = 1
        while True:
            w = None
            try:
                s.state["ws"] = "connecting"; w = WS(s.url, s.token)
                s.state.update(ws="connected", ws_connected_since=time.time(), ws_error=None); backoff = 1
                w.send(json.dumps({"op": "list_channels"})); asked = time.time(); last_ping = time.time()
                while True:
                    try: raw = w.recv()
                    except (socket.timeout, TimeoutError):
                        w.send(b"", 9); last_ping = time.time(); continue
                    try: ev = json.loads(raw)
                    except ValueError: continue
                    t = ev.get("type")
                    if t == "channels":
                        ids = [c["channel_id"] for c in ev.get("channels") or [] if c.get("channel_id")]
                        if ids: w.send(json.dumps({"op": "subscribe", "channel_ids": ids}))
                        elif ev.get("channels_loading"): asked = 0  # server still loading; it re-sends, else we re-ask below
                    elif t == "subscribed":
                        s.state["ws_channels"] = len(ev.get("channels") or ev.get("channel_ids") or [])
                    elif t == "discord.message.created":
                        s.state["ws_last_event"] = time.time()
                        m = normalize(ev)
                        if m:
                            s.state["ws_messages"] += 1
                            try: s.on_message(m)
                            except Exception as e: s.log("ws push error:", type(e).__name__, e)
                    elif t == "error":
                        s.state["ws_error"] = str(ev.get("message") or ev)[:300]
                    if not s.state["ws_channels"] and asked == 0: w.send(json.dumps({"op": "list_channels"})); asked = time.time()
                    if time.time() - last_ping > 30: w.send(b"", 9); last_ping = time.time()
            except Exception as e:
                s.state.update(ws="reconnecting", ws_error=f"{type(e).__name__}: {str(e)[:200]}", ws_channels=0)
                s.state["ws_reconnects"] += 1
                s.log("ws disconnected:", s.state["ws_error"], f"retry in {backoff}s")
            finally:
                if w: w.close()
            time.sleep(backoff + random.random()); backoff = min(60, backoff * 2)
