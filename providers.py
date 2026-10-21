"""Pluggable data providers. Each returns normalized dict:
{symbol, price, change, change_pct, source_ts, source, options:[{expiry,type,strike,last,bid,ask,volume,oi,iv,delta}]}
Swap via env DATA_PROVIDER=cboe|alphavantage."""
import json, os, re, urllib.request

def _get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)

class CboeProvider:
    name = "Cboe delayed quotes (~15 min delay)"
    URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/{}.json"
    PAT = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")
    def fetch(self, t):
        d = _get(self.URL.format(t)); x = d["data"]; opts = []
        for o in x["options"]:
            m = self.PAT.match(o["option"])
            if not m: continue
            y = m.group(2)
            opts.append(dict(expiry=f"20{y[:2]}-{y[2:4]}-{y[4:]}", type=m.group(3),
                strike=int(m.group(4))/1000, last=o.get("last_trade_price"), bid=o.get("bid"),
                ask=o.get("ask"), volume=o.get("volume"), oi=o.get("open_interest"),
                iv=o.get("iv"), delta=o.get("delta")))
        return dict(symbol=t, price=x.get("current_price"), change=x.get("price_change"),
                    change_pct=x.get("price_change_percent"), source_ts=d.get("timestamp"),
                    source=self.name, options=opts)

class AlphaVantageProvider:
    """Alpha Vantage REALTIME_OPTIONS (or a 'US Stock VIP MCP' wrapper). Needs ALPHAVANTAGE_API_KEY."""
    name = "Alpha Vantage REALTIME_OPTIONS"
    def fetch(self, t):
        k = os.environ["ALPHAVANTAGE_API_KEY"]
        d = _get(f"https://www.alphavantage.co/query?function=REALTIME_OPTIONS&symbol={t}&require_greeks=true&apikey={k}")
        f = lambda v: float(v) if v not in (None, "") else None
        opts = [dict(expiry=o["expiration"], type="C" if o["type"] == "call" else "P", strike=f(o["strike"]),
                     last=f(o.get("last")), bid=f(o.get("bid")), ask=f(o.get("ask")), volume=f(o.get("volume")),
                     oi=f(o.get("open_interest")), iv=f(o.get("implied_volatility")), delta=f(o.get("delta")))
                for o in d.get("data", [])]
        q = _get(f"https://www.alphavantage.co/query?function=GLOBAL_QUOTE&symbol={t}&apikey={k}").get("Global Quote", {})
        return dict(symbol=t, price=f(q.get("05. price")), change=f(q.get("09. change")),
                    change_pct=f((q.get("10. change percent") or "").rstrip("%") or None),
                    source_ts=q.get("07. latest trading day"), source=self.name, options=opts)

PROVIDERS = {"cboe": CboeProvider, "alphavantage": AlphaVantageProvider}
def get_provider():
    return PROVIDERS[os.environ.get("DATA_PROVIDER", "cboe")]()

class McpProvider:
    """US Stock VIP MCP -> Alpha Vantage REALTIME_OPTIONS + GLOBAL_QUOTE; Cboe fallback for the chain."""
    name = "US Stock VIP MCP · Alpha Vantage REALTIME_OPTIONS"
    def __init__(s, mcp):
        s.mcp = mcp; s.cboe = CboeProvider(); s.unentitled_until = 0; s.last_error = None
    def quote(s, t):
        r = s.mcp.call("market_data_call_tool", {"tool_name": "GLOBAL_QUOTE", "arguments": {"symbol": t}})
        txt = r.get("result") if isinstance(r, dict) and "result" in r else (r.get("structuredContent", {}).get("result") if isinstance(r, dict) else r)
        if not isinstance(txt, str): txt = json.dumps(r)
        lines = [l for l in txt.strip().splitlines() if l.strip()]
        if len(lines) < 2 or "," not in lines[0]: raise RuntimeError("GLOBAL_QUOTE: " + txt[:300])
        q = dict(zip(lines[0].strip().split(","), lines[1].strip().split(",")))
        f = lambda v: float(v.rstrip("%")) if v not in (None, "") else None
        return dict(symbol=t, price=f(q.get("price")), change=f(q.get("change")), change_pct=f(q.get("changePercent")),
                    open=f(q.get("open")), high=f(q.get("high")), low=f(q.get("low")), volume=f(q.get("volume")),
                    prev_close=f(q.get("previousClose")), latest_day=q.get("latestDay"),
                    source="US Stock VIP MCP · Alpha Vantage GLOBAL_QUOTE")
    def chain(s, t):
        import time
        if time.time() > s.unentitled_until:
            try:
                r = s.mcp.call("market_data_call_tool", {"tool_name": "REALTIME_OPTIONS",
                               "arguments": {"symbol": t, "require_greeks": True}}, timeout=90)
                txt = r.get("content", [{}])[0].get("text", "") if isinstance(r, dict) and "content" in r else (r if isinstance(r, str) else json.dumps(r))
                d = json.loads(txt) if isinstance(txt, str) and txt.strip().startswith("{") else (r if isinstance(r, dict) else {})
                if "premium endpoint" in (d.get("message") or "") or "SAMPLE DATA" in (d.get("message") or ""):
                    s.last_error = d["message"]; s.unentitled_until = time.time() + 3600
                    raise RuntimeError("unentitled")
                if not d.get("data"): raise RuntimeError("REALTIME_OPTIONS: " + str(txt)[:300])
                f = lambda v: float(v) if v not in (None, "") else None
                opts = [dict(expiry=o["expiration"], type="C" if o["type"] == "call" else "P", strike=f(o["strike"]),
                        last=f(o.get("last")), bid=f(o.get("bid")), ask=f(o.get("ask")), volume=f(o.get("volume")),
                        oi=f(o.get("open_interest")), iv=f(o.get("implied_volatility")), delta=f(o.get("delta")))
                        for o in d["data"]]
                return dict(symbol=t, options=opts, source=s.name + " (real-time)", source_ts=d["data"][0].get("date"), fallback=None)
            except Exception as e:
                if str(e) != "unentitled": s.last_error = str(e)
        d = s.cboe.fetch(t)
        d["source"] = "Cboe delayed (~15 min) — fallback"; d["fallback"] = s.last_error
        return d


