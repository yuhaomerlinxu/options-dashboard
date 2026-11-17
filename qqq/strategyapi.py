"""美股期权策略雷达 + 蝶式机会雷达.

Display-only. Longbridge read-only. Does not change /qqq, /echo, /mbot or 新规则 v3.
"""
from __future__ import annotations
import json, math, os, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import echoapi as E

ET = ZoneInfo('America/New_York')
BJ = ZoneInfo('Asia/Shanghai')
DIR = os.path.dirname(os.path.abspath(__file__))
SNAP_DIR = os.path.join(DIR, 'strategy_data')
WEIGHTS_PATH = os.path.join(DIR, 'strategy_weights.json')
os.makedirs(SNAP_DIR, exist_ok=True)

M7 = ['NVDA', 'AAPL', 'MSFT', 'AMZN', 'META', 'GOOGL', 'GOOG', 'TSLA']
SECTORS = ['信息技术', '医疗保健', '金融', '非必需消费', '通信服务', '工业',
           '必需消费', '能源', '公用事业', '房地产', '原材料']


def resolve(sym):
    s = (sym or 'QQQ').strip().upper()
    if s in ('SPX', '.SPX', 'SPX.US', '.SPX.US'):
        return {'id': 'SPX', 'bar': '.SPX.US', 'opt': 'SPY.US', 'scale': 'spy',
                'label': 'SPX', 'step': 5.0}
    if s in ('SPY', 'SPY.US'):
        return {'id': 'SPY', 'bar': 'SPY.US', 'opt': 'SPY.US', 'scale': None,
                'label': 'SPY', 'step': 1.0}
    return {'id': 'QQQ', 'bar': 'QQQ.US', 'opt': 'QQQ.US', 'scale': None,
            'label': 'QQQ', 'step': 1.0}


def _rth_now():
    return E._rth(int(time.time()))


def _ttl(fast, slow):
    return fast if _rth_now() else slow


def _asof(ts=None):
    d = E._et(ts or int(time.time()))
    return d.strftime('%Y-%m-%d %H:%M:%S ET')


def _exps(under):
    today = datetime.now(ET).date().isoformat()
    rows = E._run(['option', 'chain', under], 20) or []
    return [e['expiry_date'] for e in rows if e.get('expiry_date') and e['expiry_date'] >= today]


def _mid_px(row):
    try:
        last = float(row.get('last') or 0)
    except (TypeError, ValueError):
        last = 0.0
    return last if last > 0 else None


def _bs_delta(S, K, iv, T, call):
    if iv <= 0 or T <= 0 or S <= 0 or K <= 0:
        return 0.0
    d1 = (math.log(S / K) + 0.5 * iv * iv * T) / (iv * math.sqrt(T))
    nd = E._ncdf(d1)
    return nd if call else nd - 1.0


def _bs_price(S, K, iv, T, call, r=0.0):
    if S <= 0 or K <= 0 or T <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    if iv <= 0:
        return max(S - K, 0.0) if call else max(K - S, 0.0)
    d1 = (math.log(S / K) + (r + 0.5 * iv * iv) * T) / (iv * math.sqrt(T))
    d2 = d1 - iv * math.sqrt(T)
    df = math.exp(-r * T)
    if call:
        return S * E._ncdf(d1) - K * df * E._ncdf(d2)
    return K * df * E._ncdf(-d2) - S * E._ncdf(-d1)


def _bars(symbol, period='1m', count=500, days=1, session='intraday'):
    raw = E._run(['kline', symbol, '--period', period, '--count', str(count), '--session', session], 45)
    out = []
    for b in raw or []:
        t = E._ts(b['time'])
        if session == 'intraday' and not E._rth(t):
            continue
        out.append({
            't': t, 'o': float(b['open']), 'h': float(b['high']),
            'l': float(b['low']), 'c': float(b['close']),
            'v': float(b.get('volume') or 0),
        })
    if days and out:
        seen = []
        for b in out:
            d = str(E._et(b['t']).date())
            if d not in seen:
                seen.append(d)
        keep = set(seen[-days:])
        out = [b for b in out if str(E._et(b['t']).date()) in keep]
    return out


def _quote_batch(symbols, chunk=40):
    out = {}
    for i in range(0, len(symbols), chunk):
        part = symbols[i:i + chunk]
        try:
            rows = E._run(['quote'] + part, 25) or []
        except Exception:
            rows = []
            for s in part:
                try:
                    rows += E._run(['quote', s], 12) or []
                except Exception:
                    pass
        for r in rows:
            sym = (r.get('symbol') or '').replace('.US', '')
            try:
                last = float(r['last'])
                prev = float(r.get('prev_close') or last)
            except (TypeError, ValueError, KeyError):
                continue
            chg = last - prev
            pct = (last / prev - 1) * 100 if prev else 0
            out[sym] = {
                'last': last, 'prev': prev, 'chg': chg, 'pct': pct,
                'high': float(r['high'] or 0) or None,
                'low': float(r['low'] or 0) or None,
                'open': float(r['open'] or 0) or None,
            }
    return out


def _option_quotes(under, expiry, spot, window=0.10):
    ch = E._run(['option', 'chain', under, '--date', expiry], 25) or []
    ks = [float(r['strike']) for r in ch if r.get('standard', 'true') == 'true']
    ks = [k for k in ks if abs(k / spot - 1) <= window]
    cons = [E._occ(under, expiry, cp, k) for k in ks for cp in 'CP']
    rows = []
    for i in range(0, len(cons), 50):
        try:
            rows += E._run(['option', 'quote'] + cons[i:i + 50], 35) or []
        except Exception:
            pass
    return rows, ks


def _greeks(rows, spot, expiry, scale=1.0):
    """Per-contract greeks, then aggregate by (scaled) strike."""
    T = E._tte(expiry)
    by = {}
    used = 0
    for x in rows:
        try:
            k = float(x['strike_price'])
            oi = int(x.get('open_interest') or 0)
            vol = float(x.get('volume') or 0)
            iv = float(x.get('implied_volatility') or 0)
            cp = (x.get('direction') or 'C')[0].upper()
            last = _mid_px(x)
        except (TypeError, ValueError, KeyError):
            continue
        if iv <= 0 and last is None:
            continue
        g = E.bs_gamma(spot, k, iv, T) if iv > 0 else 0.0
        dlt = _bs_delta(spot, k, iv, T, cp == 'C') if iv > 0 else 0.0
        sign = 1 if cp == 'C' else -1
        gex_oi = oi * g * 100 * spot * sign
        gex_vol = vol * g * 100 * spot * sign
        dex = oi * dlt * 100 * spot
        pain_call = oi * max(0, 0)  # filled later
        sk = k * scale
        rec = by.setdefault(sk, {
            'k': round(sk, 2), 'gex_oi': 0.0, 'gex_vol': 0.0, 'dex': 0.0,
            'oi_c': 0, 'oi_p': 0, 'vol_c': 0, 'vol_p': 0,
            'iv_c': None, 'iv_p': None, 'last_c': None, 'last_p': None,
            'raw_k': k,
        })
        rec['gex_oi'] += gex_oi
        rec['gex_vol'] += gex_vol
        rec['dex'] += dex
        if cp == 'C':
            rec['oi_c'] += oi
            rec['vol_c'] += vol
            rec['iv_c'] = iv if iv > 0 else rec['iv_c']
            rec['last_c'] = last
        else:
            rec['oi_p'] += oi
            rec['vol_p'] += vol
            rec['iv_p'] = iv if iv > 0 else rec['iv_p']
            rec['last_p'] = last
        used += 1
    # max pain uses raw (unscaled) strikes on the option underlying
    return by, used, T


def _max_pain(rows, expiry_spot_scale=1.0):
    # Compute on raw option strikes, then scale the strike label.
    from collections import defaultdict
    legs = []
    for x in rows:
        try:
            legs.append((float(x['strike_price']), (x.get('direction') or 'C')[0].upper(),
                         int(x.get('open_interest') or 0)))
        except (TypeError, ValueError, KeyError):
            continue
    ks = sorted({k for k, _, _ in legs})
    if not ks:
        return None
    def pain(P):
        s = 0.0
        for k, cp, oi in legs:
            if cp == 'C':
                s += oi * max(0, P - k)
            else:
                s += oi * max(0, k - P)
        return s
    k0 = min(ks, key=pain)
    return round(k0 * expiry_spot_scale, 2)


