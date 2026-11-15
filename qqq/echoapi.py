"""Trade Echo-style 5-minute QQQ/SPX page data. Market data via lbsafe only (read-only).

Does not change /qqq, the 3-minute chart, or 新规则 v3.
"""
import os, json, math, subprocess, threading, time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
LB = os.environ.get('LBSAFE', os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'lbsafe'))
_lock = threading.Lock()
_cache = {}


def _run(args, timeout=40):
    r = subprocess.run([LB] + args + ['--format', 'json'], capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout or 'lbsafe failed')[:400])
    return json.loads(r.stdout)


def _cached(key, ttl, fn):
    with _lock:
        e = _cache.get(key)
        if e and time.time() - e[0] < ttl:
            return e[1]
    v = fn()
    with _lock:
        _cache[key] = (time.time(), v)
    return v


def _ts(iso):
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp())


def _et(ts):
    return datetime.fromtimestamp(ts, ET)


def _rth(ts):
    d = _et(ts)
    m = d.hour * 60 + d.minute
    return d.weekday() < 5 and 9 * 60 + 30 <= m < 16 * 60


def resolve(sym):
    s = (sym or 'QQQ').strip().upper()
    if s in ('SPX', '.SPX', 'SPX.US', '.SPX.US'):
        return {'id': 'SPX', 'bar': '.SPX.US', 'gex': 'SPY.US', 'scale': 'spy', 'label': 'SPX'}
    return {'id': 'QQQ', 'bar': 'QQQ.US', 'gex': 'QQQ.US', 'scale': None, 'label': 'QQQ'}


def _ema(vals, n):
    k = 2 / (n + 1)
    out, e = [], None
    for x in vals:
        e = x if e is None else x * k + e * (1 - k)
        out.append(round(e, 4))
    return out


