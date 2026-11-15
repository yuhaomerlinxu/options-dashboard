"""mBOT-style 5-minute SPX/QQQ page. Display-only signals, independent of 新规则 v3.

Market data via lbsafe only (read-only). Does not change /qqq or /echo.
"""
import math, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo

import echoapi as E

ET = ZoneInfo('America/New_York')

# Supertrend defaults on 5-minute bars
ATR_N = 10
ATR_MULT = 3.0
PEND_ATR = 0.25          # how close a test must come to the trail
PEND_COOL = 8            # min bars between PEND marks
SHIFT_COOL = 4           # min bars between regime shifts
RANGE_LOOK = 16          # flips in this window => rangebound
RANGE_NEAR = 0.45        # |close-stop|/ATR
RANGE_NEAR_N = 5         # of last 8 bars


def _bars(symbol, days=5, session='intraday'):
    raw = E._run(['kline', symbol, '--period', '5m', '--count', '800', '--session', session], 45)
    out = []
    for b in raw or []:
        t = E._ts(b['time'])
        if session == 'intraday' and not E._rth(t):
            continue
        o, h, l, c = float(b['open']), float(b['high']), float(b['low']), float(b['close'])
        v = float(b.get('volume') or 0)
        out.append({'t': t, 'o': o, 'h': h, 'l': l, 'c': c, 'v': v})
    days_seen = []
    for b in out:
        d = str(E._et(b['t']).date())
        if d not in days_seen:
            days_seen.append(d)
    keep = set(days_seen[-days:])
    return [b for b in out if str(E._et(b['t']).date()) in keep]


def _resample(bars, sec):
    out, cur = [], None
    for b in bars:
        bucket = int(b['t'] - (b['t'] % sec))
        if not cur or cur['t'] != bucket:
            if cur:
                out.append(cur)
            cur = {'t': bucket, 'o': b['o'], 'h': b['h'], 'l': b['l'], 'c': b['c'], 'v': b['v']}
        else:
            cur['h'] = max(cur['h'], b['h'])
            cur['l'] = min(cur['l'], b['l'])
            cur['c'] = b['c']
            cur['v'] += b['v']
    if cur:
        out.append(cur)
    return out