def _zero_gamma(by, spot):
    """Sweep net GEX (OI) as a function of moneyness using current per-strike net, sign-change."""
    if not by:
        return None
    ks = sorted(by)
    # Use current net GEX by strike; flip = midpoint of nearest sign change around spot.
    flip = None
    for a, b in zip(ks, ks[1:]):
        ga, gb = by[a]['gex_oi'], by[b]['gex_oi']
        if ga == 0 or ga * gb < 0:
            cand = (a + b) / 2
            if flip is None or abs(cand - spot) < abs(flip - spot):
                flip = cand
    return round(flip, 2) if flip is not None else None


def _profile(bars, step):
    if not bars:
        return {'poc': None, 'vwap': None, 'vwas_long': None, 'vwas_short': None,
                'high': None, 'low': None, 'session_open': None, 'vertex5': None,
                'path': [], 'running': []}
    # bucket volume
    bucket = {}
    pv = vv = 0.0
    up_pv = up_v = dn_pv = dn_v = 0.0
    running = []
    mx_5 = None
    # resample 5m max-volume node
    b5 = {}
    for b in bars:
        typ = (b['h'] + b['l'] + b['c']) / 3
        pv += typ * b['v']
        vv += b['v']
        key = round(typ / step) * step
        bucket[key] = bucket.get(key, 0) + b['v']
        if b['c'] >= b['o']:
            up_pv += typ * b['v']
            up_v += b['v']
        else:
            dn_pv += typ * b['v']
            dn_v += b['v']
        t5 = b['t'] - (b['t'] % 300)
        rec = b5.setdefault(t5, {'v': 0, 'typ': 0, 'c': b['c']})
        rec['v'] += b['v']
        rec['typ'] += typ * b['v']
        rec['c'] = b['c']
        poc = max(bucket, key=bucket.get) if bucket else typ
        vwap = pv / vv if vv else typ
        running.append({'t': b['t'], 'spot': b['c'], 'vwap': round(vwap, 4), 'poc': round(poc, 2)})
    if b5:
        best = max(b5.values(), key=lambda r: r['v'])
        mx_5 = best['typ'] / best['v'] if best['v'] else best['c']
    return {
        'poc': round(max(bucket, key=bucket.get), 2) if bucket else None,
        'vwap': round(pv / vv, 4) if vv else None,
        'vwas_long': round(up_pv / up_v, 2) if up_v else None,
        'vwas_short': round(dn_pv / dn_v, 2) if dn_v else None,
        'high': max(b['h'] for b in bars),
        'low': min(b['l'] for b in bars),
        'session_open': bars[0]['o'],
        'vertex5': round(mx_5, 2) if mx_5 else None,
        'path': [{'t': b['t'], 'c': b['c']} for b in bars],
        'running': running,
    }


def _em(spot, iv, dte_td=1):
    if not spot or not iv:
        return None
    return float(spot) * float(iv) * math.sqrt(max(dte_td, 1) / 252.0)


def _heuristics(spot, levels, em, bars, by):
    major_pos = levels.get('major_pos')
    major_neg = levels.get('major_neg')
    z = levels.get('zero_gamma')
    magnet = levels.get('core_magnet') or major_pos or spot
    em = em or max(abs((levels.get('high') or spot) - (levels.get('low') or spot)) / 2, spot * 0.004)
    dist = abs(spot - magnet) / em
    # Dealer long gamma near spot => pin (positive). Short gamma => unstable (negative).
    near = [v for k, v in by.items() if abs(k - magnet) <= em * 0.5]
    net_near = sum(v['gex_oi'] for v in near)
    tot = sum(abs(v['gex_oi']) for v in by.values()) or 1.0
    conc = sum(abs(v['gex_oi']) for v in near) / tot
    pin = 100 * math.tanh(net_near / (tot * 0.15 + 1)) * max(0.0, 1 - dist)
    pin = max(-100, min(100, pin))
    magnet_pct = min(99, max(5, int(round(100 * conc * max(0.4, 1 - 0.5 * dist)))))
    call_g = sum(max(v['gex_oi'], 0) for v in by.values())
    put_g = sum(max(-v['gex_oi'], 0) for v in by.values())
    energy = call_g / put_g if put_g > 1 else (2.0 if call_g > 0 else 1.0)
    # Dealer pressure: positive = dealers short / bid for upside squeeze; negative = long gamma suppressing.
    pressure = round(-(net_near / (tot * 0.2 + 1)), 2)
    # Close zone around support/resistance or magnet ± 0.25 em
    sup = levels.get('support')
    res = levels.get('resist')
    if sup is None or res is None:
        sup = round(min(spot, magnet) - 0.15 * em, 2)
        res = round(max(spot, magnet) + 0.15 * em, 2)
    zone = [round(min(sup, magnet - 0.2 * em), 2), round(max(res, magnet + 0.2 * em), 2)]
    # tail 10 min lock
    lock = None
    if bars and len(bars) >= 10:
        tail = bars[-10:]
        tr = max(b['h'] for b in tail) - min(b['l'] for b in tail)
        lock = tr <= 0.25 * em
    # gamma squeeze: approaching major with tight range
    squeeze = '平台接受' if dist < 0.35 and conc > 0.15 else ('关注' if dist < 0.7 else '远离磁铁')
    squeeze_pct = int(round(100 * max(0, 1 - dist) * min(1, conc * 3)))
    # phase A: core magnet; B: max pain or POC
    phase_a = magnet
    phase_b = levels.get('max_pain') or levels.get('poc') or magnet
    # last 10m / 15m path for "锁定"
    last_t = bars[-1]['t'] if bars else None
    phase_a_t = E._et(last_t).strftime('%H:%M') if last_t else ''
    # energy label
    bias = '多方偏强' if energy >= 1.15 else ('空方偏强' if energy <= 0.87 else '多空均衡')
    if magnet_pct >= 70 and dist < 0.5:
        macro = '做市商磁铁控盘'
    elif net_near < 0 and dist > 0.4:
        macro = '负伽马放大波动'
    else:
        macro = '结构博弈'
    pin_lbl = '偏稳定' if pin >= 35 else ('偏不稳' if pin <= -35 else '中性')
    tail_txt = '锁价仍有效' if lock else '锁价未形成'
    return {
        'pin': round(pin, 0),
        'pin_label': pin_lbl,
        'macro': macro,
        'magnet_pct': magnet_pct,
        'energy': round(energy, 2),
        'energy_bias': bias,
        'pressure': pressure,
        'dyn_pivot': round(levels.get('dyn_pivot') or magnet, 2),
        'zone': zone,
        'phase_a': round(phase_a, 2),
        'phase_a_time': phase_a_t,
        'phase_b': round(phase_b, 2) if phase_b else None,
        'close_zone': zone,
        'tail_lock': lock,
        'tail_text': tail_txt,
        'tail_anchor': round(magnet, 2),
        'squeeze': squeeze,
        'squeeze_pct': squeeze_pct,
        'formulas': {
            'pin': '钉盘稳定 = 100×tanh(近端净GEX / 总|GEX|)×(1−距磁铁/预期波动)。正值表示经销商正伽马、价格更易钉住磁铁；负值表示负伽马、波动更容易被放大。',
            'macro': '磁铁控盘% = 最接近的大额GEX附近集中度 × 距离衰减。多空能量 = 正GEX合计 / 负GEX合计（>1 多方偏强）。',
            'pressure': 'Dealer Pressure = −近端净GEX/总|GEX|。负值偏压制（经销商正伽马），正值偏助推。',
            'phase': 'Phase-A 取核心磁铁（|GEX|最大行权价）；Phase-B 取 Max Pain，如无则取当日 POC。大概率收盘区为结构支撑/阻力与磁铁邻域的并集。',
            'tail': '尾盘10分钟：若最近10根1分钟K线的高低波幅 ≤ 0.25×预期波动，视为锁价仍有效。',
            'squeeze': 'Gamma 挤压强度按「距磁铁远近 × 集中度」估算，仅为启发式标签。',
        },
    }


def _load_weights():
    with open(WEIGHTS_PATH, encoding='utf-8') as f:
        return json.load(f)


