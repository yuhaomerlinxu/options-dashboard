"""Display-only trade markers and regime segments for the /qqq 3m chart.

Does not change 新规则 v3: that rule still only emits OPEN/REV entries on
1-minute data, and this module never retimes or suppresses those signals.

Segments cover the whole regular session (09:30–16:00 ET). While a v3
signal is in force, the segment follows that position. After it closes —
or when v3 issues no signal (it stops new REV after 10:30 ET) — a
display-only trend regime continues the 多头/空头 shading.

Trend bias (display-only), with hysteresis:
  long  if 3m close > EMA20 and close > session VWAP
  short if 3m close < EMA20 and close < session VWAP
  otherwise hold the current regime (mixed close does not flip)
A flip needs CONFIRM=2 consecutive 3m bars of the opposite clear bias
and MIN_HOLD=4 bars (12 min) in the current trend segment. A v3 signal
may interrupt immediately (same-direction 加仓, opposite 平仓+入场).

Every segment has exactly one 入场 at its start, 止盈 when price moved
favorably, and 平仓 at the end (or the reversing 入场 of the next
segment). An open live segment has no 平仓 yet.

  入场  — v3 signal that opens or reverses a position, or the first bar
          of a confirmed display-only trend segment
  加仓  — a later v3 signal in the same direction while already in that
          segment (unchanged)
  止盈  — confirmed 3m swing extreme at least min_step beyond the last
          止盈 (or entry), with the swing close also in favor by
          min_step. If a segment never makes a qualifying swing but the
          best favorable extreme is modestly in profit, mark that once.
          If never in profit, 平仓 only.
  平仓  — v3 position: opposite v3 signal, 3m close through EMA20
          against the trade after it has been in favor / after a 止盈,
          0.4% stop before any 止盈, or last RTH bar.
          Trend position: confirmed opposite bias, or last RTH bar.

min_step is max(0.15% of entry, 0.7 × ATR(14) on 3m bars).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
CONFIRM = 2
MIN_HOLD = 4
MODEST = 0.0008  # 0.08% — fallback 止盈 threshold


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


def _bias(close, ema, vwap):
    if ema is None:
        return 0
    if vwap is None:
        if close > ema:
            return 1
        if close < ema:
            return -1
        return 0
    if close > ema and close > vwap:
        return 1
    if close < ema and close < vwap:
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
    last_day = str(_et(rth[-1]['t']).date())
    day = [b for b in rth if str(_et(b['t']).date()) == last_day]
    return day[0]['t'], day[-1]['t']


def build(bars, signals, ema20=None, vwap=None):
    bars = list(bars or [])
    signals = [s for s in (signals or []) if _dir_of(s)]
    ema20 = list(ema20 or [])
    vwap = list(vwap or [])
    if len(ema20) < len(bars):
        ema20 = ema20 + [None] * (len(bars) - len(ema20))
    if len(vwap) < len(bars):
        vwap = vwap + [None] * (len(bars) - len(vwap))
    index = {b['t']: i for i, b in enumerate(bars)}
    atr = _atr(bars)
    by = {}
    for s in sorted(signals, key=lambda x: int(x['ts'])):
        t = _bucket(int(s['ts']), index)
        if t is None:
            continue
        by.setdefault(t, []).append(s)

    def bias_at(i):
        return _bias(bars[i]['c'], ema20[i], vwap[i])

    def confirmed(i, direction):
        if direction == 0 or i + 1 < CONFIRM:
            return False
        for k in range(i - CONFIRM + 1, i + 1):
            if k < 0 or not _rth(bars[k]['t']) or bias_at(k) != direction:
                return False
        return True

    markers, segments = [], []
    pos = None
    seg = None
    opp_streak = 0

    def mark(i, kind, label, price, direction, src=None):
        b = bars[i]
        markers.append({
            't': b['t'], 'kind': kind, 'label': label,
            'price': round(float(price), 3),
            'dir': 'long' if direction == 1 else 'short',
            'src': src if src is not None else (pos['source'] if pos else 'trend'),
        })

    def fallback_tp(up_to):
        """Mark the best favorable extreme once if no swing 止盈 yet."""
        if not pos or pos['had_tp'] or up_to <= pos['entry_i']:
            return
        lo, hi, bi = None, None, None
        for j in range(pos['entry_i'] + 1, up_to + 1):
            if j >= len(bars):
                break
            if _et(bars[j]['t']).date() != _et(bars[pos['entry_i']]['t']).date():
                continue
            if pos['dir'] == 1:
                if hi is None or bars[j]['h'] > hi:
                    hi, bi = bars[j]['h'], j
            else:
                if lo is None or bars[j]['l'] < lo:
                    lo, bi = bars[j]['l'], j
        if bi is None:
            return
        px = hi if pos['dir'] == 1 else lo
        fav = (px - pos['entry_px']) if pos['dir'] == 1 else (pos['entry_px'] - px)
        need = pos['entry_px'] * MODEST
        if fav >= need:
            mark(bi, 'tp', '止盈', px, pos['dir'])
            pos['last_tp'] = px
            pos['had_tp'] = True

    def open_pos(i, direction, price, source):
        nonlocal pos, seg, opp_streak
        px = float(price)
        a = atr[i] or px * 0.002
        pos = {
            'dir': direction, 'entry_i': i, 'entry_px': px,
            'last_tp': None, 'had_tp': False,
            'min_step': max(px * 0.0015, 0.7 * a),
            'stop': px * 0.004,
            'source': source,
        }
        mark(i, 'entry', '入场', px, direction, source)
        seg = {'dir': 'long' if direction == 1 else 'short',
               'label': '多头' if direction == 1 else '空头',
               't0': bars[i]['t'], 't1': bars[i]['t'],
               'src': source}
        opp_streak = 0

    def close_pos(i, price, reason):
        nonlocal pos, seg
        if not pos:
            return
        # Do not use the still-unconfirmed last bar as a fallback extreme
        # when a next bar exists; on a true close, the close bar is fair game.
        fallback_tp(i)
        mark(i, 'close', '平仓', price, pos['dir'])
        if seg:
            seg['t1'] = bars[i]['t']
            seg['reason'] = reason
            segments.append(seg)
        pos = None
        seg = None

    def swing_tp(i):
        if not pos or i < 2 or pos['entry_i'] >= i - 1:
            return
        j = i - 1
        p, prev, cur = bars[j], bars[j - 1], bars[i]
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

    for i, b in enumerate(bars):
        if seg:
            seg['t1'] = b['t']
        rth = _rth(b['t'])
        nxt = bars[i + 1] if i + 1 < len(bars) else None
        last_rth = rth and (nxt is None or not _rth(nxt['t']) or _et(nxt['t']).date() != _et(b['t']).date())

        if rth:
            swing_tp(i)
            # Live open segment: stamp a fallback 止盈 on a confirmed extreme
            if pos and not pos['had_tp'] and not last_rth:
                fallback_tp(i - 1)

        just_opened = False
        if rth:
            for s in by.get(b['t'], []):
                d = _dir_of(s)
                px = s.get('price', b['c'])
                if pos is None:
                    open_pos(i, d, px, 'v3')
                    just_opened = True
                elif pos['dir'] == d:
                    mark(i, 'add', '加仓', px, d, 'v3')
                    pos['source'] = 'v3'
                    if seg:
                        seg['src'] = 'v3'
                else:
                    close_pos(i, px, 'opposite')
                    open_pos(i, d, px, 'v3')
                    just_opened = True

        if not rth:
            continue

        if last_rth and pos:
            close_pos(i, b['c'], 'eod')
            continue

        if just_opened or not pos:
            if not pos:
                d = bias_at(i)
                if confirmed(i, d):
                    open_pos(i, d, b['c'], 'trend')
            continue

        e = ema20[i]
        step = pos['min_step']
        if pos['source'] == 'v3':
            if not pos['had_tp']:
                against = (pos['entry_px'] - b['c']) if pos['dir'] == 1 else (b['c'] - pos['entry_px'])
                if against >= pos['stop']:
                    close_pos(i, b['c'], 'stop')
            if pos and pos['source'] == 'v3':
                in_favor = (b['c'] - pos['entry_px']) if pos['dir'] == 1 else (pos['entry_px'] - b['c'])
                if e is not None and (pos['had_tp'] or in_favor >= step):
                    if pos['dir'] == 1 and b['c'] < e:
                        close_pos(i, b['c'], 'ema')
                    elif pos['dir'] == -1 and b['c'] > e:
                        close_pos(i, b['c'], 'ema')
        else:
            bnow = bias_at(i)
            if bnow == -pos['dir']:
                opp_streak += 1
            elif bnow == pos['dir']:
                opp_streak = 0
            if opp_streak >= CONFIRM and (i - pos['entry_i']) >= MIN_HOLD:
                close_pos(i, b['c'], 'trend')
                open_pos(i, -pos['dir'] if pos else bnow, b['c'], 'trend')
                just_opened = True

        # After an exit on this bar, keep shading if bias is already clear.
        if pos is None and not just_opened:
            d = bias_at(i)
            if d != 0:
                open_pos(i, d, b['c'], 'trend')

    if pos and seg:
        fallback_tp(len(bars) - 1)
        segments.append(seg)

    focus_from, focus_to = _focus(bars)
    return {
        'segments': segments,
        'markers': markers,
        'analysis_end_t': focus_to,
        'focus_from': focus_from,
        'focus_to': focus_to,
    }
