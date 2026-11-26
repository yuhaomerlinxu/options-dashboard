"""Standalone SPX displacement / king / sigma board (/spx/levels).

Market data: Longbridge .SPX.US klines (read-only via lbsafe).
0DTE strikes and implied 1σ: Futu SPXW chain, pushed from the box
because Render cannot reach OpenD. Never places orders.
"""
import json, math, os, subprocess, time, threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
BJ = ZoneInfo('Asia/Shanghai')
LB = os.environ.get('LBSAFE', '/app/lbsafe')
DISP_K = 1.5          # |body| > k × ATR(14)
CONFLUENCE_PTS = 8    # levels within this many points count as confluent
EMA_CROSS_BARS = 3    # EMA cross may be on this bar or the previous 3
SESSIONS = 3
ATR_N, EMA_FAST, EMA_SLOW = 14, 9, 20

# timeframe, lbsafe period, history count, short label, color
TFS = (
    ('15m', '15m', 400, '15m', '#4dd0e1'),
    ('1h',  '1h',  400, '1h',  '#ffb74d'),
    ('4h',  '4h',  200, '4H',  '#fff176'),
    ('day', 'day', 120, 'Day', '#64b5f6'),
    ('week','week', 80, 'Week','#ce93d8'),
    ('month','month', 48,'Month','#f48fb1'),
)

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

def last_displacements(bars, k=DISP_K):
    """Most recent bull/bear displacement: large-body candle > k×ATR(14).

    Level is the *open* (origin) of that candle. Bull = last bullish
    displacement (support). Bear = last bearish displacement (resistance).
    """
    atrs = atr_series(bars)
    bull = bear = None
    for i, b in enumerate(bars):
        a = atrs[i]
        if not a or a <= 0:
            continue
        body = b['c'] - b['o']
        if abs(body) <= k * a:
            continue
        rec = {
            't': b['t'], 'px': round(b['o'], 2),
            'close': round(b['c'], 2),
            'body': round(body, 2), 'atr': round(a, 2),
        }
        if body > 0:
            bull = rec
        else:
            bear = rec
    return bull, bear

def completed_bar_index(bars, kind, now=None):
    """Index of the last completed day/week/month bar (excludes the in-progress one)."""
    if not bars:
        return None
    now = now or datetime.now(ET)
    last = _et(bars[-1]['t'])
    in_progress = False
    if kind == 'day':
        in_progress = last.date() == now.date() and not _session_closed(now)
        # On a full holiday after a completed prior session, last daily bar is already complete.
        if last.date() < now.date():
            in_progress = False
    elif kind == 'week':
        # ISO week: Monday start. A week is complete after Friday close (or half-day close).
        last_monday = (last.date() - timedelta(days=last.weekday()))
        now_monday = now.date() - timedelta(days=now.weekday())
        in_progress = last_monday == now_monday
    elif kind == 'month':
        in_progress = (last.year, last.month) == (now.year, now.month)
    else:
        return len(bars) - 1
    return len(bars) - 2 if in_progress and len(bars) >= 2 else len(bars) - 1

def _session_closed(now):
    # Winter close 16:00 ET, half-days 13:00 — treat after 13:00 on known half days elsewhere.
    return now.weekday() >= 5 or now.hour > 16 or (now.hour == 16 and now.minute >= 0)

def typical(bar):
    return (bar['h'] + bar['l'] + bar['c']) / 3.0

def session_vwap_sigma(bars):
    """VWAP and ±1σ of the last RTH session (volume-weighted typical price)."""
    if not bars:
        return None
    last_day = max(_et(b['t']).date() for b in bars)
    day = [b for b in bars if _et(b['t']).date() == last_day]
    pv = vv = 0.0
    acc = []
    for b in day:
        tp = typical(b)
        v = b['v'] if b['v'] > 0 else 1.0
        pv += tp * v
        vv += v
        if vv > 0:
            acc.append((tp, v, pv / vv))
    if vv <= 0 or not acc:
        return None
    vwap = pv / vv
    var = sum((tp - vwap) ** 2 * v for tp, v, _ in acc) / vv
    sig = math.sqrt(max(var, 0.0))
    return {
        'vwap': round(vwap, 2),
        'sigma': round(sig, 2),
        'upper': round(vwap + sig, 2),
        'lower': round(vwap - sig, 2),
        'session': last_day.isoformat(),
        'n': len(day),
    }

