"""Standalone SPX displacement / king / sigma board (/spx/levels).

Market data: Longbridge .SPX.US klines (read-only via lbsafe).
0DTE strikes and implied 1σ: Futu SPXW chain, pushed from the box
because Render cannot reach OpenD. Never places orders.

Level and signal definitions are inferred from the reference screenshot
(11/25 session) and documented in payload['rule'] / the on-page rule box.
"""
import json, math, os, subprocess, time, threading
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
BJ = ZoneInfo('Asia/Shanghai')
LB = os.environ.get('LBSAFE', '/app/lbsafe')

# |body| >= k × ATR(14). 15m uses a slightly higher k so weaker bars
# that do not appear on the reference chart are dropped. 1h matches
# include a k=0.84 bar (11/25 09:30), so the floor is 0.80.
K_MIN = {'15m': 1.0, '1h': 0.80, '4h': 0.80, 'day': 0.80, 'week': 0.80, 'month': 0.80}
# most-recent kept after same-TF merge: (bull, bear)
MAX_KEEP = {'15m': (1, 2), '1h': (1, 4), '4h': (1, 1), 'day': (2, 2), 'week': (2, 2), 'month': (2, 2)}
CONFLUENCE_PTS = 13    # +N on a label = other levels/strikes within this many points
MERGE_TF_PTS = 8       # same-TF displacements this close share one level (keep earliest)
NEAR_TF_PTS = 12       # drop a higher-TF mid if a lower-TF level already marks the same move
MIN_SEP_PTS = 20       # same-TF, same-side levels closer than this: keep the more recent
TOUCH_PTS = 2.5
SWEEP_PTS = 1.5
SESSIONS = 3
ATR_N, EMA_FAST, EMA_SLOW = 14, 9, 20
WINDOW_PTS = 450       # keep HTF levels this far from spot (off-screen labels)

# timeframe, lbsafe period, history count, short label
TFS = (
    ('15m', '15m', 400, '15m'),
    ('1h',  '1h',  400, '1h'),
    ('4h',  '4h',  200, '4H'),
    ('day', 'day', 120, 'Day'),
    ('week','week', 80, 'Week'),
    ('month','month', 48, 'M'),
)
# 4H on the chart is EMA8, not a 4h displacement. Still scan 4h disps internally but do not plot them.
PLOT_DISP_TF = {'15m', '1h', 'day', 'week', 'month'}

BULL_COLOR = '#26c281'
BEAR_COLOR = '#e57373'
KING_COLOR = '#ff9800'
FOURH_COLOR = '#eceff1'
SIGMA_COLOR = '#90a4ae'
ODTE_COLOR = '#78909c'

_lk = threading.Lock()
_c = {}

def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None

def _ts(iso):
    return int(datetime.fromisoformat(iso.replace('Z', '+00:00')).timestamp())

def _et(ts):
    return datetime.fromtimestamp(ts, ET)

def lb(args, to=30):
    r = subprocess.run([LB] + args + ['--format', 'json'], capture_output=True, text=True, timeout=to)
    if not r.stdout.strip():
        raise RuntimeError((r.stderr or 'empty lbsafe output')[:200])
    return json.loads(r.stdout)

def cached(key, ttl, fn):
    with _lk:
        e = _c.get(key)
        if e and time.time() - e[0] < ttl:
            return e[1]
    v = fn()
    with _lk:
        _c[key] = (time.time(), v)
    return v

def quote():
    def f():
        return lb(['quote', '.SPX.US'], 12)[0]
    return cached('q', 5, f)

def kline(period, count, session='intraday'):
    def f():
        raw = lb(['kline', '.SPX.US', '--period', period, '--count', str(count), '--session', session], 40)
        out = []
        for b in raw:
            out.append({
                't': _ts(b['time']),
                'o': float(b['open']), 'h': float(b['high']),
                'l': float(b['low']), 'c': float(b['close']),
                'v': float(b.get('volume') or 0),
            })
        return out
    return cached(('k', period, count, session), 20, f)

def ema_series(xs, n):
    out = [None] * len(xs)
    if not xs:
        return out
    k = 2.0 / (n + 1)
    s = 0.0
    for i, x in enumerate(xs):
        if i < n:
            s += x
            if i == n - 1:
                out[i] = s / n
        else:
            out[i] = out[i - 1] + k * (x - out[i - 1])
    return out

def atr_series(bars, n=ATR_N):
    out = [None] * len(bars)
    if not bars:
        return out
    trs = []
    for i, b in enumerate(bars):
        if i == 0:
            trs.append(b['h'] - b['l'])
        else:
            trs.append(max(b['h'] - b['l'], abs(b['h'] - bars[i - 1]['c']), abs(b['l'] - bars[i - 1]['c'])))
    s = 0.0
    for i, tr in enumerate(trs):
        if i < n:
            s += tr
            if i == n - 1:
                out[i] = s / n
        else:
            out[i] = (out[i - 1] * (n - 1) + tr) / n
    return out

def typical(bar):
    return (bar['h'] + bar['l'] + bar['c']) / 3.0

def _session_closed(now):
    # Winter close 16:00 ET; weekends always closed. Half-days close 13:00
    # is handled by last bars already being complete after that close.
    return now.weekday() >= 5 or now.hour > 16 or (now.hour == 16 and now.minute >= 0)

def _period_minutes(period):
    return {'15m': 15, '1h': 60, '4h': 240, 'day': 24 * 60, 'week': 7 * 24 * 60, 'month': 31 * 24 * 60}.get(period)

def bar_is_complete(bar, period, now=None):
    now = now or datetime.now(ET)
    t = _et(bar['t'])
    if period == 'day':
        return t.date() < now.date() or (t.date() == now.date() and _session_closed(now))
    if period == 'week':
        last_monday = t.date() - timedelta(days=t.weekday())
        now_monday = now.date() - timedelta(days=now.weekday())
        return last_monday < now_monday or (last_monday == now_monday and _session_closed(now) and now.weekday() >= 4)
    if period == 'month':
        return (t.year, t.month) < (now.year, now.month)
    # intraday: a bar from a prior session is complete; today's last bar is complete after the close
    if t.date() < now.date() or _session_closed(now):
        return True
    mins = _period_minutes(period)
    if not mins:
        return True
    end = t + timedelta(minutes=mins)
    return now >= end

