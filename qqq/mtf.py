"""2/3/4/5-minute resonance signals built from 1m bars (closed bars only, no repaint)."""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
ET = ZoneInfo('America/New_York'); BJ = timezone(timedelta(hours=8))
TFS = (2, 3, 4, 5); COOLDOWN = 15 * 60

def session_of(ts):
    d = datetime.fromtimestamp(ts, ET); m = d.hour * 60 + d.minute
    if 240 <= m < 570: name = '盘前'
    elif 570 <= m < 960: name = '盘中'
    elif 960 <= m < 1200: name = '盘后'
    else: name = '夜盘'
    # session id: date of session start (overnight 20:00 belongs to start date)
    sd = (d - timedelta(days=1)).date() if m < 240 else d.date()
    return name, '%s-%s' % (sd, name)

def compute(bars, now=None):
    """bars: list of {'t','o','h','l','c','v'} 1m, t = bar start epoch secs, sorted."""
    now = now or datetime.now(timezone.utc).timestamp()
    b1 = [b for b in bars if b['t'] + 60 <= now]  # closed 1m bars only
    k20 = 2 / 21
    agg = {tf: None for tf in TFS}; st = {tf: {'prev': None, 'last': None, 'ema': None, 'pema': None} for tf in TFS}
    out = []; last = {}; sid0 = None; pv = vv = 0; sh = sl = None
    for b in b1:
        name, sid = session_of(b['t'])
        if sid != sid0: sid0 = sid; pv = vv = 0; sh = sl = None; last = {}
        pv += (b['h'] + b['l'] + b['c']) / 3 * b['v']; vv += b['v']; vwap = pv / vv if vv else b['c']
        sh = b['h'] if sh is None else max(sh, b['h']); sl = b['l'] if sl is None else min(sl, b['l'])
        end = b['t'] + 60
        for tf in TFS:
            bs = b['t'] - b['t'] % (tf * 60); a = agg[tf]
            if a is None or a['t'] != bs: a = agg[tf] = {'t': bs, 'o': b['o'], 'h': b['h'], 'l': b['l'], 'c': b['c']}
            else: a['h'] = max(a['h'], b['h']); a['l'] = min(a['l'], b['l']); a['c'] = b['c']
            if end == bs + tf * 60:  # tf bar just closed
                s = st[tf]; s['prev'] = s['last']; s['last'] = dict(a)
                s['pema'] = s['ema']; s['ema'] = a['c'] if s['ema'] is None else a['c'] * k20 + s['ema'] * (1 - k20)
                s['n'] = s.get('n', 0) + 1
        # evaluate at each 1m close
        cond = {}; dirs = set()
        for tf in TFS:
            s = st[tf]
            if not s['prev'] or s['pema'] is None or s.get('n', 0) < 20: dirs.add(None); break
            L, P, e, pe = s['last'], s['prev'], s['ema'], s['pema']
            bull = L['c'] > P['h'] and L['c'] > e and e > pe
            bear = L['c'] < P['l'] and L['c'] < e and e < pe
            dirs.add('bullish' if bull else 'bearish' if bear else None)
            tbj = datetime.fromtimestamp(L['t'], BJ).strftime('%H:%M')
            cond['%dm' % tf] = '%s收 %.2f %s 前高 %.2f / 前低 %.2f · EMA20 %.2f %s' % (
                tbj, L['c'], '>' if bull else '<' if bear else '~', P['h'], P['l'], e, '↑' if e > pe else '↓')
        if len(dirs) != 1 or None in dirs: continue
        d = dirs.pop()
        if (d == 'bullish' and b['c'] <= vwap) or (d == 'bearish' and b['c'] >= vwap): continue
        if d in last and end - last[d] < COOLDOWN: continue
        last[d] = end
        cond['VWAP'] = '1m收 %.2f %s VWAP %.2f' % (b['c'], '>' if d == 'bullish' else '<', vwap)
        out.append({'direction': d, 'price': round(b['c'], 3), 'ts': end, 'session': name,
                    'trigger_time_bj': datetime.fromtimestamp(end, BJ).strftime('%Y-%m-%d %H:%M:%S'),
                    'trigger_time_et': datetime.fromtimestamp(end, ET).strftime('%Y-%m-%d %H:%M'),
                    'day_high': round(sh, 3), 'day_low': round(sl, 3), 'vwap': round(vwap, 3), 'timeframes': cond})
    return out

def backtest(bars, sigs):
    m = {b['t']: b['c'] for b in bars}; rows = []
    for s in sigs:
        r = dict(s)
        for n in (15, 30):
            c = m.get(s['ts'] + n * 60 - 60)
            sg = 1 if s['direction'] == 'bullish' else -1
            r['m%d' % n] = None if c is None else round((c - s['price']) * sg, 3)
        rows.append(r)
    return rows