def _compass(spx_q, spx_bars, quotes, weights):
    prev = spx_q['prev_close']
    last = spx_q.get('rth_last') or (spx_bars[-1]['c'] if spx_bars else spx_q['last'])
    chg = last - prev
    pct = (last / prev - 1) * 100 if prev else 0
    # 15m / 30m from 1m bars
    def ago(minutes):
        if not spx_bars:
            return None
        t0 = spx_bars[-1]['t'] - minutes * 60
        prevb = next((b for b in reversed(spx_bars) if b['t'] <= t0), spx_bars[0])
        return last - prevb['c']
    m15, m30 = ago(15), ago(30)
    sess_h = max(b['h'] for b in spx_bars) if spx_bars else last
    from_high = last - sess_h
    # reclaim: session VWAP or prior close
    typ_pv = sum(((b['h'] + b['l'] + b['c']) / 3) * b['v'] for b in spx_bars) if spx_bars else 0
    typ_v = sum(b['v'] for b in spx_bars) if spx_bars else 0
    vwap = typ_pv / typ_v if typ_v else None
    reclaim = vwap if vwap else prev

    cons = weights['constituents']
    rows = []
    for c in cons:
        q = quotes.get(c['ticker'])
        if not q:
            continue
        pts = c['weight'] * (q['pct'] / 100.0) * prev
        rows.append({
            'ticker': c['ticker'], 'name': c['name'], 'sector': c['sector'],
            'weight': c['weight'], 'last': q['last'], 'pct': round(q['pct'], 2),
            'pts': round(pts, 2), 'high': q.get('high'), 'low': q.get('low'),
        })
    cov_w = sum(c['weight'] for c in cons)
    got_w = sum(r['weight'] for r in rows)
    up = [r for r in rows if r['pts'] > 0]
    dn = [r for r in rows if r['pts'] < 0]
    flat = [r for r in rows if r['pts'] == 0]
    m7 = [r for r in rows if r['ticker'] in M7]
    # keep M7 order
    m7 = [next((r for r in m7 if r['ticker'] == t), None) for t in M7]
    m7 = [r for r in m7 if r]
    sectors = []
    for s in SECTORS:
        grp = [r for r in rows if r['sector'] == s]
        if not grp:
            continue
        w = sum(r['weight'] for r in grp)
        pts = sum(r['pts'] for r in grp)
        # sector % ≈ pts / (w * prev)
        spct = (pts / (w * prev) * 100) if w and prev else 0
        lead = min(grp, key=lambda r: r['pts']) if pts < 0 else max(grp, key=lambda r: r['pts'])
        sectors.append({
            'sector': s, 'weight': round(w, 4), 'pct': round(spct, 2), 'pts': round(pts, 2),
            'up': sum(1 for r in grp if r['pct'] > 0),
            'dn': sum(1 for r in grp if r['pct'] < 0),
            'lead': lead['ticker'] if lead else None,
            'lead_pts': lead['pts'] if lead else 0,
            'names': sorted(grp, key=lambda r: -abs(r['pts'])),
        })
    # headline
    if chg < 0 and (m15 or 0) > 0:
        headline, tone = '下跌后回升', 'mixed'
        risk = '修复仍待确认'
        explain = '主要拖累来自权重科技股，但最近15分钟出现回升。'
    elif chg < 0 and (m15 or 0) < 0:
        headline, tone = '弱势延续', 'down'
        risk = '顺势看空需谨慎追'
        explain = '指数收跌且短线仍未扭转。'
    elif chg > 0 and (m15 or 0) > 0:
        headline, tone = '高位延续', 'up'
        risk = '注意回吐'
        explain = '指数收涨，短线仍偏强。'
    elif chg > 0:
        headline, tone = '冲高回落', 'mixed'
        risk = '修复/回吐待确认'
        explain = '指数收涨，但最近一段时间有回吐。'
    else:
        headline, tone = '震荡整理', 'flat'
        risk = '方向不明'
        explain = '涨跌幅度有限。'
    if vwap and last < vwap and (m15 or 0) > 0:
        risk = '修复仍待确认'
        explain += ' 反弹尚未站上当日均价。'
    top_dn = sorted(rows, key=lambda r: r['pts'])[:3]
    if top_dn and chg < 0:
        explain = '、'.join(r['ticker'] for r in top_dn[:2]) + ' 是已覆盖股票的主要拖累。' + (
            '最近15分钟出现回升。' if (m15 or 0) > 0 else ''
        )

    # next watch: largest 15m contrib if we have highs/lows only as fallback use day pts of top abs among recent bounce
    # Without per-name 15m, use names with largest absolute day pts that also have a bounce (last near high or low).
    watch = []
    cands = sorted(rows, key=lambda r: abs(r['pts']), reverse=True)[:12]
    for r in cands:
        # 15m proxy: if last is closer to high than mid, treat as 上推
        if r.get('high') and r.get('low') and r['high'] > r['low']:
            pos = (r['last'] - r['low']) / (r['high'] - r['low'])
            bounce = pos > 0.6 and r['pts'] < 0 or pos > 0.7
            push = '上推' if (r['last'] - (r.get('low') or r['last'])) > ((r.get('high') or r['last']) - r['last']) * 0.8 or pos >= 0.55 else '下压'
        else:
            push = '上推' if r['pct'] >= 0 else '下压'
            pos = 0.5
        # break levels: tight around last using 0.3% or day range/8
        rng = (r['high'] - r['low']) if r.get('high') and r.get('low') else r['last'] * 0.006
        up_l = round(r['last'] + max(rng * 0.08, r['last'] * 0.002), 2)
        dn_l = round(r['last'] - max(rng * 0.08, r['last'] * 0.002), 2)
        # 15m contrib estimate: day pts * fraction of range position minus 0.5, rough
        c15 = round(r['pts'] * (pos - 0.5) * 0.8, 2)
        watch.append({**r, 'push': push, 'up': up_l, 'down': dn_l, 'c15': c15, 'approx_15m': True})
    # Prefer bounce-from-red names (day down, late up) first, else top abs
    watch = sorted(watch, key=lambda r: (-1 if r['pts'] < 0 and r['push'] == '上推' else 0, -abs(r.get('c15') or 0), -abs(r['pts'])))[:3]
    if all(w['push'] == '上推' for w in watch) and (m15 or 0) > 0:
        cond = '短线回升，但需站上关键位才能确认修复'
    elif all(w['push'] == '下压' for w in watch):
        cond = '权重股同步下压，方向偏空'
    else:
        cond = '暂无清晰方向优势'

    return {
        'last': round(last, 2),
        'prev': round(prev, 2),
        'chg': round(chg, 2),
        'pct': round(pct, 2),
        'headline': headline,
        'tone': tone,
        'explain': explain.strip(),
        'risk': risk,
        'ref_close': round(levels_round := (round(last / 5) * 5 if last > 1000 else round(last)), 2) if False else None,
        'm15': None if m15 is None else round(m15, 2),
        'm30': None if m30 is None else round(m30, 2),
        'from_high': round(from_high, 2),
        'reclaim': round(reclaim, 2) if reclaim else None,
        'vwap': round(vwap, 2) if vwap else None,
        'breadth': {
            'n': len(rows), 'up': len(up), 'dn': len(dn), 'flat': len(flat),
            'up_pts': round(sum(r['pts'] for r in up), 2),
            'dn_pts': round(sum(r['pts'] for r in dn), 2),
            'coverage': round(got_w * 100, 1),
            'note': '估算：贡献=估算权重×涨跌幅×SPX前收。样本约前 %d 只，覆盖 %.1f%% 权重，不是全市场 503 只。' % (len(rows), got_w * 100),
        },
        'm7': m7,
        'm7_pts': round(sum(r['pts'] for r in m7), 2),
        'm7_share': round(sum(abs(r['pts']) for r in m7) / (sum(abs(r['pts']) for r in rows) or 1) * 100, 1),
        'sectors': sectors,
        'drivers': sorted(rows, key=lambda r: -abs(r['pts']))[:8],
        'watch': watch,
        'condition': cond,
        'asof_date': str(E._et(spx_bars[-1]['t']).date()) if spx_bars else datetime.now(ET).date().isoformat(),
        'formulas': {
            'pts': '个股折算SPX点数 ≈ 估算权重 × 当日涨跌幅 × SPX前收。权重为静态估算，不是官方指数权重。',
            'm15': '近15/30分钟涨跌直接用 SPX 1分钟K线收盘差，不是成分估算。',
            'watch': '「接下来盯谁」优先选取当日拖累较大、但价格已靠近当日高位的权重股。15分钟贡献在周末仅用当日高低位置做近似，开盘后会改为真实15分钟涨跌。上破/下破为最近波动的近端观察位。',
        },
    }