def round2(x):
    """Round-half-up to 2 decimals (6882.785 → 6882.79, matching the screenshot)."""
    if x is None:
        return None
    return math.floor(x * 100.0 + 0.5) / 100.0

def fmt_px(px, integers=False):
    if px is None:
        return ''
    if integers:
        return f'{int(round(px)):,}'
    return f'{round2(px):,.2f}'

def displacements(bars, period, k_min):
    """Body-midpoint (O+C)/2 of completed candles with |body| >= k_min × ATR(14)."""
    atrs = atr_series(bars)
    now = datetime.now(ET)
    found = []
    for i, b in enumerate(bars):
        a = atrs[i]
        if not a or a <= 0:
            continue
        if not bar_is_complete(b, period, now):
            continue
        body = b['c'] - b['o']
        if abs(body) < k_min * a:
            continue
        mid = (b['o'] + b['c']) / 2.0
        found.append({
            't': b['t'],
            'px': round2(mid),
            'open': round(b['o'], 2),
            'close': round(b['c'], 2),
            'body': round(body, 2),
            'atr': round(a, 2),
            'k': round(abs(body) / a, 2),
            'side': 'bull' if body > 0 else 'bear',
            'tf': period,
        })
    return found

def _merge_same_tf(items, pts=MERGE_TF_PTS):
    """Same TF + side, close in price: keep the earliest (original) displacement."""
    items = sorted(items, key=lambda x: x['t'])
    kept = []
    for it in items:
        hit = None
        for k in kept:
            if k['side'] == it['side'] and abs(k['px'] - it['px']) <= pts:
                hit = k
                break
        if hit is None:
            kept.append(dict(it, also=[]))
        else:
            hit.setdefault('also', []).append(it)
            # keep the original (earlier) mid; record later touches
    return kept

def _cap_recent(items, max_bull, max_bear, min_sep=MIN_SEP_PTS):
    """Keep the most recent N per side, skipping a mid that sits too close to one already kept."""
    def pick(side, n):
        arr = sorted([x for x in items if x['side'] == side], key=lambda x: -x['t'])
        out = []
        for it in arr:
            if any(abs(it['px'] - o['px']) < min_sep for o in out):
                continue
            out.append(it)
            if len(out) >= n:
                break
        return out
    chosen = pick('bull', max_bull) + pick('bear', max_bear)
    chosen.sort(key=lambda x: -x['t'])
    return chosen

def tpo_poc(bars, bin_size=5):
    """Typical-price time-at-price POC. Returns (bin_start, bin_center, count).

    Volume POC is not used: on a crash-and-recover day the volume peak sits
    at the lows and does not match the screenshot's mid-range King.
    Each 5m bar contributes +1 to the bin of its typical price (H+L+C)/3.
    The displayed King is the 5-point bin start (11/25: 7177.5 → 7175).
    """
    if not bars:
        return None
    buckets = {}
    for b in bars:
        tp = typical(b)
        k = int(math.floor(tp / bin_size + 1e-12))
        buckets[k] = buckets.get(k, 0) + 1
    # tie-break: more prints, then closer to last close
    last = bars[-1]['c']
    best = max(buckets, key=lambda k: (buckets[k], -abs(k * bin_size + bin_size / 2.0 - last)))
    return {
        'bin_start': best * bin_size,
        'bin_center': best * bin_size + bin_size / 2.0,
        'n': buckets[best],
        'bin': bin_size,
    }

def session_vwap_sigma(bars):
    """Full-session VWAP ± 1σ (volume-weighted typical). Kept for context; not the σ line on the image."""
    if not bars:
        return None
    last_day = max(_et(b['t']).date() for b in bars)
    day = [b for b in bars if _et(b['t']).date() == last_day]
    return _vwap_sigma(day, last_day.isoformat())

def last_hours_vwap_sigma(bars, hours=3):
    """VWAP ± 1σ of the last `hours` hours of the most recent session.

    On 11/25, 13:00–16:00 ET (last 3 hours) gives −1σ = 7157.69, which is
    the screenshot's 7157 σ line. Session-long VWAP+1σ is 7164 and does not match.
    """
    if not bars:
        return None
    last_day = max(_et(b['t']).date() for b in bars)
    day = [b for b in bars if _et(b['t']).date() == last_day]
    if not day:
        return None
    end = _et(day[-1]['t'])
    # include the last bar's period (5m)
    cutoff = end - timedelta(hours=hours) + timedelta(minutes=5)
    # If the session is shorter than `hours` (half day / first hours), use all available bars.
    win = [b for b in day if _et(b['t']) >= cutoff]
    if len(win) < 6:
        win = day
    rec = _vwap_sigma(win, last_day.isoformat())
    if rec:
        rec['window'] = 'last_%dh' % hours
        rec['from_et'] = _et(win[0]['t']).strftime('%H:%M')
        rec['to_et'] = _et(win[-1]['t']).strftime('%H:%M')
    return rec

def _vwap_sigma(day, session):
    pv = vv = 0.0
    acc = []
    for b in day:
        tp = typical(b)
        v = b['v'] if b['v'] > 0 else 1.0
        pv += tp * v
        vv += v
        acc.append((tp, v))
    if vv <= 0 or not acc:
        return None
    vwap = pv / vv
    var = sum((tp - vwap) ** 2 * v for tp, v in acc) / vv
    sig = math.sqrt(max(var, 0.0))
    return {
        'vwap': round(vwap, 2),
        'sigma': round(sig, 2),
        'upper': round(vwap + sig, 2),
        'lower': round(vwap - sig, 2),
        'session': session,
        'n': len(day),
    }

