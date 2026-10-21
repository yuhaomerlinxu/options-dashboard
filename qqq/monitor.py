#!/usr/bin/env python3
"""QQQ intraday multi-timeframe V-reversal monitor (read-only market data via longbridge CLI).
ONLY uses read commands: quote, kline, option chain, option quote, trading days. Never trades."""
import json, os, subprocess, threading, time, logging, urllib.request
from datetime import datetime, timedelta, timezone, date
from zoneinfo import ZoneInfo
from signals import parse_bars, resample, Confirmer

BASE = os.path.dirname(os.path.abspath(__file__))
ET, BJ = ZoneInfo("America/New_York"), ZoneInfo("Asia/Shanghai")
CFG = json.load(open(os.path.join(BASE, "config.json")))
LB = os.environ.get("LBSAFE", "/app/lbsafe")
ALLOWED = {"quote", "kline", "option", "trading", "alert"}  # alert = price alerts only, never orders  # hard whitelist of read-only subcommands
logging.basicConfig(filename=os.path.join(BASE, "monitor.log"), level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("qqq")

def lb(*args, timeout=30):
    if args[0] not in ALLOWED:
        raise RuntimeError(f"blocked non-read command {args[0]}")
    r = subprocess.run([LB, *args, "--format", "json"], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"longbridge {' '.join(args)} rc={r.returncode}: {r.stderr.strip()[:300]}")
    return json.loads(r.stdout)

# ---------- webhook config ----------
def load_webhook():
    conf = {}
    p = os.path.join(BASE, "webhook.env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                conf[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("QQQ_WEBHOOK_URL", "QQQ_WEBHOOK_KEY", "KEY_HEADER", "KEY_PREFIX"):
        if os.environ.get(k):
            conf[k] = os.environ[k]
    return conf

def push_ntfy(event):
    try:
        topic=os.environ.get("NTFY_TOPIC")
        fp=os.path.join(BASE,"ntfy.env")
        if os.path.exists(fp):
            for l in open(fp):
                if l.startswith("NTFY_TOPIC="): topic=l.strip().split("=",1)[1]
        if not topic: return
        d=str(event.get("direction","")).lower()
        title=("QQQ 1/2/3m bullish" if "bull" in d else "QQQ 1/2/3m bearish" if "bear" in d else "QQQ signal")
        parts=[]
        for k,v in event.items():
            if isinstance(v,(dict,list)): v=json.dumps(v,ensure_ascii=False,default=str)
            parts.append(f"{k}: {v}")
        body="\n".join(parts)[:3500]
        req=urllib.request.Request("https://ntfy.sh/"+topic,data=body.encode(),method="POST",
            headers={"Title":title,"Priority":"urgent","Tags":"chart_with_upwards_trend" if "bull" in d else "chart_with_downwards_trend"})
        with urllib.request.urlopen(req,timeout=10) as r: log.info("ntfy status=%s",r.status)
    except Exception as e:
        log.warning("ntfy failed: %s",e)

def push_lb_alert(event):
    """Disabled: Longbridge alert creation is not a market-data command."""
    return

def emit(event):
    line = json.dumps(event, ensure_ascii=False, default=str)
    with open(os.path.join(BASE, "alerts.jsonl"), "a") as f:
        f.write(line + "\n")
    log.info("EVENT %s", line)
    # push_lb_alert disabled in Render build: `longbridge alert add` is not market-data-only
    push_ntfy(event)
    wh = load_webhook()
    url = wh.get("QQQ_WEBHOOK_URL")
    if not url:
        log.info("no webhook configured; event written to alerts.jsonl only")
        return
    headers = {"Content-Type": "application/json"}
    key = wh.get("QQQ_WEBHOOK_KEY")
    if key:
        hdr = wh.get("KEY_HEADER") or CFG.get("key_header", "Authorization")
        prefix = wh.get("KEY_PREFIX", CFG.get("key_prefix", "Bearer ") if hdr.lower() == "authorization" else "")
        headers[hdr] = prefix + key
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=line.encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=10) as r:
                log.info("webhook POST status=%s", r.status)
                return
        except Exception as e:
            log.warning("webhook attempt %d failed: %s", attempt + 1, e)
            time.sleep(2)

# ---------- session ----------
_days = {"fetched": None, "days": set(), "half": set()}
def session_bounds(now_et):
    d = now_et.date()
    if _days["fetched"] != d:
        try:
            j = lb("trading", "days", "US")
            _days.update(fetched=d, days=set(j.get("trading_days", [])), half=set(j.get("half_trading_days", [])))
        except Exception as e:
            log.warning("trading days fetch failed (%s); using weekday rule", e)
            _days.update(fetched=d, days={d.isoformat()} if d.weekday() < 5 else set(), half=set())
    if d.isoformat() not in _days["days"]:
        return None
    o = now_et.replace(hour=9, minute=30, second=0, microsecond=0)
    c = now_et.replace(hour=13 if d.isoformat() in _days["half"] else 16, minute=0, second=0, microsecond=0)
    return o, c

# ---------- options (background, cached) ----------
class Options:
    def __init__(self):
        self.data, self.lock, self.spot = {}, threading.Lock(), None
        threading.Thread(target=self.loop, daemon=True).start()
    def loop(self):
        while True:
            try:
                if self.spot and session_bounds(datetime.now(ET)):
                    self.refresh()
            except Exception as e:
                log.warning("option refresh failed: %s", e)
            time.sleep(CFG["option_cache_sec"])
    def refresh(self):
        today = datetime.now(ET).date()
        exp = today.strftime("%Y-%m-%d")
        chain = lb("option", "chain", CFG["symbol"], "--date", exp)
        strikes = sorted(float(r["strike"]) for r in chain)
        if not strikes:
            raise RuntimeError("no 0DTE chain")
        spot = self.spot
        near = sorted(strikes, key=lambda k: abs(k - spot))[: 2 * CFG["option_strike_range"]]
        root = CFG["symbol"].split(".")[0]
        syms = [f"{root}{today:%y%m%d}{cp}{int(round(k*1000))}.US" for k in near for cp in "CP"]
        quotes = []
        for i in range(0, len(syms), 40):
            quotes += lb("option", "quote", *syms[i:i+40])
        calls = [q for q in quotes if q.get("direction") == "Call"]
        puts = [q for q in quotes if q.get("direction") == "Put"]
        cw = max(calls, key=lambda q: q.get("open_interest") or 0)
        pw = max(puts, key=lambda q: q.get("open_interest") or 0)
        atm = min(near, key=lambda k: abs(k - spot))
        ac = next((q for q in calls if float(q["strike_price"]) == atm), None)
        ap = next((q for q in puts if float(q["strike_price"]) == atm), None)
        f = lambda q: None if not q else dict(symbol=q["symbol"], strike=float(q["strike_price"]),
                                              last=float(q["last"]), iv=float(q["implied_volatility"]),
                                              oi=q.get("open_interest"), volume=q.get("volume"))
        with self.lock:
            self.data = dict(expiry=exp, updated_bj=datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
                             strikes_scanned=len(near),
                             call_wall=dict(strike=float(cw["strike_price"]), oi=cw.get("open_interest")),
                             put_wall=dict(strike=float(pw["strike_price"]), oi=pw.get("open_interest")),
                             atm_call=f(ac), atm_put=f(ap))
        log.info("options cache updated: CW %s PW %s ATM %s", self.data["call_wall"], self.data["put_wall"], atm)
    def get(self):
        with self.lock:
            return dict(self.data)

# ---------- main ----------
def main():
    log.info("monitor starting pid=%s", os.getpid())
    opts = Options()
    conf = Confirmer(CFG)
    last_quote = last_kline = 0
    quote = {}
    processed = {}
    sleeping_logged = False
    while True:
        now_et = datetime.now(ET)
        b = session_bounds(now_et)
        if not b or not (b[0] <= now_et < b[1] + timedelta(seconds=70)):
            if not sleeping_logged:
                log.info("outside RTH (%s ET); sleeping", now_et.strftime("%Y-%m-%d %H:%M"))
                sleeping_logged = True
                conf = Confirmer(CFG); processed = {}
            time.sleep(30); continue
        sleeping_logged = False
        t = time.time()
        try:
            if t - last_quote >= CFG["quote_interval_sec"]:
                quote = lb("quote", CFG["symbol"])[0]; last_quote = t
                opts.spot = float(quote["last"])
            if t - last_kline >= CFG["kline_interval_sec"] and now_et.second >= 3:
                raw = lb("kline", CFG["symbol"], "--period", "1m", "--count", str(CFG["kline_count"]))
                last_kline = t
                now_utc = datetime.now(timezone.utc)
                bars = [x for x in parse_bars(raw) if x["t"] + timedelta(minutes=1) <= now_utc]  # closed only
                if bars:
                    for tf in CFG["timeframes"]:
                        rb = resample(bars, tf)
                        if rb and processed.get(tf) != rb[-1]["t"]:
                            processed[tf] = rb[-1]["t"]
                            conf.update(tf, rb)
                    sig_now = bars[-1]["t"] + timedelta(minutes=1)
                    r = conf.check(sig_now)
                    if r:
                        d, why = r
                        emit(dict(event="qqq_mtf_signal", symbol=CFG["symbol"], direction=d,
                                  trigger_time_bj=datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S"),
                                  data_timestamp_utc=bars[-1]["t"].isoformat(),
                                  data_timestamp_bj=bars[-1]["t"].astimezone(BJ).strftime("%Y-%m-%d %H:%M:%S"),
                                  price=float(quote.get("last", bars[-1]["c"])),
                                  day_high=float(quote.get("high", 0)), day_low=float(quote.get("low", 0)),
                                  timeframes=why, options_0dte=opts.get()))
        except Exception as e:
            log.exception("loop error: %s", e)
            time.sleep(5)
        time.sleep(1)

if __name__ == "__main__":
    main()