def _snap_path(sym, day):
    return os.path.join(SNAP_DIR, '%s_%s.jsonl' % (sym, day))


def _load_snaps(sym, day):
    p = _snap_path(sym, day)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return out


def _append_snap(sym, day, rec):
    p = _snap_path(sym, day)
    with open(p, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec, ensure_ascii=False) + '\n')


def _levels_from(by, spot, profile, max_pain, scale_note=None):
    if not by:
        return {}
    ks = sorted(by)
    major_pos = max(ks, key=lambda k: by[k]['gex_oi'])
    major_neg = min(ks, key=lambda k: by[k]['gex_oi'])
    core = max(ks, key=lambda k: abs(by[k]['gex_oi']))
    # support: nearest significant positive GEX below spot; resist above
    below = [k for k in ks if k <= spot and by[k]['gex_oi'] > 0]
    above = [k for k in ks if k >= spot and by[k]['gex_oi'] > 0]
    # fallback: largest |gex| below/above
    if not below:
        below = [k for k in ks if k <= spot]
    if not above:
        above = [k for k in ks if k >= spot]
    support = max(below) if below else None
    resist = min(above) if above else None
    # VWAS from option volume
    def vwas(sign):
        num = den = 0.0
        for k, v in by.items():
            w = (v['vol_c'] + v['vol_p'])
            if sign > 0 and v['gex_oi'] <= 0:
                continue
            if sign < 0 and v['gex_oi'] >= 0:
                continue
            num += k * w
            den += w
        return round(num / den, 2) if den else None
    zg = _zero_gamma(by, spot)
    tot_oi = sum(v['gex_oi'] for v in by.values())
    tot_vol = sum(v['gex_vol'] for v in by.values())
    tot_dex = sum(v['dex'] for v in by.values())
    peak = max(ks, key=lambda k: abs(by[k]['gex_oi']))
    # dyn pivot: volume-weighted strike of all options near spot
    num = den = 0.0
    for k, v in by.items():
        w = v['vol_c'] + v['vol_p'] + 0.1 * (v['oi_c'] + v['oi_p'])
        num += k * w
        den += w
    dyn = round(num / den, 2) if den else core
    return {
        'major_pos': round(major_pos, 2),
        'major_neg': round(major_neg, 2),
        'core_magnet': round(core, 2),
        'gamma_peak': round(peak, 2),
        'zero_gamma': zg,
        'support': round(support, 2) if support is not None else None,
        'resist': round(resist, 2) if resist is not None else None,
        'vwas_long': vwas(1) or profile.get('vwas_long'),
        'vwas_short': vwas(-1) or profile.get('vwas_short'),
        'max_pain': max_pain,
        'poc': profile.get('poc'),
        'vertex5': profile.get('vertex5'),
        'vwap': profile.get('vwap'),
        'dyn_pivot': dyn,
        'gex_oi_total': tot_oi,
        'gex_vol_total': tot_vol,
        'dex_total': tot_dex,
        'high': profile.get('high'),
        'low': profile.get('low'),
    }


def _atm_iv(by, spot):
    if not by:
        return None
    k0 = min(by, key=lambda k: abs(k - spot))
    rec = by[k0]
    ivs = [v for v in (rec.get('iv_c'), rec.get('iv_p')) if v]
    return sum(ivs) / len(ivs) if ivs else None


def _smile(by):
    rows = []
    for k in sorted(by):
        rec = by[k]
        if rec.get('iv_c') or rec.get('iv_p'):
            rows.append({'k': rec['k'], 'call': rec.get('iv_c'), 'put': rec.get('iv_p')})
    return rows


def _gex_bars(by, top=40):
    ks = sorted(by, key=lambda k: -abs(by[k]['gex_oi']))[:top]
    ks = sorted(ks)
    return [{'k': round(k, 2), 'gex_oi': by[k]['gex_oi'], 'gex_vol': by[k]['gex_vol'],
             'dex': by[k]['dex'], 'oi': by[k]['oi_c'] + by[k]['oi_p'],
             'vol': by[k]['vol_c'] + by[k]['vol_p']} for k in ks]


SESSION_SEC = 6.5 * 3600  # 09:30–16:00 ET

FORMULA_CLOSE_ZH = (
    '收盘预估 = w现价×现价 + w磁铁×GEX核心磁铁 + w痛点×Max Pain + w支点×（动态支点或当日POC）。'
    'w现价 = 1 − √(距收盘剩余交易时间 / 6.5 小时全时段)，限制在 20%–90%；尾盘 5 分钟提到 95%。'
    '盘前、收盘后和周末按“整段交易时段尚未开始”处理，现价权重因此约 20%。'
    '余下权重在磁铁、Max Pain 和支点之间按距离衰减分配：距离相对收盘前预期波动越远，权重越小（衰减 e^(−0.7×距离/预期波动)）。'
    '磁铁附近净 GEX 为负时，钉住不可靠，结构项整体再打 0.55，多出的权重加回现价。'
    '50% / 75% 区间 = 预估 ± 0.67 / 1.15 × 收盘前预期波动；预期波动 = 现价 × ATM隐含波动率 × √(剩余交易时间 / (252×6.5小时))。'
    '现价取常规时段最新价；盘后/盘前用上一交易日收盘。SPX 的磁铁和 Max Pain 来自 SPY 期权按比例换算。这是结构估计，不是保证。'
)


def _session_remaining(now=None):
    """Return (remaining_sec, session_sec, in_rth). Outside RTH / before open / weekend: full session remaining."""
    now = now or datetime.now(ET)
    sess = SESSION_SEC
    if now.weekday() >= 5:
        return sess, sess, False
    open_t = now.replace(hour=9, minute=30, second=0, microsecond=0)
    close_t = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now < open_t or now >= close_t:
        return sess, sess, False
    rem = (close_t - now).total_seconds()
    return max(0.0, rem), sess, True


def _spot_weight(t_rem, t_sess):
    if t_sess <= 0:
        return 1.0
    if t_rem <= 0:
        return 1.0
    if t_rem <= 5 * 60:
        return 0.95
    raw = 1.0 - math.sqrt(t_rem / t_sess)
    return min(0.90, max(0.20, raw))


def _dist_decay(spot, level, em):
    if level is None or em is None or em <= 0:
        return 0.0
    return math.exp(-0.7 * abs(float(spot) - float(level)) / float(em))


def _net_near_gex(by, magnet, em):
    if not by or magnet is None or not em:
        return 0.0, 0.0
    near = [v for k, v in by.items() if abs(k - magnet) <= em * 0.5]
    net = sum(v['gex_oi'] for v in near)
    tot = sum(abs(v['gex_oi']) for v in by.values()) or 1.0
    return net, tot