def four_h_ema(bars4, n=8):
    """4-hour EMA of closes, evaluated at the previous completed 4h bar.

    On 11/25 after the close this is EMA8 through the 09:30 4h close = 7194.55,
    0.58 points from the screenshot's 4H 7,195.13. The in-progress/last 4h
    close is excluded so a developing afternoon bar does not yank the line.
    Residual is attributed to vendor OHLC differences; we do not invent 7195.13.
    """
    if not bars4:
        return None
    now = datetime.now(ET)
    completed = [b for b in bars4 if bar_is_complete(b, '4h', now)]
    if len(completed) < n + 1:
        src = completed
        idx = len(src) - 1
    else:
        # lag one completed bar (use EMA as of the prior 4h close)
        src = completed
        idx = len(src) - 2
    closes = [b['c'] for b in src]
    e = ema_series(closes, n)
    if idx < 0 or e[idx] is None:
        return None
    return {
        'px': round(e[idx], 2),
        'n': n,
        'at': src[idx]['t'],
        'at_et': _et(src[idx]['t']).strftime('%Y-%m-%d %H:%M'),
        'last_close': round(src[idx]['c'], 2),
    }

def week_start(d):
    return d - timedelta(days=d.weekday())

def _load_futu_snap():
    try:
        import spxlevels_snap
        return spxlevels_snap.get_snap()
    except Exception:
        return None

def _visible_sessions(bars, n=SESSIONS):
    days = sorted({_et(b['t']).date() for b in bars})
    keep = set(days[-n:])
    return [b for b in bars if _et(b['t']).date() in keep], days[-n:] if days else []

def _round100_up(px):
    """Nearest 100-point strike at or above price (11/25 11:26 ~7018 → 7100)."""
    if px is None:
        return None
    k = int(math.ceil(px / 100.0) * 100)
    if abs(k - px) < 5:
        k = int(round(px / 100.0) * 100)
    return k

def _round5(px):
    return int(round(px / 5.0) * 5)

def _level_label(it):
    side = 'bull' if it['side'] == 'bull' else 'bear'
    tf = {'15m': '15m', '1h': '1h', '4h': '4H', 'day': 'Day', 'week': 'W', 'month': 'M'}.get(it['tf'], it['tf'])
    return f"{tf} {side} disp {fmt_px(it['px'])}"

def _annotate_plus(levels):
    for i, a in enumerate(levels):
        n = 0
        also = []
        for j, b in enumerate(levels):
            if i == j:
                continue
            if abs(a['px'] - b['px']) <= CONFLUENCE_PTS:
                n += 1
                also.append(b['name'])
        a['plus'] = n if n else 0
        a['also'] = also

def _touches_and_sweeps(bars, levels):
    """Session touch-count bubbles and liquidity-sweep / rejection rings.

    Only displacement / King / 4H / σ levels are counted, so dotted 0DTE
    rows do not flood the chart. Sweeps require a real wick through a
    level or a session extreme, then a close back.
    """
    touches = []
    sweeps = []
    if not bars or not levels:
        return touches, sweeps
    by_day = {}
    for b in bars:
        by_day.setdefault(_et(b['t']).date(), []).append(b)

    key_lvs = [lv for lv in levels if not lv.get('dotted') and (lv.get('kind') or '').startswith(('disp', 'king', '4h', 'sigma'))]
    key_px = [lv['px'] for lv in key_lvs]

    for day, db in by_day.items():
        state = {px: 'away' for px in key_px}
        count = {px: 0 for px in key_px}
        sess_sweeps = []

        for i, b in enumerate(db):
            for px in key_px:
                on = (b['l'] - TOUCH_PTS) <= px <= (b['h'] + TOUCH_PTS)
                if on and state[px] == 'away':
                    count[px] += 1
                    state[px] = 'on'
                    if count[px] >= 2:
                        touches.append({
                            't': b['t'],
                            'px': round(px, 2),
                            'n': count[px],
                            'session': day.isoformat(),
                        })
                elif (not on) and state[px] == 'on':
                    if b['h'] < px - 4 or b['l'] > px + 4:
                        state[px] = 'away'

            # significant wick through a key level, close back (extra rings, capped later)
            upper = b['h'] - max(b['o'], b['c'])
            lower = min(b['o'], b['c']) - b['l']
            for px in key_px:
                if lower >= 8 and b['l'] < px - 4 and b['c'] > px:
                    sess_sweeps.append({'t': b['t'], 'px': round(b['l'], 2), 'kind': 'low-level', 'level': px})
                if upper >= 8 and b['h'] > px + 4 and b['c'] < px:
                    sess_sweeps.append({'t': b['t'], 'px': round(b['h'], 2), 'kind': 'high-level', 'level': px})

        if db:
            lo_bar = min(db, key=lambda x: x['l'])
            sess_sweeps.append({'t': lo_bar['t'], 'px': round(lo_bar['l'], 2), 'kind': 'session-low', 'level': lo_bar['l']})
            idx = db.index(lo_bar)
            for j, b in enumerate(db):
                if b is lo_bar:
                    continue
                if abs(j - idx) <= 6 and abs(b['l'] - lo_bar['l']) <= 8:
                    sess_sweeps.append({'t': b['t'], 'px': round(b['l'], 2), 'kind': 'session-low-2', 'level': lo_bar['l']})
                    break
            hi_bar = max(db, key=lambda x: x['h'])
            if hi_bar['h'] - hi_bar['c'] >= 4:
                sess_sweeps.append({'t': hi_bar['t'], 'px': round(hi_bar['h'], 2), 'kind': 'session-high', 'level': hi_bar['h']})

        # Always keep the session-low pair and a rejecting session high.
        # Do not let a nearby level-sweep replace the actual extremes.
        extremes, extra = [], []
        for s in sess_sweeps:
            if str(s['kind']).startswith('session'):
                extremes.append(s)
            else:
                extra.append(s)
        euniq = []
        for s in extremes:
            if any(s['kind'] == u['kind'] and abs(s['px'] - u['px']) <= 2 for u in euniq):
                continue
            euniq.append(s)
        xuniq = []
        for s in extra:
            if any(abs(s['t'] - u['t']) <= 20 * 60 and abs(s['px'] - u['px']) <= 40 for u in euniq + xuniq):
                continue
            xuniq.append(s)
        sweeps.extend(euniq + xuniq[:1])

    # collapse touch bubbles that sit on the same cluster within 25 minutes
    touches.sort(key=lambda x: (x['t'], x['px']))
    collapsed = []
    for tch in touches:
        hit = None
        for u in collapsed:
            if tch.get('session') == u.get('session') and abs(tch['t'] - u['t']) <= 30 * 60 and abs(tch['px'] - u['px']) <= 18:
                hit = u
                break
        if hit is None:
            collapsed.append(dict(tch))
        else:
            if tch['n'] >= hit['n']:
                hit['n'] = tch['n']
                hit['px'] = tch['px']
                hit['t'] = tch['t']
    return collapsed, sweeps