def _round5(px):
    return int(round(px / 5.0) * 5)

def _load_futu_snap():
    try:
        import spxlevels_snap
        return spxlevels_snap.get_snap()
    except Exception:
        return None

def _group_levels(items, pts=CONFLUENCE_PTS):
    """Cluster nearby levels; the first (priority-sorted) name is kept, +N for extras."""
    items = sorted([x for x in items if x and x.get('px') is not None], key=lambda x: (x['px'], x.get('priority', 9)))
    groups = []
    for it in items:
        placed = False
        for g in groups:
            if abs(g['px'] - it['px']) <= pts:
                g['members'].append(it)
                # keep the tighter / higher-priority price as the displayed one
                if it.get('priority', 9) < g.get('priority', 9):
                    g['px'] = it['px']
                    g['name'] = it['name']
                    g['color'] = it['color']
                    g['kind'] = it['kind']
                    g['priority'] = it['priority']
                g['n'] = len(g['members'])
                placed = True
                break
        if not placed:
            groups.append({
                'px': it['px'], 'name': it['name'], 'color': it['color'],
                'kind': it['kind'], 'priority': it.get('priority', 9),
                'members': [it], 'n': 1, 'tf': it.get('tf'), 'side': it.get('side'),
            })
    return groups

def _visible_sessions(bars, n=SESSIONS):
    days = sorted({_et(b['t']).date() for b in bars})
    keep = set(days[-n:])
    return [b for b in bars if _et(b['t']).date() in keep], days[-n:] if days else []