def _rsi(closes, n=14):
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    g = l = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        if d > 0:
            g += d
        else:
            l -= d
    g /= n
    l /= n
    out[n] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        g = (g * (n - 1) + max(d, 0)) / n
        l = (l * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if l == 0 else 100 - 100 / (1 + g / l)
    return [None if x is None else round(x, 2) for x in out]


def _vwap(bars):
    out, pv, vv, day = [], 0.0, 0.0, None
    for b in bars:
        d = _et(b['t']).date()
        if d != day:
            day, pv, vv = d, 0.0, 0.0
        typ = (b['h'] + b['l'] + b['c']) / 3
        pv += typ * b['v']
        vv += b['v']
        out.append(round(pv / vv, 4) if vv else b['c'])
    return out


def money(x):
    if x is None:
        return '—'
    s = '-' if x < 0 else '+'
    a = abs(x)
    if a >= 1e9:
        return '%s$%.1fB' % (s, a / 1e9)
    if a >= 1e6:
        return '%s$%.1fM' % (s, a / 1e6)
    if a >= 1e3:
        return '%s$%.0fK' % (s, a / 1e3)
    return '%s$%.0f' % (s, a)


def _bars(symbol, days=3):
    raw = _run(['kline', symbol, '--period', '5m', '--count', '400', '--session', 'intraday'], 40)
    out = []
    for b in raw or []:
        t = _ts(b['time'])
        if not _rth(t):
            continue
        o, h, l, c = float(b['open']), float(b['high']), float(b['low']), float(b['close'])
        v = float(b.get('volume') or 0)
        turn = b.get('turnover')
        try:
            dollar = float(turn) if turn not in (None, '', 0, '0') else c * v
        except (TypeError, ValueError):
            dollar = c * v
        if v == 0 and h == l:
            continue
        flow = dollar if c >= o else -dollar
        out.append({'t': t, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v, 'flow': round(flow, 2)})
    days_rth = []
    for b in out:
        d = str(_et(b['t']).date())
        if d not in days_rth:
            days_rth.append(d)
    keep = set(days_rth[-days:])
    return [b for b in out if str(_et(b['t']).date()) in keep]


def _quote(symbol):
    d = _run(['quote', symbol], 15)[0]
    last = float(d['last'])
    prev = float(d.get('prev_close') or last)
    # Prefer last regular-session close as the RTH headline when extended-hours
    # last is only used if we later add an ext flag; this page is RTH-only.
    return {
        'last': last,
        'prev_close': prev,
        'open': d.get('open'),
        'high': d.get('high'),
        'low': d.get('low'),
        'change': last - prev,
        'change_pct': (last / prev - 1) * 100 if prev else 0,
        'raw': d,
    }


def _ncdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_gamma(S, K, iv, T):
    if iv <= 0 or T <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    return math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (S * iv * math.sqrt(T))


def _tte(expiry):
    close = datetime.fromisoformat(expiry + 'T16:00:00').replace(tzinfo=ET)
    return max((close - datetime.now(ET)).total_seconds() / (365 * 86400), 1 / (365 * 24 * 2))


def _occ(sym, exp, cp, k):
    tk = sym[:-3]
    y = exp.replace('-', '')[2:]
    return '%s%s%s%d.US' % (tk, y, cp, int(round(k * 1000)))


def _gex_raw(under, spot):
    today = datetime.now(ET).date().isoformat()
    exps = [e['expiry_date'] for e in _run(['option', 'chain', under], 20)]
    exps = [e for e in exps if e >= today][:2]
    if not exps:
        return {'ok': False, 'error': 'no expiry'}
    cons, meta = [], []
    for e in exps:
        ch = _run(['option', 'chain', under, '--date', e], 25)
        ks = [float(r['strike']) for r in ch if r.get('standard', 'true') == 'true']
        ks = [k for k in ks if abs(k / spot - 1) <= 0.08]
        for k in ks:
            cons += [_occ(under, e, 'C', k), _occ(under, e, 'P', k)]
    rows = []
    for i in range(0, len(cons), 50):
        try:
            rows += _run(['option', 'quote'] + cons[i:i + 50], 35) or []
        except Exception:
            pass
    if not rows:
        return {'ok': False, 'error': 'no option quotes', 'exps': exps}
    net, call_g, put_g = {}, {}, {}
    used = 0
    for x in rows:
        try:
            k = float(x['strike_price'])
            oi = int(x.get('open_interest') or 0)
            iv = float(x.get('implied_volatility') or 0)
            cp = (x.get('direction') or 'C')[0].upper()
            e = x['expiry_date']
        except (TypeError, ValueError, KeyError):
            continue
        if oi <= 0 or iv <= 0:
            continue
        g = bs_gamma(spot, k, iv, _tte(e))
        # User-specified GEX: OI × gamma × 100 × spot; calls +, puts −
        val = oi * g * 100 * spot * (1 if cp == 'C' else -1)
        net[k] = net.get(k, 0) + val
        if cp == 'C':
            call_g[k] = call_g.get(k, 0) + val
        else:
            put_g[k] = put_g.get(k, 0) + val
        used += 1
    if not net:
        return {'ok': False, 'error': 'no gex rows', 'exps': exps}
    anchor = max(net, key=lambda k: abs(net[k]))
    # Gamma flip: sign change of net GEX across strikes (sorted), midpoint.
    flip = None
    ks = sorted(net)
    for a, b in zip(ks, ks[1:]):
        if net[a] == 0 or net[a] * net[b] < 0:
            cand = (a + b) / 2
            if flip is None or abs(cand - spot) < abs(flip - spot):
                flip = cand
    # Drift: strike nearest the gamma flip, else largest call-GEX above spot.
    drift = None
    if flip is not None:
        drift = min(net, key=lambda k: abs(k - flip))
        if drift == anchor:
            drift = None
    if drift is None:
        above = [(call_g.get(k, 0), k) for k in call_g if k > spot + 0.01 and k != anchor]
        drift = max(above)[1] if above else None
    if drift is None:
        rest = [k for k in net if k != anchor]
        if rest:
            drift = max(rest, key=lambda k: abs(net[k]))
    majors = sorted(net, key=lambda k: -abs(net[k]))
    others = [k for k in majors if k not in (anchor, drift)][:5]
    return {
        'ok': True,
        'anchor': round(anchor, 2),
        'drift': round(drift, 2) if drift is not None else None,
        'flip': round(flip, 2) if flip is not None else None,
        'others': [round(k, 2) for k in others],
        'net': {str(k): round(v, 1) for k, v in net.items()},
        'exps': exps,
        'n': used,
        'spot_used': spot,
    }


_gex_pend = set()

def gex(spec, spot, wait=False):
    ttl = 300 if _rth(int(time.time())) else 1800

    def compute():
        if spec['scale'] == 'spy':
            spy_px = _quote('SPY.US')['last']
            raw = _gex_raw('SPY.US', spy_px)
            if raw.get('ok'):
                ratio = float(spot) / spy_px
                sc = lambda x: None if x is None else round(x * ratio, 2)
                raw['anchor'] = sc(raw['anchor'])
                raw['drift'] = sc(raw['drift'])
                raw['flip'] = sc(raw.get('flip'))
                raw['others'] = [sc(k) for k in raw.get('others') or []]
                raw['ratio'] = round(ratio, 4)
                raw['source'] = 'SPY 期权（Longbridge OI×BS Gamma），按 SPX/SPY=%.4f 换算' % ratio
                raw['approx'] = 'SPY×%.2f' % ratio
        else:
            raw = _gex_raw(spec['gex'], float(spot))
            if raw.get('ok'):
                raw['source'] = 'QQQ 期权（Longbridge OI × Black-Scholes Gamma × 100 × spot；Call 为正、Put 为负）'
                raw['approx'] = None
        raw['asof'] = datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')
        return raw

    key = ('gex', spec['id'])
    with _lock:
        e = _cache.get(key)
        fresh = e and time.time() - e[0] < ttl
        stale = e[1] if e else None
        running = spec['id'] in _gex_pend
        if not fresh and not running:
            _gex_pend.add(spec['id'])
            def go():
                try:
                    v = compute()
                    with _lock:
                        _cache[key] = (time.time(), v)
                except Exception as ex:
                    with _lock:
                        _cache[key] = (time.time() - ttl + 20, {'ok': False, 'error': str(ex)})
                finally:
                    with _lock:
                        _gex_pend.discard(spec['id'])
            threading.Thread(target=go, daemon=True).start()
    if wait:
        for _ in range(60):
            with _lock:
                e = _cache.get(key)
                if e and time.time() - e[0] < ttl and not e[1].get('pending'):
                    return e[1]
            time.sleep(1)
    if stale and stale.get('ok'):
        return stale
    if fresh and e:
        return e[1]
    return {'ok': False, 'pending': True}



def payload(sym='QQQ'):
    spec = resolve(sym)
    bars = _cached(('bars', spec['bar']), 5 if _rth(int(time.time())) else 60, lambda: _bars(spec['bar']))
    q = _cached(('q', spec['bar']), 3 if _rth(int(time.time())) else 30, lambda: _quote(spec['bar']))
    closes = [b['c'] for b in bars]
    ema8 = _ema(closes, 8)
    ema21 = _ema(closes, 21)
    vwap = _vwap(bars)
    rsi = _rsi(closes, 14)
    last_day = str(_et(bars[-1]['t']).date()) if bars else None
    day_flow = sum(b['flow'] for b in bars if str(_et(b['t']).date()) == last_day) if last_day else 0
    last_flow = bars[-1]['flow'] if bars else 0
    # headline price: last RTH close from the last 5m bar when not in RTH
    last_px = bars[-1]['c'] if bars else q['last']
    if _rth(int(time.time())):
        last_px = q['last']
    prev = q['prev_close']
    chg = last_px - prev
    chg_pct = (last_px / prev - 1) * 100 if prev else 0
    last_t = bars[-1]['t'] if bars else int(time.time())
    try:
        g = gex(spec, last_px)
    except Exception as e:
        g = {'ok': False, 'error': str(e)}
    return {
        'symbol': spec['label'],
        'bar_symbol': spec['bar'],
        'period': '5m',
        'session': 'RTH',
        'last': round(last_px, 2),
        'prev_close': round(prev, 2),
        'change': round(chg, 2),
        'change_pct': round(chg_pct, 2),
        'asof_et': (lambda d: d.strftime('%b ') + str(d.day) + d.strftime(', %Y · ') + d.strftime('%I:%M %p').lstrip('0') + ' ET')(_et(last_t)),
        'asof_ts': last_t,
        'bars': bars,
        'ema8': ema8,
        'ema21': ema21,
        'vwap': vwap,
        'rsi': rsi,
        'flow': [b['flow'] for b in bars],
        'flow_last': last_flow,
        'flow_last_lbl': money(last_flow),
        'flow_day': day_flow,
        'flow_day_lbl': money(day_flow),
        'flow_note': '标的净成交额（阳线为+，阴线为−，用成交额；不是期权资金流）',
        'gex': g,
        'source': {
            'bars': 'Longbridge 5m kline, session=intraday (09:30–16:00 ET)',
            'vwap': 'session VWAP, reset 09:30 ET, typical price (H+L+C)/3',
            'cloud': 'EMA 8 / EMA 21 on 5-minute closes',
            'rsi': 'RSI 14 (Wilder) on 5-minute closes',
        },
    }


def prewarm():
    def run():
        for s in ('QQQ', 'SPX'):
            try:
                payload(s)
            except Exception as e:
                print('echo prewarm', s, repr(e), flush=True)
    threading.Thread(target=run, daemon=True).start()