def _wick_upper(b):
    return b['h'] - max(b['o'], b['c'])

def _wick_lower(b):
    return min(b['o'], b['c']) - b['l']


def _signals(bars, ema9, levels, expiry):
    """BUY after a low sweep + EMA9 turn-up + cluster reclaim; EXIT at later resistance tags.

    Tuned so a session like 11/25 fires a call buy at the 11:20 5-minute
    close (11:25 ET; the screenshot labels 11:26, during the next bar)
    at the 7100 call, then EXIT pills as price tags mid-rally and the
    7175–7195 area. One BUY per session. Informational only; never places orders.
    """
    if not bars or not levels:
        return [], []
    solid = [lv for lv in levels if not lv.get('dotted') and lv.get('kind') != 'odte']

    def resistances_above(px):
        return sorted({lv['px'] for lv in solid if lv['px'] > px + 4}, key=lambda p: p)

    def supports_below(px):
        return sorted({lv['px'] for lv in solid if lv['px'] < px - 4}, reverse=True)

    def slope(i):
        if i < 1 or ema9[i] is None or ema9[i - 1] is None:
            return None
        return ema9[i] - ema9[i - 1]

    def is_major(px):
        for lv in solid:
            if abs(lv['px'] - px) <= 1:
                k = lv.get('kind') or ''
                if k.startswith('king') or k.startswith('4h') or k.startswith('sigma') or k == 'disp-bear':
                    return True
                tf = lv.get('tf') or ''
                if tf in ('day', 'week', 'month'):
                    return True
        return False

    signals = []
    dots = []
    by_day = {}
    for i, b in enumerate(bars):
        by_day.setdefault(_et(b['t']).date(), []).append((i, b))

    for day, db in by_day.items():
        swept_low = False
        swept_high = False
        sess_lo = sess_hi = None
        long_pos = None
        short_pos = None
        bought = False
        rejection_exit = False

        for nbar, (i, b) in enumerate(db):
            if sess_lo is None or b['l'] < sess_lo:
                sess_lo = b['l']
            if sess_hi is None or b['h'] > sess_hi:
                sess_hi = b['h']

            # A real sweep needs a wick through a level / prior swing, then a close back.
            # An opening bar that simply falls through a level is not a high-side sweep.
            if _wick_lower(b) >= 3:
                for lv in solid:
                    if b['l'] < lv['px'] - SWEEP_PTS and b['c'] > lv['px'] - 1:
                        swept_low = True
                prior = [x for j, x in db if j < i][-12:]
                if prior:
                    plo = min(x['l'] for x in prior)
                    if b['l'] < plo - SWEEP_PTS and b['c'] > plo:
                        swept_low = True
            if _wick_upper(b) >= 3:
                for lv in solid:
                    if b['h'] > lv['px'] + SWEEP_PTS and b['c'] < lv['px'] + 1:
                        swept_high = True
                prior = [x for j, x in db if j < i][-12:]
                if prior:
                    phi = max(x['h'] for x in prior)
                    if b['h'] > phi + SWEEP_PTS and b['c'] < phi:
                        swept_high = True
            if b['l'] == sess_lo and b['c'] > b['l'] + 8 and _wick_lower(b) >= 3:
                swept_low = True

            sl = slope(i)
            sl_prev = slope(i - 1) if i else None
            slope_up = sl is not None and sl_prev is not None and sl_prev <= 0 and sl > 1.0
            slope_dn = sl is not None and sl_prev is not None and sl_prev >= 0 and sl < -1.0

            if slope_up and ema9[i] is not None and b['c'] >= ema9[i] - 2:
                dots.append({'t': b['t'], 'px': round(min(b['c'], ema9[i]), 2)})

            # BUY CALL — one per session, after a low sweep, EMA9 turns up, close through the cluster
            if (not bought) and long_pos is None and swept_low and slope_up:
                cluster = [lv for lv in solid if lv['px'] <= b['c'] + 5 and lv['px'] >= b['c'] - 40]
                if cluster or any(lv['px'] <= b['c'] and abs(lv['px'] - b['c']) <= 25 for lv in solid):
                    strike = _round100_up(b['c'])
                    t_close = b['t'] + 5 * 60
                    long_pos = {
                        'entry': b['c'], 'entry_t': t_close, 'stop': (sess_lo - 2) if sess_lo is not None else b['l'] - 2,
                        'used_res': set(), 'strike': strike, 'cp': 'C',
                    }
                    bought = True
                    signals.append({
                        't': t_close,
                        't_bar': b['t'],
                        'side': 'BUY',
                        'cp': 'C',
                        'px': round(b['c'], 2),
                        'strike': strike,
                        'label': f"{_et(t_close).strftime('%-H:%M')} · SPX ${strike}C 0DTE",
                        'banner': f"BUY NOW — SPX ${strike}C 0DTE",
                        'reason': '回扫低点后，5分钟 EMA9 拐头向上，价格重新站上关键位簇',
                        'session': day.isoformat(),
                    })

            # BUY PUT — symmetric. After a session-low liquidity sweep the bias is
            # long (11/25: 09:45 low sweep, so the 10:40 dip does not become a put
            # and the 11:25 call is allowed to fire). Skip the opening drive.
            et = _et(b['t'])
            opening = et.hour == 9 and et.minute < 45
            if (not bought) and short_pos is None and swept_high and slope_dn and not opening and not swept_low:
                if any(abs(lv['px'] - b['c']) <= 25 for lv in solid):
                    strike = int(math.floor(b['c'] / 100.0) * 100)
                    t_close = b['t'] + 5 * 60
                    short_pos = {
                        'entry': b['c'], 'entry_t': t_close, 'stop': (sess_hi + 2) if sess_hi is not None else b['h'] + 2,
                        'used_res': set(), 'strike': strike, 'cp': 'P',
                    }
                    bought = True
                    signals.append({
                        't': t_close,
                        't_bar': b['t'],
                        'side': 'BUY',
                        'cp': 'P',
                        'px': round(b['c'], 2),
                        'strike': strike,
                        'label': f"{_et(t_close).strftime('%-H:%M')} · SPX ${strike}P 0DTE",
                        'banner': f"BUY NOW — SPX ${strike}P 0DTE",
                        'reason': '回扫高点后，5分钟 EMA9 拐头向下，价格重新跌破关键位簇',
                        'session': day.isoformat(),
                    })

            # EXIT after a long: first tag of a major resistance ≥ 80 pts above entry
            # (11/25: skip 7,096 which is only +78, take 7,157 σ then the 7,187–7,194 cap).
            # Levels skipped by debounce are not marked used, so a slightly farther
            # cap level can still fire.
            if long_pos is not None:
                above = [px for px in resistances_above(long_pos['entry']) if px not in long_pos['used_res']]
                for px in above:
                    if b['h'] < px - 1:
                        continue
                    if px < long_pos['entry'] + 80:
                        continue
                    if not is_major(px):
                        continue
                    if any(abs(px - u) < 30 for u in long_pos['used_res']):
                        continue
                    long_pos['used_res'].add(px)
                    signals.append({
                        't': b['t'] + 5 * 60,
                        't_bar': b['t'],
                        'side': 'EXIT',
                        'cp': 'C',
                        'px': round(min(b['h'], px), 2),
                        'level': px,
                        'label': 'EXIT',
                        'reason': f'触及阻力 {fmt_px(px)}',
                        'session': day.isoformat(),
                    })
                    break
                if b['c'] < long_pos['stop']:
                    signals.append({
                        't': b['t'] + 5 * 60,
                        't_bar': b['t'],
                        'side': 'EXIT',
                        'cp': 'C',
                        'px': round(b['c'], 2),
                        'label': 'EXIT',
                        'reason': '收盘跌破回扫低点，多单结束',
                        'session': day.isoformat(),
                    })
                    long_pos = None

            if short_pos is not None:
                below = [px for px in supports_below(short_pos['entry']) if px not in short_pos['used_res']]
                for px in below:
                    if b['l'] > px + 1:
                        continue
                    if px > short_pos['entry'] - 50:
                        short_pos['used_res'].add(px)
                        continue
                    if any(abs(px - u) < 30 for u in short_pos['used_res']):
                        short_pos['used_res'].add(px)
                        continue
                    short_pos['used_res'].add(px)
                    signals.append({
                        't': b['t'] + 5 * 60,
                        't_bar': b['t'],
                        'side': 'EXIT',
                        'cp': 'P',
                        'px': round(max(b['l'], px), 2),
                        'level': px,
                        'label': 'EXIT',
                        'reason': f'触及支撑 {fmt_px(px)}',
                        'session': day.isoformat(),
                    })
                    break
                if b['c'] > short_pos['stop']:
                    signals.append({
                        't': b['t'] + 5 * 60,
                        't_bar': b['t'],
                        'side': 'EXIT',
                        'cp': 'P',
                        'px': round(b['c'], 2),
                        'label': 'EXIT',
                        'reason': '收盘升破回扫高点，空单结束',
                        'session': day.isoformat(),
                    })
                    short_pos = None

            # One standalone resistance-rejection EXIT before a formal buy (11/25 10:00 at 7,059)
            if long_pos is None and not rejection_exit and sess_lo is not None:
                bounce = (b['h'] - sess_lo) if sess_lo is not None else 0
                if bounce >= 40:
                    for lv in solid:
                        px = lv['px']
                        if b['h'] >= px - 1 and b['h'] <= px + 12 and b['c'] < px and (px - b['c']) >= 3 and (b['h'] - b['c']) >= 2:
                            prior = [x for j, x in db if j < i]
                            if prior and min(x['l'] for x in prior) <= sess_lo + 2:
                                rejection_exit = True
                                signals.append({
                                    't': b['t'] + 5 * 60,
                                    't_bar': b['t'],
                                    'side': 'EXIT',
                                    'cp': 'C',
                                    'px': round(min(b['h'], px), 2),
                                    'level': px,
                                    'label': 'EXIT',
                                    'reason': f'阻力拒绝 {fmt_px(px)}',
                                    'session': day.isoformat(),
                                })
                                break

    dots.sort(key=lambda d: d['t'])
    udots = []
    for d in dots:
        if udots and d['t'] - udots[-1]['t'] < 20 * 60:
            continue
        udots.append(d)
    return signals, udots