def _signals(bars, ema9, ema20, groups, spot, expiry):
    """Historical BUY / EXIT markers on the visible 5m bars. Informational only."""
    if not bars or not groups:
        return []
    bull_lvls = [g for g in groups if g.get('side') == 'bull' or g.get('kind') == 'disp-bull']
    bear_lvls = [g for g in groups if g.get('side') == 'bear' or g.get('kind') == 'disp-bear']
    # also treat king / 4h / sigma as confluence partners (already in groups)
    def near(px):
        return [g for g in groups if abs(g['px'] - px) <= CONFLUENCE_PTS]

    def ema_up(i):
        for j in range(max(1, i - EMA_CROSS_BARS + 1), i + 1):
            if ema9[j] is None or ema20[j] is None or ema9[j - 1] is None or ema20[j - 1] is None:
                continue
            if ema9[j - 1] <= ema20[j - 1] and ema9[j] > ema20[j]:
                return True
        return False

    def ema_down(i):
        for j in range(max(1, i - EMA_CROSS_BARS + 1), i + 1):
            if ema9[j] is None or ema20[j] is None or ema9[j - 1] is None or ema20[j - 1] is None:
                continue
            if ema9[j - 1] >= ema20[j - 1] and ema9[j] < ema20[j]:
                return True
        return False

    def next_res(px):
        above = sorted([g['px'] for g in groups if g['px'] > px + 1])
        return above[0] if above else None

    def next_sup(px):
        below = sorted([g['px'] for g in groups if g['px'] < px - 1])
        return below[-1] if below else None

    exp_lab = ''
    if expiry:
        try:
            exp_lab = datetime.strptime(str(expiry)[:10], '%Y-%m-%d').strftime('%-m/%-d')
        except Exception:
            exp_lab = str(expiry)[:10]
    side = None  # 'call' | 'put'
    entry_px = None
    exit_level = None
    ev = []
    for i in range(1, len(bars)):
        if ema9[i] is None or ema20[i] is None:
            continue
        b, p = bars[i], bars[i - 1]
        if side is None:
            # reclaim a bull disp: prior close below, this close above
            hit = None
            for g in bull_lvls:
                if p['c'] < g['px'] <= b['c'] and ema_up(i) and len(near(g['px'])) >= 2:
                    hit = g
                    break
            if hit:
                k = _round5(b['c'])
                side, entry_px, exit_level = 'call', hit['px'], next_res(hit['px'])
                ev.append({
                    't': b['t'], 'side': 'BUY', 'cp': 'C', 'k': k,
                    'px': round(b['c'], 2), 'level': hit['px'],
                    'label': 'BUY NOW — SPX $%sC %s' % (k, ('0DTE' if not exp_lab else exp_lab)),
                })
                continue
            hit = None
            for g in bear_lvls:
                if p['c'] > g['px'] >= b['c'] and ema_down(i) and len(near(g['px'])) >= 2:
                    hit = g
                    break
            if hit:
                k = _round5(b['c'])
                side, entry_px, exit_level = 'put', hit['px'], next_sup(hit['px'])
                ev.append({
                    't': b['t'], 'side': 'BUY', 'cp': 'P', 'k': k,
                    'px': round(b['c'], 2), 'level': hit['px'],
                    'label': 'BUY NOW — SPX $%sP %s' % (k, ('0DTE' if not exp_lab else exp_lab)),
                })
                continue
        elif side == 'call':
            hit_opp = exit_level is not None and b['h'] >= exit_level
            fail = b['c'] < entry_px
            if hit_opp or fail:
                ev.append({
                    't': b['t'], 'side': 'EXIT', 'cp': 'C',
                    'px': round(exit_level if hit_opp and exit_level else b['c'], 2),
                    'level': exit_level if hit_opp else entry_px,
                    'label': 'EXIT',
                    'reason': '触及反向关键位' if hit_opp else '收盘跌回位移位下方',
                })
                side = entry_px = exit_level = None
        elif side == 'put':
            hit_opp = exit_level is not None and b['l'] <= exit_level
            fail = b['c'] > entry_px
            if hit_opp or fail:
                ev.append({
                    't': b['t'], 'side': 'EXIT', 'cp': 'P',
                    'px': round(exit_level if hit_opp and exit_level else b['c'], 2),
                    'level': exit_level if hit_opp else entry_px,
                    'label': 'EXIT',
                    'reason': '触及反向关键位' if hit_opp else '收盘涨回位移位上方',
                })
                side = entry_px = exit_level = None
    return ev

def _fmt_px(px):
    return format(px, ',.2f')