def blend_close_estimate(spot, levels, by=None, iv=None, now=None, asof_ts=None, scale_note=None):
    """Blend RTH spot, GEX magnet, Max Pain and dyn_pivot/POC into a close estimate."""
    now = now or datetime.now(ET)
    t_rem, t_sess, in_rth = _session_remaining(now)
    # expected move to the coming RTH close
    iv = float(iv) if iv else None
    em_daily = _em(spot, iv, 1) if iv else None
    frac = math.sqrt(t_rem / t_sess) if t_sess else 1.0
    em_close = (em_daily * frac) if em_daily else (abs(spot) * 0.006)
    magnet = (levels or {}).get('core_magnet')
    max_pain = (levels or {}).get('max_pain')
    # Max Pain at the edge of the fetched strike window is usually an artifact of a truncated chain.
    if by and max_pain is not None:
        ks = sorted(by)
        if ks and (max_pain >= ks[-1] * 0.995 or max_pain <= ks[0] * 1.005):
            max_pain = None
    pivot = (levels or {}).get('dyn_pivot') or (levels or {}).get('poc')
    pivot_name = '动态支点' if (levels or {}).get('dyn_pivot') is not None else 'POC'

    net_near, tot_gex = _net_near_gex(by or {}, magnet, em_close)
    pin_scale = 0.55 if net_near < 0 else 1.0

    w_spot0 = _spot_weight(t_rem, t_sess)
    w_struct_budget = (1.0 - w_spot0) * pin_scale
    w_spot = 1.0 - w_struct_budget

    parts = [
        ('GEX 核心磁铁', 'magnet', magnet, _dist_decay(spot, magnet, em_close), 1.0),
        ('Max Pain', 'max_pain', max_pain, _dist_decay(spot, max_pain, em_close), 1.0),
        (pivot_name, 'pivot', pivot, _dist_decay(spot, pivot, em_close), 0.5),
    ]
    raw = [(n, k, v, d * base) for n, k, v, d, base in parts if v is not None and d * base > 1e-9]
    sraw = sum(r[3] for r in raw)
    components = [{'name': '现价', 'key': 'spot', 'value': round(float(spot), 2),
                   'weight': round(w_spot, 4), 'decay': 1.0}]
    if sraw <= 0 or w_struct_budget <= 0:
        w_spot = 1.0
        components[0]['weight'] = 1.0
        est = float(spot)
    else:
        est = w_spot * float(spot)
        for n, k, v, rw in raw:
            w = w_struct_budget * (rw / sraw)
            components.append({'name': n, 'key': k, 'value': round(float(v), 2),
                               'weight': round(w, 4), 'decay': round(rw, 4)})
            est += w * float(v)
        # leftover rounding to spot
        wsum = sum(c['weight'] for c in components)
        if abs(wsum - 1) > 1e-6:
            components[0]['weight'] = round(components[0]['weight'] + (1 - wsum), 4)

    lo50, hi50 = est - 0.67 * em_close, est + 0.67 * em_close
    lo75, hi75 = est - 1.15 * em_close, est + 1.15 * em_close
    ts = asof_ts or int(now.timestamp())
    asof_dt = datetime.fromtimestamp(ts, ET) if asof_ts else now
    return {
        'estimate': round(est, 2),
        'range_50': [round(lo50, 2), round(hi50, 2)],
        'range_75': [round(lo75, 2), round(hi75, 2)],
        'em_close': round(em_close, 2),
        'atm_iv': None if iv is None else round(iv, 4),
        'spot': round(float(spot), 2),
        'components': components,
        'w_spot': round(w_spot, 4),
        'pin_scale': pin_scale,
        'net_near_gex': round(net_near, 2),
        't_remaining_sec': int(t_rem),
        't_session_sec': int(t_sess),
        'in_rth': in_rth,
        'asof_bj': asof_dt.astimezone(BJ).strftime('%Y-%m-%d %H:%M:%S'),
        'asof_et': asof_dt.strftime('%Y-%m-%d %H:%M:%S ET'),
        'computed_bj': datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S'),
        'formula': FORMULA_CLOSE_ZH,
        'note': scale_note,
    }



def _quiet_stdio():
    """Silence C-level stdout/stderr (the Longport SDK prints a quote-level table)."""
    import os
    class _G:
        def __enter__(self):
            self._null = os.open(os.devnull, os.O_WRONLY)
            self._o1, self._o2 = os.dup(1), os.dup(2)
            os.dup2(self._null, 1)
            os.dup2(self._null, 2)
            return self
        def __exit__(self, *exc):
            os.dup2(self._o1, 1)
            os.dup2(self._o2, 2)
            os.close(self._o1)
            os.close(self._o2)
            os.close(self._null)
            return False
    return _G()


def _sdk_ctx():
    """Optional Longport Python SDK (read-only). Used when the CLI wrapper is unavailable."""
    from longport.openapi import Config, QuoteContext
    ctx = getattr(_sdk_ctx, '_ctx', None)
    if ctx is None:
        with _quiet_stdio():
            ctx = QuoteContext(Config.from_apikey_env())
        _sdk_ctx._ctx = ctx
    return ctx


def _sdk_ts(ts):
    if hasattr(ts, 'timestamp'):
        return int(ts.timestamp())
    s = str(ts).replace('Z', '+00:00')
    return int(datetime.fromisoformat(s).timestamp())


def _sdk_quote(symbol):
    q = _sdk_ctx().quote([symbol])[0]
    last = float(q.last_done)
    prev = float(q.prev_close or last)
    return {
        'last': last, 'prev_close': prev,
        'open': float(q.open) if q.open is not None else None,
        'high': float(q.high) if q.high is not None else None,
        'low': float(q.low) if q.low is not None else None,
        'change': last - prev,
        'change_pct': (last / prev - 1) * 100 if prev else 0,
    }


def _sdk_bars(symbol, count=500):
    from longport.openapi import Period, AdjustType
    raw = _sdk_ctx().candlesticks(symbol, Period.Min_1, count, AdjustType.NoAdjust) or []
    out = []
    for b in raw:
        sess = str(getattr(b, 'trade_session', '') or '')
        if sess and 'Intraday' not in sess:
            continue
        out.append({
            't': _sdk_ts(b.timestamp),
            'o': float(b.open), 'h': float(b.high),
            'l': float(b.low), 'c': float(b.close),
            'v': float(b.volume or 0),
        })
    # keep last RTH day
    if out:
        last_d = str(E._et(out[-1]['t']).date())
        out = [b for b in out if str(E._et(b['t']).date()) == last_d]
    return out


def _sdk_exps(under):
    today = datetime.now(ET).date()
    rows = _sdk_ctx().option_chain_expiry_date_list(under) or []
    out = []
    for d in rows:
        if hasattr(d, 'isoformat'):
            iso = d.isoformat()
        else:
            iso = str(d)[:10]
        if iso >= today.isoformat():
            out.append(iso)
    return out


def _sdk_option_quotes(under, expiry, spot, window=0.10):
    d = datetime.fromisoformat(expiry).date()
    info = _sdk_ctx().option_chain_info_by_date(under, d) or []
    picks = []
    for r in info:
        if hasattr(r, 'standard') and not r.standard:
            continue
        k = float(r.price)
        if abs(k / spot - 1) > window:
            continue
        picks.append(r)
    cons = []
    for r in picks:
        cons.append(r.call_symbol)
        cons.append(r.put_symbol)
    rows, ks = [], [float(r.price) for r in picks]
    ctx = _sdk_ctx()
    for i in range(0, len(cons), 50):
        try:
            part = ctx.option_quote(cons[i:i + 50]) or []
        except Exception:
            part = []
        for q in part:
            iv = float(q.implied_volatility or 0)
            if iv > 2:  # percent quoted
                iv /= 100.0
            direction = str(q.direction or '')
            rows.append({
                'strike_price': float(q.strike_price),
                'open_interest': int(q.open_interest or 0),
                'volume': float(q.volume or 0),
                'implied_volatility': iv,
                'direction': direction,
                'last': float(q.last_done) if q.last_done is not None else None,
            })
    return rows, ks


def _cli_or_sdk(cli_fn, sdk_fn):
    try:
        return cli_fn()
    except Exception as cli_err:
        try:
            with _quiet_stdio():
                return sdk_fn()
        except Exception:
            raise cli_err


