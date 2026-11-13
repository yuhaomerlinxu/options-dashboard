"""Display-only trade markers and regime segments for the /qqq 3m chart.

Does not change 新规则 v3: entries still come only from v3 signals
(OPEN / REV on 1-minute data). The rule itself has no add / take-profit /
close events, so those markers and the 多头/空头 shading are derived here
for display:

  入场  — a v3 signal that opens a new position (flat → long/short, or a
          reverse after the previous position is closed)
  加仓  — a later v3 signal in the same direction while already in that
          position (v3 cooldown is per direction, so this can happen)
  止盈  — display-only scale-out: a confirmed 3m swing extreme at least
          min_step beyond the last 止盈 (or entry), with the swing bar
          close also in favor by min_step (filters long-wick spikes). Does not exit the trade.
  平仓  — display-only exit, first of:
          (1) opposite v3 signal (then a new 入场 on the same bar),
          (2) 3m close through EMA20 against the position after it has
              been in favor by min_step (or after a 止盈),
          (3) stop: 3m close 0.4% against the first entry before any 止盈,
          (4) last regular-session 3m bar (16:00 ET).

min_step is max(0.15% of entry, 0.7 × ATR(14) on 3m bars).
Stop / take-profit / EMA-exit never suppress or retimes a v3 entry.
v3 emits no new signals after 10:30 ET (REV_END) except none after 15:45,
so later 入场/加仓 will not appear; 止盈/平仓 may still be marked after that.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')


def _et(ts):
    return datetime.fromtimestamp(ts, ET)


def _rth(ts):
    d = _et(ts)
    m = d.hour * 60 + d.minute
    return d.weekday() < 5 and 9 * 60 + 30 <= m < 16 * 60


def _atr(bars, n=14):
    out = [None] * len(bars)
    trs = []
    prev = None
    for i, b in enumerate(bars):
        tr = b['h'] - b['l']
        if prev is not None:
            tr = max(tr, abs(b['h'] - prev['c']), abs(b['l'] - prev['c']))
        trs.append(tr)
        out[i] = sum(trs[-n:]) / len(trs[-n:])
        prev = b
    return out


def _bucket(ts, index):
    """Map a v3 signal timestamp (1m close) to a 3m bar start time."""
    start = int(ts) - 60
    t = start - start % 180
    if t in index:
        return t
    # nearest 3m bar that contains or is just before the 1m close
    for dt in (0, -180, 180, -360, 360):
        if t + dt in index:
            return t + dt
    return None


def _dir_of(sig):
    d = str(sig.get('direction') or '').lower()
    if d.startswith('bull') or d == 'call':
        return 1
    if d.startswith('bear') or d == 'put':
        return -1
    return 0


def _focus(bars, session_date=None):
    rth = [b for b in bars if _rth(b['t'])]
    if session_date:
        day = [b for b in rth if str(_et(b['t']).date()) == str(session_date)]
        if day:
            rth = day
    if not rth and bars:
        return bars[0]['t'], bars[-1]['t']
    if not rth:
        return None, None
    # last session's RTH if several days are present
    last_day = str(_et(rth[-1]['t']).date())
    day = [b for b in rth if str(_et(b['t']).date()) == last_day]
    return day[0]['t'], day[-1]['t']


def build(bars, signals, ema20=None):
    bars = list(bars or [])
    signals = [s for s in (signals or []) if _dir_of(s)]
    ema20 = list(ema20 or [])
    if len(ema20) < len(bars):
        ema20 = ema20 + [None] * (len(bars) - len(ema20))
    index = {b['t']: i for i, b in enumerate(bars)}
    atr = _atr(bars)
    by = {}
    for s in sorted(signals, key=lambda x: int(x['ts'])):
        t = _bucket(int(s['ts']), index)
        if t is None:
            continue
        by.setdefault(t, []).append(s)

    markers, segments = [], []
    pos = None
    seg = None

    def mark(i, kind, label, price, direction):
        b = bars[i]
        markers.append({
            't': b['t'], 'kind': kind, 'label': label,
            'price': round(float(price), 3),
            'dir': 'long' if direction == 1 else 'short',
        })

    def open_pos(i, direction, price):
        nonlocal pos, seg
        px = float(price)
        a = atr[i] or px * 0.002
        pos = {
            'dir': direction, 'entry_i': i, 'entry_px': px,
            'last_tp': None, 'had_tp': False,
            'min_step': max(px * 0.0015, 0.7 * a),
            'stop': px * 0.004,
        }
        mark(i, 'entry', '入场', px, direction)
        seg = {'dir': 'long' if direction == 1 else 'short',
               'label': '多头' if direction == 1 else '空头',
               't0': bars[i]['t'], 't1': bars[i]['t']}

    def close_pos(i, price, reason):
        nonlocal pos, seg
        if not pos:
            return
        mark(i, 'close', '平仓', price, pos['dir'])
        if seg:
            seg['t1'] = bars[i]['t']
            seg['reason'] = reason
            segments.append(seg)
        pos = None
        seg = None

    for i, b in enumerate(bars):
        if seg:
            seg['t1'] = b['t']
        rth = _rth(b['t'])
        nxt = bars[i + 1] if i + 1 < len(bars) else None
        last_rth = rth and (nxt is None or not _rth(nxt['t']) or _et(nxt['t']).date() != _et(b['t']).date())

        # Confirmed 3m swing 止盈 on the previous bar (needs this bar to confirm)
        if pos and i >= 2 and pos['entry_i'] < i - 1:
            j = i - 1
            p, prev, cur = bars[j], bars[j - 1], b
            step = pos['min_step']
            last = pos['last_tp'] if pos['last_tp'] is not None else pos['entry_px']
            if pos['dir'] == 1:
                swing = p['h'] >= prev['h'] and p['h'] > cur['h']
                fav_ext, fav_c = p['h'] - last, p['c'] - pos['entry_px']
                px = p['h']
            else:
                swing = p['l'] <= prev['l'] and p['l'] < cur['l']
                fav_ext, fav_c = last - p['l'], pos['entry_px'] - p['c']
                px = p['l']
            if swing and fav_ext >= step and fav_c >= step:
                mark(j, 'tp', '止盈', px, pos['dir'])
                pos['last_tp'] = px
                pos['had_tp'] = True

        just_opened = False
        for s in by.get(b['t'], []):
            d = _dir_of(s)
            px = s.get('price', b['c'])
            if pos is None:
                open_pos(i, d, px)
                just_opened = True
            elif pos['dir'] == d:
                mark(i, 'add', '加仓', px, d)
            else:
                close_pos(i, px, 'opposite')
                open_pos(i, d, px)
                just_opened = True

        if not pos or just_opened or not rth:
            continue

        e = ema20[i]
        step = pos['min_step']
        if not pos['had_tp']:
            against = (pos['entry_px'] - b['c']) if pos['dir'] == 1 else (b['c'] - pos['entry_px'])
            if against >= pos['stop']:
                close_pos(i, b['c'], 'stop')
                continue
        in_favor = (b['c'] - pos['entry_px']) if pos['dir'] == 1 else (pos['entry_px'] - b['c'])
        if e is not None and (pos['had_tp'] or in_favor >= step):
            if pos['dir'] == 1 and b['c'] < e:
                close_pos(i, b['c'], 'ema')
                continue
            if pos['dir'] == -1 and b['c'] > e:
                close_pos(i, b['c'], 'ema')
                continue
        if last_rth:
            close_pos(i, b['c'], 'eod')

    if seg:
        segments.append(seg)

    focus_from, focus_to = _focus(bars)
    analysis_end = focus_to
    return {
        'segments': segments,
        'markers': markers,
        'analysis_end_t': analysis_end,
        'focus_from': focus_from,
        'focus_to': focus_to,
    }