# ---------------------------------------------------------------------------
# Longbridge OpenAPI CLI provider (MARKET DATA ONLY).
# Only `quote`, `option chain`, `option quote` are ever executed; anything else is refused.
import subprocess, threading, time as _time, concurrent.futures as _cf
from datetime import datetime as _dt, timezone as _tz
from zoneinfo import ZoneInfo as _ZI
_ET = _ZI("America/New_York")
LB_BIN = os.environ.get("LONGBRIDGE_BIN", "/usr/local/bin/longbridge")
_LB_ALLOWED = (("quote",), ("option", "chain"), ("option", "quote"))

def lb(args, timeout=25):
    if not any(tuple(args[:len(a)]) == a for a in _LB_ALLOWED):
        raise PermissionError("longbridge subcommand not allowed (market data only): %r" % args[:2])
    r = subprocess.run([LB_BIN] + list(args) + ["--format", "json"], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError("longbridge %s rc=%s: %s" % (" ".join(args[:2]), r.returncode, (r.stderr or r.stdout).strip()[:400]))
    return json.loads(r.stdout or "null")

class _TTL:
    def __init__(s): s.lk = threading.Lock(); s.d = {}; s.klk = {}
    def get(s, key, ttl, fn):
        with s.lk:
            e = s.d.get(key)
            if e and _time.time() - e[0] < ttl: return e[1]
            kl = s.klk.setdefault(key, threading.Lock())
        with kl:  # single-flight per key
            with s.lk:
                e = s.d.get(key)
                if e and _time.time() - e[0] < ttl: return e[1]
            v = fn()
            with s.lk: s.d[key] = (_time.time(), v)
            return v

def _f(v):
    try: return float(v) if v not in (None, "") else None
    except (TypeError, ValueError): return None

class LongbridgeProvider:
    """Primary: Longbridge OpenAPI CLI (real-time last/IV/OI + pre/post/overnight underlying).
    Secondary: Cboe delayed, merged in for bid/ask/delta where (expiry,type,strike) matches.
    Each option row carries `fs` = {field: "Longbridge"|"Cboe"} for per-field source labels."""
    name = "Longbridge OpenAPI (real-time)"
    QUOTE_TTL, CHAIN_TTL, STRIKES_TTL, CBOE_TTL = 3, 5, 600, 60
    N_EXP, WINDOW, BATCH = 3, 0.12, 60
    def __init__(s, cboe=None):
        s.cboe = cboe or CboeProvider(); s.c = _TTL(); s.last_error = None; s.calls = 0
    def _lb(s, args, timeout=25):
        s.calls += 1; return lb(args, timeout)
    @staticmethod
    def _sym(t): t = t.upper(); return t if t.endswith(".US") else t + ".US"
    def _quote_raw(s, t):
        return s.c.get(("q", t), s.QUOTE_TTL, lambda: s._lb(["quote", s._sym(t)], 15)[0])
    def quote(s, t):
        t0 = _time.time(); q = s._quote_raw(t)
        ext = None
        for k in ("pre_market", "post_market", "overnight"):
            v = q.get(k)
            if v and v.get("timestamp") and (ext is None or v["timestamp"] > ext[1]["timestamp"]): ext = (k, v)
        price = _f(q.get("last"))
        ts = ext[1]["timestamp"] if ext else q.get("timestamp")
        return dict(symbol=t, price=price, change=_f(q.get("change_value")), change_pct=_f(q.get("change_percentage")),
                    open=_f(q.get("open")), high=_f(q.get("high")), low=_f(q.get("low")), volume=_f(q.get("volume")),
                    prev_close=_f(q.get("prev_close")), latest_day=_dt.now(_ET).date().isoformat(),
                    ext_session=ext[0] if ext else None, ext_price=_f(ext[1]["last"]) if ext else None, ext_ts=ts,
                    source_ts=ts, source="Longbridge OpenAPI quote", latency_ms=round((_time.time() - t0) * 1000))
    def _expiries(s, sym):
        today = _dt.now(_ET).date().isoformat()
        ex = s.c.get(("exp", sym), s.STRIKES_TTL, lambda: [e["expiry_date"] for e in s._lb(["option", "chain", sym], 15)])
        return [e for e in ex if e >= today][:s.N_EXP]
    def _strikes(s, sym, e):
        return s.c.get(("k", sym, e), s.STRIKES_TTL,
                       lambda: [float(r["strike"]) for r in s._lb(["option", "chain", sym, "--date", e], 20) if r.get("standard", "true") == "true"])
    @staticmethod
    def _occ(sym, e, cp, k): return "%s%s%s%d.US" % (sym[:-3], e.replace("-", "")[2:], cp, round(k * 1000))
    def _cboe(s, t):
        try: return s.c.get(("cboe", t), s.CBOE_TTL, lambda: s.cboe.fetch(t)), None
        except Exception as e: return None, "%s: %s" % (type(e).__name__, e)
    def chain(s, t):
        return s.c.get(("chain", t), s.CHAIN_TTL, lambda: s._chain(t))
    def _chain(s, t):
        t0 = _time.time(); sym = s._sym(t)
        with _cf.ThreadPoolExecutor(2) as ex:
            fc = ex.submit(s._cboe, t)  # secondary source in parallel
            q = s._quote_raw(t); spot = _f(q.get("last"))
            exps = s._expiries(sym); cons = []
            for e in exps:
                for k in s._strikes(sym, e):
                    if abs(k / spot - 1) <= s.WINDOW: cons += [s._occ(sym, e, "C", k), s._occ(sym, e, "P", k)]
            rows, errs = [], []
            for i in range(0, len(cons), s.BATCH):  # batched option quote calls
                try: rows += s._lb(["option", "quote"] + cons[i:i + s.BATCH], 30) or []
                except Exception as e: errs.append(str(e))
            cb, cberr = fc.result()
        if not rows: raise RuntimeError("Longbridge option quote returned no rows: " + "; ".join(errs)[:400])
        cbm = {(o["expiry"], o["type"], o["strike"]): o for o in (cb or {}).get("options", [])}
        opts, newest = [], ""
        for x in rows:
            e, cp, k = x["expiry_date"], x["direction"][0], float(x["strike_price"])
            last = _f(x.get("last")); last = last if last else None
            o = dict(expiry=e, type=cp, strike=k, last=last, bid=None, ask=None, volume=_f(x.get("volume")),
                     oi=_f(x.get("open_interest")), iv=_f(x.get("implied_volatility")), delta=None, ts=x.get("timestamp"))
            fs = {f: "Longbridge" for f in ("last", "volume", "oi", "iv")}
            c = cbm.get((e, cp, k))
            if c:
                for f in ("bid", "ask", "delta"):
                    if c.get(f) is not None: o[f] = c[f]; fs[f] = "Cboe"
                if o["last"] is None and c.get("last"): o["last"] = c["last"]; fs["last"] = "Cboe"
            o["fs"] = fs; opts.append(o); newest = max(newest, x.get("timestamp") or "")
        strad = {}
        for e in exps:
            byk = {}
            for o in opts:
                if o["expiry"] == e: byk.setdefault(o["strike"], {})[o["type"]] = o
            ks = [k for k, v in byk.items() if "C" in v and "P" in v]
            if not ks: continue
            k = min(ks, key=lambda k: abs(k - spot)); legs = {}
            for cp in "CP":
                o = byk[k][cp]
                if o["fs"].get("last") == "Longbridge" and o["last"]: legs[cp] = (o["last"], "Longbridge last")
                elif o["bid"] and o["ask"]: legs[cp] = ((o["bid"] + o["ask"]) / 2, "Cboe mid")
                elif o["last"]: legs[cp] = (o["last"], "Cboe last")
            if len(legs) == 2:
                v = legs["C"][0] + legs["P"][0]
                strad[e] = dict(strike=k, call=legs["C"][0], put=legs["P"][0], straddle=round(v, 2),
                                implied_move_pct=round(v / spot * 100, 2), call_src=legs["C"][1], put_src=legs["P"][1])
        cb_ts = (cb or {}).get("source_ts")
        s.last_error = ("Cboe secondary: " + cberr) if cberr else (("; ".join(errs)) or None)
        return dict(symbol=t, price=spot, change=_f(q.get("change_value")), change_pct=_f(q.get("change_percentage")),
                    source=s.name + " · bid/ask/delta: Cboe delayed (~15 min)" + (" [Cboe unavailable]" if cberr else ""),
                    source_ts=newest, cboe_ts=cb_ts, fetched_epoch=round(_time.time(), 3),
                    build_ms=round((_time.time() - t0) * 1000), n_contracts=len(opts),
                    n_cboe_matched=sum(1 for o in opts if "bid" in o["fs"]), expiries=exps, straddle=strad,
                    options=opts, fallback=s.last_error, primary="longbridge")

PROVIDERS["longbridge"] = LongbridgeProvider