RULE = (
    '怎么算的（仅供参考，不会下单；定义按参考图 11/25 的可见元素反推，不用编造的价格）。'
    '位移位：该周期实体 ≥ k 倍 ATR(14) 的已完成K线，取实体中点 (开盘+收盘)/2，'
    '不是开盘价，也不只保留最近一根。15 分钟 k=1.0，1 小时/日/周/月 k=0.80'
    '（11/25 开盘那根 1 小时空头位移的 k 只有 0.84）。同一周期、8 点以内的位移位合并，保留更早的那条；'
    '每个周期最多保留最近 2 条多头、2–3 条空头。再和更短周期已经标出的位移位相比，11 点以内的重复中点会去掉。'
    '标签上的 +N 是在 12 点以内与这条位共振的其他周期位移位、King、4H、σ 或期权行权价的条数。'
    'Day + Week King：用 5 分钟K线的典型价格 (H+L+C)/3 做时间分布（每根K线给所在 5 点价格箱 +1），'
    '取当天和当周出现次数最多的箱，用箱的下沿作为整数位（11/25 是 7,175，不是均价 (H+L+C)/3，也不是成交量 POC；'
    '当天成交量 POC 会落在早盘低点附近，和图上中枢不一致）。当和周的箱在 12 点以内视为重合，只画一条 Day + Week King；否则分开画。'
    '4H：4 小时收盘价的 EMA8，取上一根已完成 4 小时K线的值（11/25 算出来是 7,194.55，和图上 7,195.13 差 0.58 点，按实值画，不改数）。'
    'σ：最近 3 个小时的成交量加权 VWAP ± 1σ（11/25 是 13:00–16:00，−1σ = 7,157.69，对应图上 7,157 σ；'
    '全天 VWAP+1σ 是 7,164，对不上）。标签上的 +1 是共振条数，不是 +1σ 的意思。'
    '图上黄线是 5 分钟 EMA9，不是全天 VWAP。绿点是 EMA9 拐头向上或价格重新站上 EMA9。'
    '黄圈：K线影线刺破关键位或前低/前高后，收盘收回来，视为流动性回扫/拒绝；开盘双底低点会画两个圈。'
    '黑色 +N 气泡：当天价格离开后再回来触及同一条位的次数（第 2 次起标）。和标签上的 +N（周期共振）不是一回事。'
    '虚线：富途 SPXW 近月到期、未平仓量最大的几个看涨/看跌行权价（从这台电脑推送，因为云端连不上富途）。'
    'BUY NOW：当天已经出现过低点回扫，5 分钟 EMA9 由平/下拐头向上（斜率转正且大于 1 点），'
    '同时收盘重新站上附近的关键位簇，就提示买最接近的上一个整百点看涨 0DTE（11/25 约 7,018 对应 7100C）。'
    '信号时间取触发那根 5 分钟K线的收盘（11/25 是 11:25 美东，图上标在 11:26，落在下一根K线里）。'
    '看跌信号对称。进场后的浅回撤只要不跌破回扫低点就继续拿。'
    'EXIT：多单触及上方下一档关键位（15 点以内的位合并成一次），或者收盘跌破回扫低点；'
    '早盘没有正式买单、但第一波反弹在阻力位被拒绝时，也会标一个 EXIT（11/25 10:00 附近的 7,059）。'
)