def close_estimate(sym='SPX', expiry=None):
    """Fetch market data (same caches as /strategy) and return a close-estimate dict.

    Prefers lbsafe/Longbridge CLI (as on Render). If the CLI is unavailable, falls
    back to the official Longport Python SDK using LONGPORT_* env vars. Read-only.
    """
    spec = resolve(sym)
    rth = _rth_now()
    q = _cli_or_sdk(
        lambda: E._cached(('st-q', spec['bar']), _ttl(5, 60), lambda: E._quote(spec['bar'])),
        lambda: _sdk_quote(spec['bar']))
    bars = _cli_or_sdk(
        lambda: E._cached(('st-1m', spec['bar']), _ttl(8, 120), lambda: _bars(spec['bar'], '1m', 500, 1)),
        lambda: _sdk_bars(spec['bar']))
    last = bars[-1]['c'] if bars else q['last']
    if rth:
        last = q['last']
    last_t = bars[-1]['t'] if bars else int(time.time())
    if spec['scale'] == 'spy':
        spy = _cli_or_sdk(
            lambda: E._cached('st-q-spy', _ttl(5, 60), lambda: E._quote('SPY.US')),
            lambda: _sdk_quote('SPY.US'))
        scale = last / spy['last']
        spot_opt = spy['last']
        scale_note = 'SPX 模式用 SPY 期权按 SPX/SPY=%.4f 换算' % scale
    else:
        scale, spot_opt = 1.0, last
        scale_note = None
    exps = _cli_or_sdk(
        lambda: E._cached(('st-exps', spec['opt']), _ttl(60, 1800), lambda: _exps(spec['opt'])),
        lambda: _sdk_exps(spec['opt']))
    if not expiry or expiry not in exps:
        expiry = exps[0] if exps else None
    if not expiry:
        return {'error': '没有可用的未到期期权', 'symbol': spec['label']}
    rows, ks = _cli_or_sdk(
        lambda: E._cached(('st-opt', spec['id'], expiry), _ttl(60, 1800),
                          lambda: _option_quotes(spec['opt'], expiry, spot_opt, 0.10)),
        lambda: _sdk_option_quotes(spec['opt'], expiry, spot_opt, 0.10))
    by, nused, T = _greeks(rows, spot_opt, expiry, scale)
    mp = _max_pain(rows, scale)
    profile = _profile(bars, 1.0 if spec['id'] == 'SPX' else 0.25)
    levels = _levels_from(by, last, profile, mp)
    iv = _atm_iv(by, last)
    est = blend_close_estimate(last, levels, by, iv, asof_ts=last_t, scale_note=scale_note)
    est.update({
        'symbol': spec['label'],
        'expiry': expiry,
        'exps': exps,
        'n_quotes': nused,
        'levels': {
            'core_magnet': levels.get('core_magnet'),
            'max_pain': levels.get('max_pain'),
            'dyn_pivot': levels.get('dyn_pivot'),
            'poc': levels.get('poc'),
            'zero_gamma': levels.get('zero_gamma'),
            'support': levels.get('support'),
            'resist': levels.get('resist'),
        },
        'prev_close': q.get('prev_close'),
        'source': 'longbridge-cli-or-sdk',
    })
    return est



def payload(sym='QQQ', expiry=None):
    spec = resolve(sym)
    rth = _rth_now()
    # quotes / bars
    def q_under():
        return E._cached(('st-q', spec['bar']), _ttl(5, 60), lambda: E._quote(spec['bar']))
    def q_spy():
        return E._cached('st-q-spy', _ttl(5, 60), lambda: E._quote('SPY.US'))
    def q_spx():
        return E._cached('st-q-spx', _ttl(5, 60), lambda: E._quote('.SPX.US'))
    def b_1m():
        return E._cached(('st-1m', spec['bar']), _ttl(8, 120), lambda: _bars(spec['bar'], '1m', 500, 1))
    def b_spx():
        return E._cached('st-1m-spx', _ttl(8, 120), lambda: _bars('.SPX.US', '1m', 500, 1))

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_q, f_spy, f_spx, f_b, f_bs = ex.submit(q_under), ex.submit(q_spy), ex.submit(q_spx), ex.submit(b_1m), ex.submit(b_spx)
        q, spy_q, spx_q, bars, spx_bars = f_q.result(), f_spy.result(), f_spx.result(), f_b.result(), f_bs.result()

    last = bars[-1]['c'] if bars else q['last']
    if rth:
        last = q['last']
    # Use last RTH bar on the weekend
    prev = q['prev_close']
    spot_opt = spy_q['last'] if spec['scale'] == 'spy' else last
    scale = (last / spot_opt) if spec['scale'] == 'spy' else 1.0

    exps = E._cached(('st-exps', spec['opt']), _ttl(60, 1800), lambda: _exps(spec['opt']))
    if not expiry or expiry not in exps:
        expiry = exps[0] if exps else None
    if not expiry:
        return {'error': '没有可用的未到期期权', 'symbol': spec['label']}

    def chain():
        return _option_quotes(spec['opt'], expiry, spot_opt, 0.10)
    rows, raw_ks = E._cached(('st-opt', spec['id'], expiry), _ttl(60, 1800), chain)
    by, nused, T = _greeks(rows, spot_opt, expiry, scale)
    # scale greeks already applied to keys; totals need no extra scale except strike labels
    mp = _max_pain(rows, scale)
    profile = _profile(bars, 1.0 if spec['id'] == 'SPX' else 0.25)
    levels = _levels_from(by, last, profile, mp)
    iv = _atm_iv(by, last)
    # dte trading days
    close = datetime.fromisoformat(expiry + 'T16:00:00').replace(tzinfo=ET)
    dte_td = max(int(round((close.date() - datetime.now(ET).date()).days * 5 / 7)) or 1, 1)
    em = _em(last, iv, dte_td)
    heur = _heuristics(last, levels, em, bars, by)

    # snapshots
    day = str(E._et((bars[-1]['t'] if bars else int(time.time()))).date())
    snaps = _load_snaps(spec['id'], day)
    # if no snaps, we can still expose path + current point
    history_note = None
    if len(snaps) < 2:
        history_note = '历史快照从下个交易日开始积累。现仅有当前GEX/IV；Spot、POC、VWAP 已用周五1分钟K线回填。'

    # compact current snapshot (also used by sampler)
    cur_snap = {
        't': bars[-1]['t'] if bars else int(time.time()),
        'spot': last,
        'gex_oi': levels.get('gex_oi_total'),
        'gex_vol': levels.get('gex_vol_total'),
        'dex': levels.get('dex_total'),
        'zero': levels.get('zero_gamma'),
        'major_pos': levels.get('major_pos'),
        'major_neg': levels.get('major_neg'),
        'gamma_peak': levels.get('gamma_peak'),
        'core': levels.get('core_magnet'),
        'vwas_l': levels.get('vwas_long'),
        'vwas_s': levels.get('vwas_short'),
        'max_pain': levels.get('max_pain'),
        'poc': levels.get('poc'),
        'vertex': levels.get('vertex5'),
    }

    # max change table: current only
    maxchg = [{'window': '当前', 'k': levels.get('core_magnet'),
               'gex': (by.get(levels['core_magnet']) or {}).get('gex_oi') if levels.get('core_magnet') in by else None}]
    if snaps:
        # compare last snap vs earlier ones for 5/10/15/30m if timestamps allow
        nowt = cur_snap['t']
        for lab, sec in (('1m', 60), ('5m', 300), ('10m', 600), ('15m', 900), ('30m', 1800)):
            old = next((s for s in reversed(snaps) if nowt - s.get('t', 0) >= sec * 0.7), None)
            if not old:
                continue
            # we only stored aggregates, not per-strike history in v1
            maxchg.append({'window': lab, 'k': old.get('core'), 'gex': None, 'note': '仅存聚合快照'})

    # constituents
    wdoc = E._cached('st-weights', 86400, _load_weights)
    tickers = [c['ticker'] + '.US' for c in wdoc['constituents']]
    quotes = E._cached('st-eq-quotes', _ttl(15, 180), lambda: _quote_batch(tickers))
    # attach rth last for spx
    spx_q2 = dict(spx_q)
    spx_q2['rth_last'] = spx_bars[-1]['c'] if spx_bars else spx_q['last']
    if not rth and spx_bars:
        spx_q2['rth_last'] = spx_bars[-1]['c']
    compass = _compass(spx_q2, spx_bars, quotes, wdoc)
    # 当时收盘参考 = SPX rounded magnet or 5-pt
    compass['ref_close'] = heur['phase_a'] if spec['id'] == 'SPX' else round((spx_q2['rth_last'] or last) / 5) * 5

    chg = last - prev
    chg_pct = (last / prev - 1) * 100 if prev else 0
    last_t = bars[-1]['t'] if bars else int(time.time())

    return {
        'symbol': spec['label'],
        'bar_symbol': spec['bar'],
        'opt_symbol': spec['opt'],
        'approx': 'SPY期权按 SPX/SPY=%.4f 换算' % scale if spec['scale'] else None,
        'expiry': expiry,
        'exps': exps,
        'last': round(last, 2),
        'prev_close': round(prev, 2),
        'change': round(chg, 2),
        'change_pct': round(chg_pct, 2),
        'asof_et': _asof(last_t),
        'asof_ts': last_t,
        'session_date': day,
        'levels': {k: (round(v, 2) if isinstance(v, float) and k not in ('gex_oi_total', 'gex_vol_total', 'dex_total') else v)
                   for k, v in levels.items()},
        'heur': heur,
        'em': None if em is None else round(em, 2),
        'atm_iv': None if iv is None else round(iv, 4),
        'gex_bars': _gex_bars(by),
        'smile': _smile(by),
        'spot_path': profile['path'],
        'running': profile['running'],
        'snaps': snaps[-400:],
        'current_snap': cur_snap,
        'maxchg': maxchg,
        'history_note': history_note,
        'compass': compass,
        'n_contracts': nused,
        'source': {
            'spot': 'Longbridge %s 报价与1分钟K线（RTH）' % spec['bar'],
            'gex': 'Longbridge 期权链 OI × Black-Scholes Gamma × 100 × spot，Call 为正、Put 为负'
                   + ('；SPX 用 SPY 期权换算' if spec['scale'] else ''),
            'gex_vol': '同上，但用当日已成交量代替 OI（日内累计，不是逐笔订单流）',
            'dex': 'OI × BS Delta × 100 × spot',
            'flow': '右侧订单流用各行权价 GEX-by-Vol 近似，不是逐笔期权成交。盘中行权价成交量差分从下一交易日开始积累。',
            'weights': wdoc.get('coverage_note'),
        },
        'tips': heur['formulas'],
    }