def payload():
    with _lk:
        e = _c.get('payload')
        if e and time.time() - e[0] < 15:
            return e[1]
    out = {'symbol': '.SPX.US', 'source': 'Longbridge', 'period': '5m'}
    try:
        q = quote()
        last = _f(q.get('last'))
        pc = _f(q.get('prev_close'))
        out['last'] = last
        out['prev_close'] = pc
        out['chg_pct'] = (last / pc - 1) * 100 if last and pc else _f(q.get('change_percentage'))
        out['day_high'] = _f(q.get('high'))
        out['day_low'] = _f(q.get('low'))
        out['ts'] = q.get('timestamp')
    except Exception as e:
        last = None
        out['quote_error'] = str(e)[:200]
    try:
        raw5 = kline('5m', 400)
        vis, days = _visible_sessions(raw5, SESSIONS)
        # EMA computed on the full raw series so the first visible bars are valid
        closes_all = [b['c'] for b in raw5]
        e9_all = ema_series(closes_all, EMA_FAST)
        e20_all = ema_series(closes_all, EMA_SLOW)
        # map by time
        e9m = {raw5[i]['t']: e9_all[i] for i in range(len(raw5))}
        e20m = {raw5[i]['t']: e20_all[i] for i in range(len(raw5))}
        bars = []
        e9 = []
        e20 = []
        for b in vis:
            rec = dict(b)
            rec['ema20'] = e20m.get(b['t'])
            rec['ema9'] = e9m.get(b['t'])
            bars.append(rec)
            e9.append(e9m.get(b['t']))
            e20.append(e20m.get(b['t']))
        out['bars'] = bars
        out['sessions'] = [d.isoformat() for d in days]
        vw = session_vwap_sigma(vis)
        out['vwap'] = vw
    except Exception as e:
        bars, e9, e20, vw = [], [], [], None
        out['bars'] = []
        out['bars_error'] = str(e)[:200]

    levels = []
    disp = {}
    for key, period, cnt, lab, color in TFS:
        try:
            hb = kline(period, cnt)
        except Exception as e:
            disp[key] = {'error': str(e)[:160]}
            continue
        bull, bear = last_displacements(hb)
        disp[key] = {'bull': bull, 'bear': bear, 'n': len(hb)}
        if bull:
            levels.append({
                'px': bull['px'], 'name': '%s bull disp %s' % (lab, _fmt_px(bull['px'])),
                'color': color, 'kind': 'disp-bull', 'side': 'bull', 'tf': key,
                'priority': 2, 't': bull['t'],
            })
        if bear:
            levels.append({
                'px': bear['px'], 'name': '%s bear disp %s' % (lab, _fmt_px(bear['px'])),
                'color': color, 'kind': 'disp-bear', 'side': 'bear', 'tf': key,
                'priority': 2, 't': bear['t'],
            })
        if key == 'day':
            try:
                di = completed_bar_index(hb, 'day')
                out['day_king'] = round(typical(hb[di]), 2) if di is not None else None
            except Exception:
                out['day_king'] = None
        if key == 'week':
            try:
                wi = completed_bar_index(hb, 'week')
                out['week_king'] = round(typical(hb[wi]), 2) if wi is not None else None
            except Exception:
                out['week_king'] = None
        if key == '4h':
            try:
                # last completed 4h typical price
                i = -2 if len(hb) >= 2 else -1
                # if the last 4h bar is still forming (its start + 4h > now), use previous
                last_t = _et(hb[-1]['t'])
                if datetime.now(ET) < last_t + timedelta(hours=4) and len(hb) >= 2:
                    i = -2
                out['h4_pivot'] = round(typical(hb[i]), 2)
            except Exception:
                out['h4_pivot'] = None

    dk, wk = out.get('day_king'), out.get('week_king')
    if dk is not None and wk is not None:
        king = round((dk + wk) / 2.0, 2)
        out['day_week_king'] = king
        levels.append({
            'px': king,
            'name': 'Day + Week King %s' % _fmt_px(king),
            'color': '#ffd54f', 'kind': 'king', 'priority': 1,
            'note': '日枢 %s · 周枢 %s（均为已完成周期的 (H+L+C)/3）' % (_fmt_px(dk), _fmt_px(wk)),
        })
    elif dk is not None:
        out['day_week_king'] = dk
        levels.append({'px': dk, 'name': 'Day King %s' % _fmt_px(dk), 'color': '#ffd54f', 'kind': 'king', 'priority': 1})
    elif wk is not None:
        out['day_week_king'] = wk
        levels.append({'px': wk, 'name': 'Week King %s' % _fmt_px(wk), 'color': '#ffd54f', 'kind': 'king', 'priority': 1})
    if out.get('h4_pivot') is not None:
        levels.append({
            'px': out['h4_pivot'],
            'name': '4H %s' % _fmt_px(out['h4_pivot']),
            'color': '#ffe082', 'kind': 'h4', 'priority': 3,
        })

    if vw:
        levels.append({'px': vw['vwap'], 'name': 'VWAP %s' % _fmt_px(vw['vwap']), 'color': '#f5a623', 'kind': 'vwap', 'priority': 4})
        levels.append({'px': vw['upper'], 'name': 'VWAP +1σ %s' % _fmt_px(vw['upper']), 'color': '#b0bec5', 'kind': 'sigma-vwap', 'priority': 5})
        levels.append({'px': vw['lower'], 'name': 'VWAP −1σ %s' % _fmt_px(vw['lower']), 'color': '#b0bec5', 'kind': 'sigma-vwap', 'priority': 5})

    futu = _load_futu_snap()
    out['futu'] = None
    if futu:
        sigma = _f(futu.get('one_sigma_move')) or _f(futu.get('straddle_move'))
        # one_sigma_move is already straddle/sqrt(2/pi); straddle_move is E[|move|] ≈ straddle
        if sigma and last:
            # Prefer true 1σ if present
            sig_px = _f(futu.get('one_sigma_move'))
            if sig_px:
                up, lo = round(last + sig_px, 2), round(last - sig_px, 2)
                levels.append({'px': up, 'name': 'σ 隐含 +1  %s' % _fmt_px(up), 'color': '#90a4ae', 'kind': 'sigma-imp', 'priority': 5})
                levels.append({'px': lo, 'name': 'σ 隐含 −1  %s' % _fmt_px(lo), 'color': '#90a4ae', 'kind': 'sigma-imp', 'priority': 5})
                out['implied_sigma'] = sig_px
        strikes = []
        for r in (futu.get('strikes') or []):
            k = _f(r.get('k'))
            if k is None:
                continue
            oi_c, oi_p = int(r.get('oi_c') or 0), int(r.get('oi_p') or 0)
            if oi_c + oi_p <= 0:
                continue
            side = 'call' if (last is not None and k >= last) or (last is None and oi_c >= oi_p) else 'put'
            strikes.append({
                'k': k, 'oi_c': oi_c, 'oi_p': oi_p, 'gex_oi': r.get('gex_oi'),
                'side': side,
            })
        # Keep the biggest 4 call-OI above and 4 put-OI below (plus walls)
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
                'name': ('%s %s%s%s' % (expl, int(s['k']) if s['k']==int(s['k']) else s['k'], tag, extra)).strip(),
                'color': '#78909c', 'kind': 'odte', 'priority': 8, 'dotted': True,
            })

    groups = _group_levels(levels)
    # distance from last
    lo = min((b['l'] for b in bars), default=None)
    hi = max((b['h'] for b in bars), default=None)
    for g in groups:
        if last is not None:
            g['dist'] = round(g['px'] - last, 2)
        g['offscreen'] = bool(lo is not None and hi is not None and (g['px'] < lo or g['px'] > hi))
        if g['n'] > 1:
            extras = [m['name'] for m in g['members'][1:]]
            g['plus'] = g['n'] - 1
            g['also'] = extras
    out['levels'] = groups
    out['disp'] = disp

    expiry = (out.get('futu') or {}).get('expiry')
    try:
        out['signals'] = _signals(bars, e9, e20, groups, last, expiry)
    except Exception as e:
        out['signals'] = []
        out['signals_error'] = str(e)[:200]

    out['server_time'] = int(time.time())
    out['server_time_bj'] = datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')
    out['rule'] = (
        '信号规则（仅供参考，不会下单）：'
        '① 买入看涨（BUY NOW CALL）：价格重新站上一条多头位移位（上一根 5 分钟K线收在该位下方，本根收在上方），'
        '同时 5 分钟 EMA9 在本根或前 3 根内上穿 EMA20，且该位移位 8 点以内至少还有一条其他关键位'
        '（其他周期位移位、Day+Week King、4H 或 σ）。目标行权价为最接近现价的 5 点一档看涨期权。'
        '② 买入看跌（BUY NOW PUT）对称：空头位移位被跌破，EMA9 下穿 EMA20，同样需要共振。'
        '③ 离场（EXIT）：价格触及下一档反向关键位，或收盘重新穿越进场那条位移位。'
        '位移位定义：该周期 ATR(14) 的 1.5 倍以上实体K线，取最近一次多头/空头位移K线的开盘价'
        '（多头位移=支撑，空头位移=阻力）。'
        'Day+Week King：已完成的上一交易日与上一自然周的典型价格 (H+L+C)/3 的均值。'
        '4H：最近一根已完成的 4 小时K线的 (H+L+C)/3。'
        'σ：当日成交量加权 VWAP±1σ；若富途 0DTE/近月跨式可用，再叠加隐含 1σ（跨式价格/√(2/π)）。'
        '虚线行为富途 SPXW 近月到期、未平仓量最大的几个看涨/看跌行权价。'
    )
    with _lk:
        _c['payload'] = (time.time(), out)
    return out