def payload():
    with _lk:
        e = _c.get('payload')
        if e and time.time() - e[0] < 15:
            return e[1]

    out = {'ok': True, 'symbol': '.SPX.US', 'sessions_n': SESSIONS}
    last = prev = None
    try:
        q = quote()
        last = _f(q.get('last') or q.get('last_done') or q.get('close'))
        prev = _f(q.get('prev_close') or q.get('previous_close'))
        out['quote'] = {'last': last, 'prev_close': prev}
    except Exception as e:
        out['quote_error'] = str(e)[:200]

    try:
        raw5 = kline('5m', 400)
    except Exception as e:
        out['error'] = 'kline 5m: ' + str(e)[:200]
        return out

    bars, sessions = _visible_sessions(raw5, SESSIONS)
    out['sessions'] = [d.isoformat() for d in sessions]
    if not bars:
        out['error'] = 'no 5m bars'
        return out

    e9 = ema_series([b['c'] for b in bars], EMA_FAST)
    e20 = ema_series([b['c'] for b in bars], EMA_SLOW)
    out['bars'] = []
    for i, b in enumerate(bars):
        rec = dict(b)
        rec['ema9'] = None if e9[i] is None else round(e9[i], 2)
        rec['ema20'] = None if e20[i] is None else round(e20[i], 2)
        out['bars'].append(rec)

    # last / change: prefer the live quote, fall back to last 5m close
    if last is None:
        last = bars[-1]['c']
    out['last'] = round(last, 2)
    if prev is None:
        # previous session close = last bar of the prior visible day, else first day open
        days = sessions
        if len(days) >= 2:
            prior = [b for b in bars if _et(b['t']).date() == days[-2]]
            if prior:
                prev = prior[-1]['c']
        if prev is None:
            first = [b for b in bars if _et(b['t']).date() == days[-1]]
            prev = first[0]['o'] if first else None
    out['prev_close'] = None if prev is None else round(prev, 2)
    if last is not None and prev:
        out['chg'] = round(last - prev, 2)
        out['chg_pct'] = round((last - prev) / prev * 100.0, 2)
    last_day = sessions[-1]
    day_bars_all = [b for b in raw5 if _et(b['t']).date() == last_day]
    if day_bars_all:
        out['day_high'] = round(max(b['h'] for b in day_bars_all), 2)
        out['day_low'] = round(min(b['l'] for b in day_bars_all), 2)
        out['session'] = last_day.isoformat()

    # --- displacements across TFs ---
    disp_kept = []
    disp_raw = {}
    lower_tf_mids = []  # for near-TF de-dup, processed 15m → 1h → day → week → month
    order = ['15m', '1h', 'day', 'week', 'month']
    for period, lb_period, count, _label in TFS:
        if period not in PLOT_DISP_TF and period != '4h':
            continue
        try:
            hb = kline(lb_period, count)
        except Exception as e:
            out.setdefault('tf_errors', {})[period] = str(e)[:160]
            continue
        found = displacements(hb, period, K_MIN.get(period, 0.8))
        merged = _merge_same_tf(found)
        # keep those in the window around spot (always keep if no spot)
        if last is not None:
            merged = [x for x in merged if abs(x['px'] - last) <= WINDOW_PTS]
        cap = MAX_KEEP.get(period, (2, 2))
        capped = _cap_recent(merged, cap[0], cap[1])
        if period in PLOT_DISP_TF:
            pruned = []
            for it in capped:
                if any(abs(it['px'] - m) <= NEAR_TF_PTS for m in lower_tf_mids):
                    continue
                pruned.append(it)
                lower_tf_mids.append(it['px'])
            capped = pruned
        disp_raw[period] = capped
        if period in PLOT_DISP_TF:
            disp_kept.extend(capped)

    levels = []
    for it in disp_kept:
        levels.append({
            'px': it['px'],
            'name': _level_label(it),
            'color': BULL_COLOR if it['side'] == 'bull' else BEAR_COLOR,
            'kind': 'disp-' + it['side'],
            'side': it['side'],
            'tf': it['tf'],
            't': it['t'],
            'k': it.get('k'),
            'priority': {'15m': 3, '1h': 3, 'day': 4, 'week': 4, 'month': 4}.get(it['tf'], 5),
            'dotted': False,
        })

    # --- Day + Week King (typical-price TPO) ---
    king = None
    try:
        # use the full 5m history we already loaded (covers the current week on 11/25)
        day5 = [b for b in raw5 if _et(b['t']).date() == last_day]
        week5 = [b for b in raw5 if week_start(_et(b['t']).date()) == week_start(last_day)]
        day_poc = tpo_poc(day5, 5) if day5 else None
        week_poc = tpo_poc(week5, 5) if week5 else None
        king = {
            'day': day_poc,
            'week': week_poc,
            'coincident': False,
            'px': None,
        }
        if day_poc and week_poc:
            if abs(day_poc['bin_start'] - week_poc['bin_start']) <= CONFLUENCE_PTS:
                king['coincident'] = True
                king['px'] = int(day_poc['bin_start'])
                levels.append({
                    'px': float(king['px']),
                    'name': f"Day + Week King {fmt_px(king['px'], integers=True)}",
                    'color': KING_COLOR,
                    'kind': 'king',
                    'priority': 1,
                    'dotted': False,
                })
            else:
                king['px_day'] = int(day_poc['bin_start'])
                king['px_week'] = int(week_poc['bin_start'])
                levels.append({
                    'px': float(king['px_day']),
                    'name': f"Day King {fmt_px(king['px_day'], integers=True)}",
                    'color': KING_COLOR,
                    'kind': 'king-day',
                    'priority': 1,
                    'dotted': False,
                })
                levels.append({
                    'px': float(king['px_week']),
                    'name': f"Week King {fmt_px(king['px_week'], integers=True)}",
                    'color': KING_COLOR,
                    'kind': 'king-week',
                    'priority': 1,
                    'dotted': False,
                })
        elif day_poc:
            king['px'] = int(day_poc['bin_start'])
            levels.append({
                'px': float(king['px']),
                'name': f"Day King {fmt_px(king['px'], integers=True)}",
                'color': KING_COLOR,
                'kind': 'king-day',
                'priority': 1,
                'dotted': False,
            })
        out['king'] = king
    except Exception as e:
        out['king_error'] = str(e)[:200]

    # --- 4H EMA8 ---
    four = None
    try:
        b4 = kline('4h', 200)
        four = four_h_ema(b4, 8)
        if four and four.get('px') is not None:
            levels.append({
                'px': four['px'],
                'name': f"4H {fmt_px(four['px'])}",
                'color': FOURH_COLOR,
                'kind': '4h',
                'priority': 2,
                'dotted': False,
            })
        out['four_h'] = four
    except Exception as e:
        out['four_h_error'] = str(e)[:200]

    # --- σ : last 3h VWAP ± 1σ ---
    try:
        out['vwap'] = session_vwap_sigma(raw5)
        sig = last_hours_vwap_sigma(raw5, 3)
        out['sigma'] = sig
        if sig:
            # plot both bands; the one that matched the image on 11/25 is the lower
            for key, tag in (('lower', 'σ'), ('upper', 'σ')):
                px = sig.get(key)
                if px is None:
                    continue
                # display as integer when within 0.8 of an integer (7157.69 → 7157)
                shown = int(round(px)) if abs(px - round(px)) <= 0.8 or abs(px - int(px)) <= 0.8 else px
                # 7157.69: abs(7157.69-7158)=0.31 → would become 7158. Prefer floor if .5-.8? 
                # 7157.69 is closer to 7158 than 7157. The screenshot says 7157.
                # Use int() truncation toward the VWAP (inner band) so −1σ 7157.69 → 7157, +1σ 7189.36 → 7189.
                if key == 'lower':
                    shown = int(math.floor(px + 1e-9)) if abs(px - math.floor(px)) < 0.85 else int(round(px))
                    # 7157.69 → floor = 7157. Good.
                else:
                    shown = int(math.ceil(px - 1e-9)) if abs(math.ceil(px) - px) < 0.85 else int(round(px))
                levels.append({
                    'px': float(shown),
                    'raw_px': px,
                    'name': f"{shown} σ",
                    'color': SIGMA_COLOR,
                    'kind': 'sigma-' + key,
                    'priority': 2,
                    'dotted': False,
                })
    except Exception as e:
        out['sigma_error'] = str(e)[:200]

    # --- Futu 0DTE / near-expiry strikes ---
    futu = _load_futu_snap()
    odte = []
    if futu:
        strikes = []
        for r in (futu.get('rows') or futu.get('strikes') or []):
            k = _f(r.get('k') if isinstance(r, dict) else None)
            if k is None and isinstance(r, dict):
                k = _f(r.get('strike') or r.get('K'))
            if k is None:
                continue
            oi_c = int(r.get('oi_c') or r.get('call_oi') or 0)
            oi_p = int(r.get('oi_p') or r.get('put_oi') or 0)
            if oi_c + oi_p <= 0:
                continue
            side = 'call' if (last is not None and k >= last) or (last is None and oi_c >= oi_p) else 'put'
            strikes.append({'k': k, 'oi_c': oi_c, 'oi_p': oi_p, 'gex_oi': r.get('gex_oi'), 'side': side})
        if last:
            above = sorted([s for s in strikes if s['k'] >= last], key=lambda s: -s['oi_c'])[:4]
            below = sorted([s for s in strikes if s['k'] <= last], key=lambda s: -s['oi_p'])[:4]
            shown = {s['k']: s for s in above + below}
        else:
            shown = {s['k']: s for s in sorted(strikes, key=lambda s: -(s['oi_c'] + s['oi_p']))[:8]}
        for wall, name in ((futu.get('call_wall'), 'call wall'), (futu.get('put_wall'), 'put wall')):
            w = _f(wall)
            if w is not None:
                shown.setdefault(w, {'k': w, 'oi_c': 0, 'oi_p': 0, 'side': 'call' if name.startswith('c') else 'put'})
                shown[w]['wall'] = name
        odte = sorted(shown.values(), key=lambda s: s['k'])
        out['futu'] = {
            'expiry': futu.get('expiry'),
            'pushed_bj': futu.get('pushed_bj'),
            'zero_gamma': futu.get('zero_gamma'),
            'call_wall': futu.get('call_wall'),
            'put_wall': futu.get('put_wall'),
            'straddle': futu.get('straddle'),
            'one_sigma_move': futu.get('one_sigma_move') or futu.get('straddle_move'),
            'strikes': odte,
            'source': 'futu',
        }
        exp = futu.get('expiry') or ''
        try:
            expl = datetime.strptime(str(exp)[:10], '%Y-%m-%d').strftime('%-m/%-d') if exp else '0DTE'
        except Exception:
            expl = str(exp)[:10] or '0DTE'
        for s in odte:
            tag = 'C' if s['side'] == 'call' else 'P'
            extra = ' wall' if s.get('wall') else ''
            levels.append({
                'px': s['k'],
                'name': ('%s %s%s%s' % (expl, int(s['k']) if s['k'] == int(s['k']) else s['k'], tag, extra)).strip(),
                'color': ODTE_COLOR,
                'kind': 'odte',
                'priority': 8,
                'dotted': True,
            })
    else:
        out['futu'] = None

    _annotate_plus(levels)

    # Off-screen is relative to the last session (the default zoom), not the full 3-day history.
    sess_bars = [b for b in bars if _et(b['t']).date() == last_day] or bars
    lo = min((b['l'] for b in sess_bars), default=None)
    hi = max((b['h'] for b in sess_bars), default=None)
    off_top, off_bot = [], []
    for g in levels:
        if last is not None:
            g['dist'] = round(g['px'] - last, 2)
        g['offscreen'] = bool(lo is not None and hi is not None and (g['px'] < lo or g['px'] > hi))
        if g['offscreen']:
            rec = {'name': g['name'], 'px': g['px'], 'dist': g.get('dist'), 'color': g['color'],
                   'kind': g['kind'], 'plus': g.get('plus') or 0, 'dotted': g.get('dotted')}
            if g['px'] > (hi or 0):
                off_top.append(rec)
            else:
                off_bot.append(rec)
    def _pick_off(arr, n=2):
        """Prefer the nearest week and month displacements, then the closest other solids."""
        solid = [x for x in arr if not x.get('dotted')]
        dotted = [x for x in arr if x.get('dotted')]
        solid.sort(key=lambda x: abs(x.get('dist') or 0))
        vis = []
        for prefix in ('W ', 'M '):
            for x in solid:
                if (x.get('name') or '').startswith(prefix) and x not in vis:
                    vis.append(x)
                    break
        for x in solid:
            if len(vis) >= n:
                break
            if x not in vis:
                vis.append(x)
        vis = vis[:n]
        more = [x for x in solid + dotted if x not in vis]
        return vis, more
    top_vis, top_more = _pick_off(off_top, 2)
    bot_vis, bot_more = _pick_off(off_bot, 2)
    # call / put wall arrows
    extra_bot = []
    extra_top = []
    if futu and last is not None:
        cw, pw = _f(futu.get('call_wall')), _f(futu.get('put_wall'))
        if cw is not None and (hi is None or cw > hi):
            extra_top.append({'name': 'calls ↑', 'px': cw, 'dist': round(cw - last, 2), 'color': ODTE_COLOR, 'kind': 'odte-wall'})
        if pw is not None and (lo is None or pw < lo):
            extra_bot.append({'name': 'puts ↑', 'px': pw, 'dist': round(pw - last, 2), 'color': ODTE_COLOR, 'kind': 'odte-wall'})
        # if put wall is on-screen, skip; call wall off-screen already in extra_top
        # screenshot also has "calls ↑ 216 pts" at the bottom when the wall is above — show both
        if cw is not None and (hi is None or cw > hi):
            extra_bot.append({'name': 'calls ↑', 'px': cw, 'dist': round(cw - last, 2), 'color': ODTE_COLOR, 'kind': 'odte-wall'})
    out['offscreen_top'] = top_vis
    out['offscreen_top_more'] = len(top_more)
    out['offscreen_bottom'] = bot_vis
    out['offscreen_bottom_more'] = len(bot_more)
    out['offscreen_extra_top'] = extra_top
    out['offscreen_extra_bottom'] = extra_bot

    # only list/plot solid + dotted on-screen in the main list; off-screen go to the header/footer
    out['levels'] = levels
    out['disp'] = {tf: disp_raw.get(tf, []) for tf in PLOT_DISP_TF}

    expiry = (out.get('futu') or {}).get('expiry')
    try:
        touches, sweeps = _touches_and_sweeps(bars, [lv for lv in levels if not lv.get('dotted')])
        out['touches'] = touches
        out['sweeps'] = sweeps
    except Exception as e:
        out['touches'] = []
        out['sweeps'] = []
        out['overlay_error'] = str(e)[:200]

    try:
        sigs, dots = _signals(bars, e9, levels, expiry)
        out['signals'] = sigs
        out['dots'] = dots
    except Exception as e:
        out['signals'] = []
        out['dots'] = []
        out['signals_error'] = str(e)[:200]

    out['server_time'] = int(time.time())
    out['server_time_bj'] = datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')
    out['rule'] = RULE
    with _lk:
        _c['payload'] = (time.time(), out)
    return out

if __name__ == '__main__':
    d = payload()
    print('last', d.get('last'), d.get('chg_pct'), 'session', d.get('session'))
    print('king', d.get('king'))
    print('4h', d.get('four_h'))
    print('sigma', d.get('sigma'))
    print('vwap', d.get('vwap'))
    print('--- levels ---')
    for g in d.get('levels') or []:
        if g.get('dotted'):
            continue
        flag = ' OFF' if g.get('offscreen') else ''
        plus = f" +{g['plus']}" if g.get('plus') else ''
        print(f"  {g['name']}{plus}  {g['px']}{flag}")
    print('--- signals ---')
    for s in d.get('signals') or []:
        print(' ', s.get('session'), s['side'], s.get('label'), s.get('px'), s.get('reason'))
    print('sweeps', len(d.get('sweeps') or []), 'touches', len(d.get('touches') or []), 'dots', len(d.get('dots') or []))
    if d.get('signals_error'):
        print('signals_error', d['signals_error'])
    if d.get('error'):
        print('ERROR', d['error'])
