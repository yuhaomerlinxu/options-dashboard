"""Minimal MCP streamable-HTTP JSON-RPC client (stdlib). Config from env vars only."""
import json, os, threading, urllib.request, urllib.error, itertools
class MCP:
    def __init__(s):
        s.url = os.environ["USSTOCK_MCP_URL"]; s.key = os.environ["USSTOCK_MCP_KEY"]
        s.sid = None; s.ids = itertools.count(1); s.lock = threading.Lock(); s.tools = None
    def _post(s, body, timeout=60):
        h = {"User-Agent": "curl/8.5.0", "X-API-Key": s.key, "Content-Type": "application/json",
             "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26"}
        if s.sid: h["Mcp-Session-Id"] = s.sid
        req = urllib.request.Request(s.url, data=json.dumps(body).encode(), headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            sid = r.headers.get("Mcp-Session-Id")
            if sid: s.sid = sid
            ct = r.headers.get("Content-Type", ""); raw = r.read().decode("utf-8", "replace")
        if not raw.strip(): return None
        if "text/event-stream" in ct:
            res = None
            for ev in raw.split("\n\n"):
                data = "\n".join(l[5:].lstrip() for l in ev.splitlines() if l.startswith("data:"))
                if data:
                    try:
                        m = json.loads(data)
                        if "id" in m and body.get("id") == m["id"]: res = m
                    except ValueError: pass
            return res
        return json.loads(raw)
    def _init(s):
        s.sid = None
        r = s._post({"jsonrpc": "2.0", "id": next(s.ids), "method": "initialize", "params": {
            "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "options-dashboard", "version": "2"}}})
        s._post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        s.tools = [t["name"] for t in (s._post({"jsonrpc": "2.0", "id": next(s.ids), "method": "tools/list"}) or {}).get("result", {}).get("tools", [])]
        return r
    def call(s, name, args, timeout=60):
        with s.lock:
            for attempt in (0, 1):
                if s.tools is None: s._init()
                try:
                    r = s._post({"jsonrpc": "2.0", "id": next(s.ids), "method": "tools/call",
                                 "params": {"name": name, "arguments": args}}, timeout)
                except urllib.error.HTTPError as e:
                    if e.code in (400, 404) and attempt == 0: s.tools = None; continue  # session expired
                    raise
                break
        if r is None: raise RuntimeError("empty MCP response")
        if "error" in r: raise RuntimeError(f"MCP error: {r['error']}")
        res = r["result"]
        txt = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        if res.get("structuredContent") is not None: out = res["structuredContent"]
        else:
            try: out = json.loads(txt)
            except ValueError: out = txt
        if res.get("isError"): raise RuntimeError(f"tool error: {txt[:500]}")
        return out