def fly_payload(sym='SPX', expiry=None, center=None, wing=None, typ='auto'):
    spec = resolve(sym)
    rth = _rth_now()
    q = _cli_or_sdk(
        lambda: E._cached(('st-q', spec['bar']), _ttl(5, 60), lambda: E._quote(spec['bar'])),
        lambda: _sdk_quote(spec['bar']))
    bars = _cli_or_sdk(
        lambda: E._cached(('st-1m', spec['bar']), _ttl(8, 120), lambda: _bars(spec['bar'], '1m', 500, 1)),
        lambda: _sdk_bars(spec['bar']))
    last = bars[-1]['c'] if bars else q['last']
    if rth:
        last = q['last']
    if spec['scale'] == 'spy':
        spy = _cli_or_sdk(
            lambda: E._cached('st-q-spy', _ttl(5, 60), lambda: E._quote('SPY.US')),
            lambda: _sdk_quote('SPY.US'))
        scale = last / spy['last']
        spot_opt = spy['last']
    else:
        scale, spot_opt = 1.0, last
    exps = _cli_or_sdk(
        lambda: E._cached(('st-exps', spec['opt']), _ttl(60, 1800), lambda: _exps(spec['opt'])),
        lambda: _sdk_exps(spec['opt']))
    if not expiry or expiry not in exps:
        expiry = exps[0] if exps else None
    if not expiry:
        return {'error': '没有可用的未到期期权', 'symbol': spec['label']}
    # wider window for 50-pt (SPX) / 10-pt (QQQ) wings
    window = 0.08 if spec['id'] != 'SPX' else 0.06
    rows, ks = _cli_or_sdk(
        lambda: E._cached(('st-opt', spec['id'], expiry), _ttl(60, 1800),
                          lambda: _option_quotes(spec['opt'], expiry, spot_opt, max(window, 0.10))),
        lambda: _sdk_option_quotes(spec['opt'], expiry, spot_opt, max(window, 0.10)))
    by, nused, T = _greeks(rows, spot_opt, expiry, scale)
    # map scaled strike -> rec
    # fly priced on RAW option last prices, then dollar cost × scale (SPY lot × ratio ≈ SPX dollars)
    raw = {}
    for k, rec in by.items():
        raw[rec['raw_k']] = rec

    def cost(raw_c, raw_w, cp):
        lo, hi = raw_c - raw_w, raw_c + raw_w
        def px(kk, side):
            rec = raw.get(kk) or raw.get(round(kk, 4))
            if not rec:
                # nearest
                if not raw:
                    return None
                kk2 = min(raw, key=lambda x: abs(x - kk))
                if abs(kk2 - kk) > 0.15:
                    return None
                rec = raw[kk2]
            return rec.get('last_c') if side == 'C' else rec.get('last_p')
        a, b, c = px(lo, cp), px(raw_c, cp), px(hi, cp)
        if None in (a, b, c):
            return None
        debit = a + c - 2 * b
        if debit <= 0:
            return None  # credit flies / bad prints
        return debit * 100  # dollars per 1-lot of the option underlying

    # grid
    if spec['id'] == 'SPX':
        # display in SPX points; underlying wings are 1,2,3... SPY points
        disp_wings = [5, 10, 15, 20, 25, 30, 40, 50]
        # centers: snap SPY strikes * scale to nearby 5-pt
        spy_ks = sorted(raw)
        centers_raw = [k for k in spy_ks if abs(k - spot_opt) <= 12]
    else:
        disp_wings = [1, 2, 3, 4, 5, 6, 8, 10] if spec['id'] == 'QQQ' else [1, 2, 3, 4, 5, 6, 8, 10]
        centers_raw = [k for k in sorted(raw) if abs(k - spot_opt) <= (10 if spec['id'] == 'QQQ' else 8)]

    grid = []
    for ck in centers_raw:
        row = {'center_raw': ck, 'center': round(ck * scale, 2), 'cells': []}
        for dw in (disp_wings if spec['id'] != 'SPX' else disp_wings):
            raw_w = dw / scale if spec['id'] == 'SPX' else dw
            # only if raw wing hits a strike
            if spec['id'] == 'SPX':
                # require raw_w close to an integer (SPY $1)
                if abs(raw_w - round(raw_w)) > 0.15:
                    row['cells'].append({'wing': dw, 'cost': None, 'cp': None})
                    continue
                raw_w = round(raw_w)
            best = None
            for cp in ('C', 'P'):
                dol = cost(ck, raw_w, cp)
                if dol is None:
                    continue
                dol_disp = dol * scale  # SPY dollars × ratio ≈ SPX dollars
                if best is None or dol_disp < best['cost']:
                    best = {'wing': dw, 'cost': round(dol_disp, 2), 'cp': cp,
                            'raw_cost': dol, 'raw_w': raw_w}
            row['cells'].append(best or {'wing': dw, 'cost': None, 'cp': None})
        grid.append(row)

    # blended close estimate (same function as /api/strategy/close_estimate)
    mp = _max_pain(rows, scale)
    profile = _profile(bars, 1.0 if spec['id'] == 'SPX' else 0.25)
    levels = _levels_from(by, last, profile, mp)
    iv_est = _atm_iv(by, last)
    last_t_est = bars[-1]['t'] if bars else int(time.time())
    scale_note = ('SPX 模式用 SPY 期权按 SPX/SPY=%.4f 换算' % scale) if spec['scale'] else None
    close_est = blend_close_estimate(last, levels, by, iv_est, asof_ts=last_t_est, scale_note=scale_note)
    close_est['symbol'] = spec['label']
    close_est['expiry'] = expiry
    target = close_est['estimate']
    # default selection: cheaper fly at center nearest target, smallest available wing
    def pick_default():
        if not grid:
            return None
        row = min(grid, key=lambda r: abs(r['center'] - target))
        cells = [c for c in row['cells'] if c.get('cost') is not None]
        if not cells:
            return None
        if typ in ('C', 'P'):
            prefer = [c for c in cells if c.get('cp') == typ] or cells
        else:
            prefer = cells
        # smallest wing first
        prefer = sorted(prefer, key=lambda c: (c['wing'], c['cost']))
        c0 = prefer[0]
        return {'center': row['center'], 'center_raw': row['center_raw'], 'wing': c0['wing'],
                'raw_w': c0.get('raw_w'), 'cp': c0['cp'], 'cost': c0['cost']}
    sel = None
    if center and wing:
        row = min(grid, key=lambda r: abs(r['center'] - float(center))) if grid else None
        if row:
            cell = next((c for c in row['cells'] if c['wing'] == int(float(wing)) and c.get('cost') is not None), None)
            if typ in ('C', 'P') and cell and cell.get('cp') != typ:
                # try this wing with forced type
                raw_w = (int(float(wing)) / scale) if spec['id'] == 'SPX' else int(float(wing))
                if spec['id'] == 'SPX':
                    raw_w = round(raw_w)
                dol = cost(row['center_raw'], raw_w, typ)
                if dol is not None:
                    cell = {'wing': int(float(wing)), 'cost': round(dol * scale, 2), 'cp': typ, 'raw_w': raw_w}
            if cell:
                sel = {'center': row['center'], 'center_raw': row['center_raw'], 'wing': cell['wing'],
                       'raw_w': cell.get('raw_w'), 'cp': cell['cp'], 'cost': cell['cost']}
    if not sel:
        sel = pick_default()

    # P&L at expiry for selected fly, in display dollars
    pnl = None
    if sel and sel.get('cost') is not None:
        w_disp = sel['wing'] if spec['id'] != 'SPX' else sel['wing']
        # max profit = wing_width_display * 100 - wait: for native QQQ, wing is 1 point, *100 = $100 width
        # for SPX-from-SPY, we already scaled cost to SPX dollars; width value = (raw_w * 100) * scale
        if spec['id'] == 'SPX':
            width_value = (sel.get('raw_w') or (sel['wing'] / scale)) * 100 * scale
        else:
            width_value = sel['wing'] * 100
        debit = sel['cost']
        max_profit = width_value - debit
        be_lo = sel['center'] - (width_value - debit) / 100.0
        be_hi = sel['center'] + (width_value - debit) / 100.0
        # payoff vs price
        lo_p = sel['center'] - (sel['wing'] if spec['id'] != 'SPX' else sel['wing'])
        # use display wing for x axis
        wpx = sel['wing']
        xs, ys, ynow = [], [], []
        lo = (profile_low := (min(b['l'] for b in bars) if bars else last - 2 * (wpx or 5)))
        hi = (max(b['h'] for b in bars) if bars else last + 2 * (wpx or 5))
        span = max(hi - lo, 4 * wpx)
        x0, x1 = sel['center'] - span, sel['center'] + span
        # current combo value via BS
        recs = raw
        cp = sel['cp']
        ck, rw = sel['center_raw'], sel.get('raw_w') or wpx
        def leg_iv(kk, side):
            rec = raw.get(kk)
            if not rec:
                return None
            return rec.get('iv_c') if side == 'C' else rec.get('iv_p')
        def payoff(S_disp):
            # long low, short 2 mid, long high, in display dollars
            # intrinsic in underlying points * 100 * scale factor
            S_raw = S_disp / scale
            def intrins(K, call):
                return max(S_raw - K, 0) if call else max(K - S_raw, 0)
            call = (cp == 'C')
            v = intrins(ck - rw, call) + intrins(ck + rw, call) - 2 * intrins(ck, call)
            return v * 100 * scale - debit
        def now_value(S_disp):
            S_raw = S_disp / scale
            call = (cp == 'C')
            def pr(K):
                iv = leg_iv(K, cp) or 0
                return _bs_price(S_raw, K, iv, T, call)
            v = pr(ck - rw) + pr(ck + rw) - 2 * pr(ck)
            return v * 100 * scale
        n = 80
        for i in range(n + 1):
            x = x0 + (x1 - x0) * i / n
            xs.append(round(x, 2))
            ys.append(round(payoff(x), 2))
            try:
                ynow.append(round(now_value(x), 2))
            except Exception:
                ynow.append(None)
        pnl_at = payoff(last)
        edge = min(abs(last - be_lo), abs(last - be_hi))
        inside = be_lo <= last <= be_hi
        pnl = {
            'debit': round(debit, 2),
            'max_profit': round(max_profit, 2),
            'max_loss': round(-debit, 2),
            'be': [round(be_lo, 2), round(be_hi, 2)],
            'pct': round(max_profit / debit * 100, 1) if debit else None,
            'xs': xs, 'ys': ys, 'ynow': ynow,
            'pnl_at_spot': round(pnl_at, 2),
            'inside': inside,
            'edge': round(edge, 2),
            'width_value': round(width_value, 2),
            'legs': [
                {'k': round((ck - rw) * scale, 2), 'side': '买', 'cp': cp, 'n': 1},
                {'k': round(ck * scale, 2), 'side': '卖', 'cp': cp, 'n': 2},
                {'k': round((ck + rw) * scale, 2), 'side': '买', 'cp': cp, 'n': 1},
            ],
        }

    # remaining ranges from ATM IV
    iv = _atm_iv(by, last)
    close = datetime.fromisoformat(expiry + 'T16:00:00').replace(tzinfo=ET)
    dte_td = max(int(round((close.date() - datetime.now(ET).date()).days * 5 / 7)) or 1, 1)
    sigma = _em(last, iv, dte_td) or (last * 0.008)
    ranges = {
        '90': [round(last - 1.645 * sigma, 2), round(last + 1.645 * sigma, 2)],
        '75': [round(last - 1.150 * sigma, 2), round(last + 1.150 * sigma, 2)],
        '50': [round(last - 0.674 * sigma, 2), round(last + 0.674 * sigma, 2)],
        'high': max(b['h'] for b in bars) if bars else None,
        'low': min(b['l'] for b in bars) if bars else None,
    }
    # fit tag
    if sel and pnl:
        # 不适配 if remaining 75% range is much wider than wings or target outside be
        wpx = sel['wing']
        wide = (ranges['75'][1] - ranges['75'][0]) > 2.5 * wpx
        far = abs((target or last) - sel['center']) > wpx
        fit = '不适配' if (wide or far or not pnl['inside']) else '适配'
    else:
        fit = '—'

    sess_hi = ranges['high']
    sess_lo = ranges['low']
    last_t = bars[-1]['t'] if bars else int(time.time())
    # time to expiry label
    secs = (close - datetime.now(ET)).total_seconds()
    if secs <= 0:
        tte_lbl = '已到期（周末显示下一期 %s）' % expiry
    elif secs < 3600:
        tte_lbl = '距到期不足 %d 分钟' % max(int(secs // 60), 1)
    elif secs < 86400:
        tte_lbl = '距到期约 %.1f 小时' % (secs / 3600)
    else:
        tte_lbl = '距到期约 %.1f 天' % (secs / 86400)

    return {
        'symbol': spec['label'],
        'approx': 'SPX 模式用 SPY 期权按 SPX/SPY=%.4f 换算，成本×比例，翼展 5 点通常没有对应合约' % scale if spec['scale'] else None,
        'expiry': expiry,
        'exps': exps,
        'last': round(last, 2),
        'asof_et': _asof(last_t),
        'asof_ts': last_t,
        'tte': tte_lbl,
        'contract': 'SPY 换算' if spec['scale'] else spec['opt'].replace('.US', ''),
        'target': target,
        'close_estimate': close_est,
        'sel': sel,
        'pnl': pnl,
        'fit': fit,
        'ranges': ranges,
        'grid': grid,
        'wings': disp_wings if spec['id'] != 'QQQ' and spec['id'] != 'SPY' else disp_wings,
        'n_quotes': nused,
        'atm_iv': None if iv is None else round(iv, 4),
        'em': None if sigma is None else round(sigma, 2),
        'source': {
            'price': '期权最新价（长桥无买卖盘中间价），组合成本 = (低+高−2×中)×100'
                     + ('，再按 SPX/SPY 比例放大' if spec['scale'] else ''),
            'now': '当前曲线用 Black-Scholes（同一IV与剩余期限）估算组合价值；周末无当日逐笔历史。',
            'range': '剩余走势 50/75/90% 区间 = 当前价 ± 0.67/1.15/1.65 × ATM IV 到期预期波动；高低点来自当日1分钟K线。',
        },
    }


_sampler_started = False

def _sampler_loop():
    while True:
        try:
            if _rth_now():
                for s in ('QQQ', 'SPY', 'SPX'):
                    try:
                        d = payload(s)
                        day = d.get('session_date')
                        snap = d.get('current_snap')
                        if day and snap:
                            # avoid dupes within 90s
                            prev = _load_snaps(d['symbol'], day)
                            if not prev or snap['t'] - prev[-1].get('t', 0) >= 90:
                                _append_snap(d['symbol'], day, snap)
                    except Exception as e:
                        print('strategy sampler', s, repr(e), flush=True)
                time.sleep(180)
            else:
                time.sleep(60)
        except Exception as e:
            print('strategy sampler loop', repr(e), flush=True)
            time.sleep(60)


def prewarm():
    def run():
        for s in ('QQQ', 'SPX', 'SPY'):
            try:
                payload(s)
            except Exception as e:
                print('strategy prewarm', s, repr(e), flush=True)
        try:
            fly_payload('SPX')
        except Exception as e:
            print('strategy fly prewarm', repr(e), flush=True)
    threading.Thread(target=run, daemon=True).start()
    global _sampler_started
    if not _sampler_started:
        _sampler_started = True
        threading.Thread(target=_sampler_loop, daemon=True).start()