def _atr(bars, n=ATR_N):
    out = [None] * len(bars)
    if not bars:
        return out
    tr = []
    for i, b in enumerate(bars):
        if i == 0:
            tr.append(b['h'] - b['l'])
        else:
            pc = bars[i - 1]['c']
            tr.append(max(b['h'] - b['l'], abs(b['h'] - pc), abs(b['l'] - pc)))
    if len(tr) < n:
        return out
    a = sum(tr[:n]) / n
    out[n - 1] = a
    for i in range(n, len(tr)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
    return out


def _supertrend(bars, n=ATR_N, mult=ATR_MULT):
    """Classic Supertrend. Returns (stop, trend) where trend is +1 up / -1 down / 0 warmup."""
    atr = _atr(bars, n)
    stop = [None] * len(bars)
    trend = [0] * len(bars)
    fu = fl = None
    td = 1
    for i, b in enumerate(bars):
        if atr[i] is None:
            continue
        mid = (b['h'] + b['l']) / 2
        bu = mid + mult * atr[i]
        bl = mid - mult * atr[i]
        if fu is None:
            fu, fl, td = bu, bl, 1
        else:
            fu = bu if (bu < fu or bars[i - 1]['c'] > fu) else fu
            fl = bl if (bl > fl or bars[i - 1]['c'] < fl) else fl
            if td == 1 and b['c'] < fl:
                td = -1
                fu = bu
            elif td == -1 and b['c'] > fu:
                td = 1
                fl = bl
        trend[i] = td
        stop[i] = round(fl if td == 1 else fu, 4)
    return stop, trend, atr


def _regime(bars, trend, stop, atr):
    """up / down / range per bar. Range if the trail is chopped or price hugs the stop."""
    out = []
    for i in range(len(bars)):
        if trend[i] == 0 or atr[i] is None:
            out.append('range')
            continue
        lo = max(0, i - RANGE_LOOK + 1)
        flips = sum(1 for j in range(lo + 1, i + 1) if trend[j] and trend[j - 1] and trend[j] != trend[j - 1])
        near = 0
        for j in range(max(0, i - 7), i + 1):
            if atr[j] and stop[j] is not None and atr[j] > 0:
                if abs(bars[j]['c'] - stop[j]) / atr[j] < RANGE_NEAR:
                    near += 1
        if flips >= 2 or near >= RANGE_NEAR_N:
            out.append('range')
        else:
            out.append('up' if trend[i] == 1 else 'down')
    return out


def _signals(bars, trend, stop, atr, regime):
    marks = []
    last_shift = -999
    last_pend = -999
    last_9 = {'up': -999, 'down': -999}

    for i, b in enumerate(bars):
        if trend[i] == 0 or atr[i] is None or stop[i] is None:
            continue
        prev = trend[i - 1] if i else 0
        # Regime shift: Supertrend flip, with a short cooldown so we don't stutter.
        if prev and trend[i] != prev and (i - last_shift) >= SHIFT_COOL:
            if trend[i] == 1:
                marks.append({'t': b['t'], 'kind': 'LONG Shift', 'dir': 'long', 'price': round(b['c'], 2),
                              'pos': 'below', 'color': '#2ee6d6'})
            else:
                marks.append({'t': b['t'], 'kind': 'SHORT Shift', 'dir': 'short', 'price': round(b['c'], 2),
                              'pos': 'above', 'color': '#e14ec4'})
            last_shift = i

        # PEND: price tests the trailing stop but the close stays on the trend side.
        tested = False
        if trend[i] == 1:
            tested = b['l'] <= stop[i] + PEND_ATR * atr[i] and b['c'] >= stop[i]
        elif trend[i] == -1:
            tested = b['h'] >= stop[i] - PEND_ATR * atr[i] and b['c'] <= stop[i]
        if tested and i != last_shift and (i - last_pend) >= PEND_COOL and (i - last_shift) >= 3:
            marks.append({'t': b['t'], 'kind': 'PEND', 'dir': 'pend', 'price': round(b['c'], 2),
                          'pos': 'below' if trend[i] == 1 else 'above', 'color': '#f0c14b'})
            last_pend = i

        # 9on10: 9 of the last 10 bars close in the trend direction (close vs previous close).
        if i >= 9:
            window = bars[i - 9:i + 1]
            if trend[i] == 1:
                n = sum(1 for j in range(1, 10) if window[j]['c'] > window[j - 1]['c'])
                # first bar of window vs its previous is already in the 9 comparisons from bars[i-9]..[i]
                n = sum(1 for k in range(i - 9, i + 1) if bars[k]['c'] > bars[k - 1]['c'])
                if n >= 9 and last_9['up'] < last_shift:
                    marks.append({'t': b['t'], 'kind': 'LONG 9on10', 'dir': 'long9', 'price': round(b['c'], 2),
                                  'pos': 'below', 'color': '#f0c14b'})
                    last_9['up'] = i
            elif trend[i] == -1:
                n = sum(1 for k in range(i - 9, i + 1) if bars[k]['c'] < bars[k - 1]['c'])
                if n >= 9 and last_9['down'] < last_shift:
                    marks.append({'t': b['t'], 'kind': 'SHORT 9on10', 'dir': 'short9', 'price': round(b['c'], 2),
                                  'pos': 'above', 'color': '#f0c14b'})
                    last_9['down'] = i
    return marks


def _delta(price_bars, vol_bars):
    """Intrabar volume split: up = vol*(close-low)/range, down = vol*(high-close)/range.
    Yellow (divergence) when the sign of delta disagrees with close vs previous close.
    """
    vol_map = {b['t']: b for b in vol_bars}
    out = []
    prev_c = None
    for b in price_bars:
        src = vol_map.get(b['t'], b)
        v = float(src.get('v') or 0)
        rng = src['h'] - src['l']
        if rng > 0:
            up = v * (src['c'] - src['l']) / rng
            dn = v * (src['h'] - src['c']) / rng
            dlt = up - dn
        else:
            dlt = 0.0
        px_dir = 0 if prev_c is None else (1 if b['c'] > prev_c else -1 if b['c'] < prev_c else 0)
        d_dir = 1 if dlt > 0 else -1 if dlt < 0 else 0
        div = bool(px_dir and d_dir and px_dir != d_dir)
        out.append({'t': b['t'], 'v': round(dlt, 1), 'div': div})
        prev_c = b['c']
    return out


def _exp_move(spec, spot):
    under = 'SPY.US' if spec.get('scale') == 'spy' else 'QQQ.US'
    q_under = E._quote(under)
    uspot = q_under['last']
    scale = (float(spot) / uspot) if spec.get('scale') == 'spy' else 1.0
    today = datetime.now(ET).date().isoformat()
    exps = [e['expiry_date'] for e in E._run(['option', 'chain', under], 20)]
    exps = [e for e in exps if e >= today]
    if not exps:
        return {'ok': False, 'error': 'no expiry'}
    e0 = exps[0]
    ch = E._run(['option', 'chain', under, '--date', e0], 20)
    ks = sorted({float(r['strike']) for r in ch if r.get('standard', 'true') == 'true'},
                key=lambda k: abs(k - uspot))
    if not ks:
        return {'ok': False, 'error': 'no strikes'}
    k0 = ks[0]
    recs = E._run(['option', 'quote', E._occ(under, e0, 'C', k0), E._occ(under, e0, 'P', k0)], 20) or []
    ivs = [float(x['implied_volatility']) for x in recs if float(x.get('implied_volatility') or 0) > 0]
    if not ivs:
        return {'ok': False, 'error': 'no iv', 'exp': e0, 'strike': k0}
    iv = sum(ivs) / len(ivs)
    # Remaining sessions until expiry, at least one regular session.
    close = datetime.fromisoformat(e0 + 'T16:00:00').replace(tzinfo=ET)
    days = max((close - datetime.now(ET)).total_seconds() / 86400, 0.4)
    # Use trading-day convention for a short-dated expected move.
    move = float(spot) * iv * math.sqrt(max(days, 1) / 365.0 * 365 / 252.0)
    # simpler: spot * iv * sqrt(max(dte_trading, 1)/252)
    dte_td = max(int(round((close.date() - datetime.now(ET).date()).days * 5 / 7)) or 1, 1)
    move = float(spot) * iv * math.sqrt(dte_td / 252.0)
    return {
        'ok': True,
        'under': under,
        'exp': e0,
        'strike': k0 if spec.get('scale') != 'spy' else round(k0 * scale, 2),
        'iv': round(iv, 4),
        'dte': dte_td,
        'move': round(move, 2),
        'label': '%s %.0f ±%.0f' % (spec['label'], float(spot), move),
        'source': ('SPY ATM IV × SPX/SPY' if spec.get('scale') == 'spy' else 'QQQ ATM IV') + ' · %s · IV %.1f%%' % (e0, iv * 100),
    }


def _vix():
    q = E._run(['quote', '.VIX.US'], 12)[0]
    last = float(q['last'])
    prev = float(q.get('prev_close') or last)
    bars = []
    try:
        raw = E._run(['kline', '.VIX.US', '--period', '1h', '--count', '20'], 15) or []
        bars = [{'t': E._ts(b['time']), 'c': float(b['close'])} for b in raw]
    except Exception:
        pass
    # Rising VIX (above previous close) is bearish for the index.
    if last > prev:
        bias = 'Bearish'
    elif last < prev:
        bias = 'Bullish'
    else:
        bias = 'Neutral'
    return {
        'last': last,
        'prev': prev,
        'change': round(last - prev, 3),
        'bias': bias,
        'source': 'Longbridge .VIX.US vs previous close',
    }


def _status(bars, regime, trend, marks, delta, vix, exp, mtf):
    last_r = regime[-1] if regime else 'range'
    regime_lbl = {'up': 'TRENDING UP', 'down': 'TRENDING DOWN'}.get(last_r, 'RANGEBOUND')
    last_m = marks[-1] if marks else None
    if last_m and last_m['kind'].endswith('9on10'):
        setup = last_m['kind']
    elif last_r == 'range':
        setup = 'Range rejections' if last_m and last_m['kind'] == 'PEND' else 'Range'
    elif last_m and last_m['kind'].endswith('Shift'):
        setup = 'Breakout'
    elif last_m and last_m['kind'] == 'PEND':
        setup = 'Trail test'
    else:
        setup = 'Trend hold'

    # Wall Shift: last confirmed shift still held?
    hold = '—'
    last_shift = next((m for m in reversed(marks) if m['kind'].endswith('Shift')), None)
    if last_shift:
        side = 'L' if last_shift['kind'].startswith('LONG') else 'S'
        n = 0
        for b in reversed(bars):
            if b['t'] < last_shift['t']:
                break
            n += 1
        still = (side == 'L' and trend[-1] == 1) or (side == 'S' and trend[-1] == -1)
        hold = ('%s holding 1' % side) if still else ('%s faded' % side)
        hold = hold + (' · %d bars' % n)

    net6 = sum(x['v'] for x in delta[-6:]) if delta else 0
    vol_bias = 'Bullish' if net6 > 0 else 'Bearish' if net6 < 0 else 'Neutral'

    # TICK proxy: share of last 10 closes that were up vs the prior close. Not $TICK.
    start = max(1, len(bars) - 10)
    ups = sum(1 for k in range(start, len(bars)) if bars[k]['c'] > bars[k - 1]['c'])
    tick_n = len(bars) - start
    tick_bias = 'Bullish' if tick_n and ups >= 6 else 'Bearish' if tick_n and ups <= 4 else 'Neutral'
    tick_lbl = '%s (%d/%d up bars)' % (tick_bias, ups, tick_n)

    nine = '—'
    if len(bars) >= 11 and trend[-1]:
        if trend[-1] == 1:
            n = sum(1 for k in range(len(bars) - 10, len(bars)) if bars[k]['c'] > bars[k - 1]['c'])
            nine = 'Long %d on 10' % n
        else:
            n = sum(1 for k in range(len(bars) - 10, len(bars)) if bars[k]['c'] < bars[k - 1]['c'])
            nine = 'Short %d on 10' % n

    alert = 'Waiting for a signal'
    if last_m:
        dt = E._et(last_m['t']).strftime('%b %-d %I:%M %p ET').replace(' 0', ' ')
        alert = '%s @ %s (%s)' % (last_m['kind'], last_m['price'], dt)

    def tone(s):
        s = s or ''
        if s in ('TRENDING UP', 'Breakout') or s.startswith('Bullish') or s.startswith('L holding') or s.startswith('Long'):
            return 'bull'
        if s in ('TRENDING DOWN',) or s.startswith('Bearish') or s.startswith('S holding') or s.startswith('Short') or s.startswith('SHORT'):
            return 'bear'
        if 'Pending' in s or s.startswith('Neutral') or s in ('PEND', 'Trail test', 'Range rejections', 'RANGEBOUND', 'Waiting for a signal'):
            return 'wait'
        return 'neutral'

    rows = [
        ('Regime', regime_lbl),
        ('Setup', setup),
        ('Wall Shift', hold),
        ('MTF Light', mtf['label']),
        ('Vol Delta', vol_bias),
        ('TICK', tick_lbl),
        ('VIX', ('%s  %.2f' % (vix['bias'], vix['last'])) if vix else 'n/a'),
        ('EXP Thresh', (exp or {}).get('label') or 'n/a'),
        ('9on10', nine),
        ('Alert Output', alert),
    ]
    return {
        'rows': [{'k': k, 'v': v, 'tone': tone(v)} for k, v in rows],
        'mtf': mtf,
    }


def _mtf(trend5, st15, st60):
    def last_td(tr):
        for x in reversed(tr):
            if x:
                return x
        return 0
    a, b, c = last_td(trend5), last_td(st15[1] if st15 else [0]), last_td(st60[1] if st60 else [0])
    def name(x):
        return 'Up' if x == 1 else 'Down' if x == -1 else '—'
    parts = '5m %s · 15m %s · 1h %s' % (name(a), name(b), name(c))
    if a == b == c == 1:
        label, tone = 'Bullish', 'bull'
    elif a == b == c == -1:
        label, tone = 'Bearish', 'bear'
    elif a == -1 and (b == 1 or c == 1):
        label, tone = 'Pending Short', 'wait'
    elif a == 1 and (b == -1 or c == -1):
        label, tone = 'Pending Long', 'wait'
    elif a == -1:
        label, tone = 'Pending Short', 'wait'
    elif a == 1:
        label, tone = 'Pending Long', 'wait'
    else:
        label, tone = 'Mixed', 'wait'
    return {'label': label, 'detail': parts, 'tone': tone, 't5': a, 't15': b, 't60': c}


def payload(sym='SPX'):
    spec = E.resolve(sym)
    rth = E._rth(int(time.time()))
    ttl_b, ttl_q = (8, 4) if rth else (90, 40)
    bars = E._cached(('mbot-bars', spec['bar']), ttl_b, lambda: _bars(spec['bar'], 5))
    q = E._cached(('mbot-q', spec['bar']), ttl_q, lambda: E._quote(spec['bar']))
    if spec['id'] == 'SPX':
        vol_bars = E._cached(('mbot-bars', 'SPY.US'), ttl_b, lambda: _bars('SPY.US', 5))
        vol_src = 'SPY 5m volume (SPX index volume is not used)'
    else:
        vol_bars = bars
        vol_src = 'QQQ 5m volume'

    stop, trend, atr = _supertrend(bars)
    regime = _regime(bars, trend, stop, atr)
    marks = _signals(bars, trend, stop, atr, regime)
    delta = _delta(bars, vol_bars)
    b15 = _resample(bars, 15 * 60)
    b60 = _resample(bars, 60 * 60)
    st15 = _supertrend(b15)
    st60 = _supertrend(b60)
    mtf = _mtf(trend, st15, st60)

    def cache_or(key, ttl, fn, fallback=None):
        try:
            return E._cached(key, ttl, fn)
        except Exception as e:
            return fallback or {'ok': False, 'error': str(e)}

    last_px = bars[-1]['c'] if bars else q['last']
    if rth:
        last_px = q['last']
    prev = q['prev_close']
    vix = cache_or('mbot-vix', 60 if rth else 300, _vix, {'bias': 'n/a', 'last': None, 'source': 'unavailable'})
    exp = cache_or(('mbot-exp', spec['id']), 180 if rth else 1800,
                   lambda: _exp_move(spec, last_px), {'ok': False})

    # Session levels (last RTH day) + visible high/low
    last_day = str(E._et(bars[-1]['t']).date()) if bars else None
    day_bars = [b for b in bars if str(E._et(b['t']).date()) == last_day] if last_day else []
    vis_h = max(b['h'] for b in bars) if bars else last_px
    vis_l = min(b['l'] for b in bars) if bars else last_px
    sess_h = max(b['h'] for b in day_bars) if day_bars else vis_h
    sess_l = min(b['l'] for b in day_bars) if day_bars else vis_l
    typ = [(b['h'] + b['l'] + b['c']) / 3 for b in day_bars]
    vv = [b['v'] for b in day_bars]
    if day_bars and sum(vv) > 0:
        vwap = sum(t * v for t, v in zip(typ, vv)) / sum(vv)
    else:
        vwap = last_px
    sess_mid = (sess_h + sess_l) / 2
    # Previous RTH close = last bar of the prior day
    prev_sess = prev
    days = []
    for b in bars:
        d = str(E._et(b['t']).date())
        if d not in days:
            days.append(d)
    if len(days) >= 2:
        prior = [b for b in bars if str(E._et(b['t']).date()) == days[-2]]
        if prior:
            prev_sess = prior[-1]['c']

    status = _status(bars, regime, trend, marks, delta, vix, exp, mtf)

    last_t = bars[-1]['t'] if bars else int(time.time())
    chg = last_px - prev
    chg_pct = (last_px / prev - 1) * 100 if prev else 0
    asof = (lambda d: d.strftime('%b ') + str(d.day) + d.strftime(', %Y · ') + d.strftime('%I:%M %p').lstrip('0') + ' ET')(E._et(last_t))

    colors = [{'up': 'teal', 'down': 'mag'}.get(r, 'gray') for r in regime]
    trail = [None if s is None else round(s, 2) for s in stop]

    return {
        'symbol': spec['label'],
        'bar_symbol': spec['bar'],
        'period': '5m',
        'last': round(last_px, 2),
        'prev_close': round(prev, 2),
        'change': round(chg, 2),
        'change_pct': round(chg_pct, 2),
        'asof_et': asof,
        'asof_ts': last_t,
        'bars': bars,
        'colors': colors,
        'trail': trail,
        'marks': marks,
        'delta': delta,
        'levels': {
            'high': round(vis_h, 2),
            'low': round(vis_l, 2),
            'sess_high': round(sess_h, 2),
            'sess_low': round(sess_l, 2),
            'sess_mid': round(sess_mid, 2),
            'vwap': round(vwap, 2),
            'prev_sess': round(prev_sess, 2),
        },
        'status': status,
        'vix': vix,
        'exp': exp,
        'delta_note': '成交量差：按K线高低位置拆分的成交量（阳−阴近似）。黄柱表示量差方向与收盘涨跌相反。' + vol_src + '。不是期权资金流，也不是官方 $TICK。',
        'rules': (
            '展示规则（与 QQQ 新规则 v3 无关）：跟踪止损为 Supertrend（ATR %d × %.1f）。'
            '收盘上穿止损 = LONG Shift，下穿 = SHORT Shift。'
            '价格触及止损但收盘仍在趋势一侧 = PEND。'
            '最近 10 根里有 9 根收盘方向与趋势一致 = LONG/SHORT 9on10（每个趋势只标一次）。'
            '近期频繁翻转或紧贴止损 = RANGEBOUND，K 线画灰色；上升趋势青色，下降趋势品红。'
            % (ATR_N, ATR_MULT)
        ),
        'source': {
            'bars': 'Longbridge 5m kline, session=intraday (09:30–16:00 ET), last 5 sessions, real timestamps (overnight gaps visible)',
            'trail': 'Supertrend ATR%s x %s on 5-minute bars' % (ATR_N, ATR_MULT),
            'delta': vol_src,
            'vix': (vix or {}).get('source'),
            'exp': (exp or {}).get('source'),
            'tick': 'proxy: up-closes in the last 10 5m bars (not $TICK)',
            'mtf': 'Supertrend on resampled 15m and 60m from the same 5m bars',
        },
    }


def prewarm():
    def run():
        for s in ('SPX', 'QQQ'):
            try:
                payload(s)
            except Exception as e:
                print('mbot prewarm', s, repr(e), flush=True)
    threading.Thread(target=run, daemon=True).start()
