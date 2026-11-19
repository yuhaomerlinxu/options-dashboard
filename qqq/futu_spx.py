"""Read-only SPX/SPXW option-chain loader for the daily forecast.

Uses Futu OpenD (OpenQuoteContext only — never opens a trade context,
never places, modifies or cancels orders) plus a Longbridge .SPX.US
spot, because Futu is not entitled to the US index cash quote.

Dealer GEX convention (standard retail / SqueezeMetrics-style)
--------------------------------------------------------------
Customers are assumed to be net sellers of calls (overwriting) and net
buyers of puts (hedging). Dealers therefore carry the opposite book:

    call GEX  = + OI * gamma * multiplier * spot
    put  GEX  = - OI * gamma * multiplier * spot

where ``multiplier`` is 100 for SPX/SPXW. A positive net GEX at a
strike means dealers are long gamma there and tend to fade moves
(pinning). A negative net GEX means dealers are short gamma and tend
to amplify moves. Relative rankings across strikes do not depend on
whether the last term is ``spot`` or ``spot**2`` (spot is common);
this module uses ``spot`` to stay consistent with ``strategyapi._greeks``.

Gamma flip (zero-gamma level)
-----------------------------
Walk strikes from low to high and accumulate net GEX. The flip is the
interpolated strike where the cumulative sum crosses zero, taking the
crossing closest to spot if there are several. This is *not* the same
as a sign change in the single-strike GEX series.

Call / put walls
----------------
Call wall = strike with the largest call open interest.
Put  wall = strike with the largest put open interest.

If Futu is unavailable, callers must fall back to the existing
SPY-scaled Longbridge path; this module never fabricates a chain.
"""
from __future__ import annotations

import math
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
BJ = ZoneInfo('Asia/Shanghai')

HOST = os.environ.get('FUTU_OPEND_HOST', '127.0.0.1')
PORT = int(os.environ.get('FUTU_OPEND_PORT', '11111'))
UNDERLYING = 'US..SPX'
SNAPSHOT_BATCH = 100  # stay at or under the 100-slot subscription quota
DEFAULT_WINDOW = 0.10  # ±10% of spot
MULTIPLIER = 100.0


def opend_reachable(host=None, port=None, timeout=0.4):
    """Fast TCP check so Render can skip Futu without hanging on 127.0.0.1:11111."""
    import socket
    host = host or HOST
    port = int(port or PORT)
    try:
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        return True
    except OSError:
        return False

INV_SQRT_2_PI = math.sqrt(2.0 / math.pi)  # ≈ 0.79788; E[|X|] for unit normal

# Allow system Python to import the box-local Futu SDK without activating the venv.
_FUTU_SITE = os.environ.get(
    'FUTU_SITE',
    '/workspace/futu-venv/lib/python3.13/site-packages',
)
if _FUTU_SITE and os.path.isdir(_FUTU_SITE) and _FUTU_SITE not in sys.path:
    sys.path.insert(0, _FUTU_SITE)


def _fnum(x):
    if x is None:
        return None
    try:
        if hasattr(x, 'item'):
            x = x.item()
        v = float(x)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


def _ensure_futu():
    try:
        from futu import OpenQuoteContext, RET_OK  # noqa: F401
        return True, None
    except Exception as e:
        return False, type(e).__name__ + ': ' + str(e)[:200]


def mid_px(bid, ask, last=None):
    b, a, last = _fnum(bid), _fnum(ask), _fnum(last)
    if b is not None and a is not None and b > 0 and a > 0:
        return (b + a) / 2.0
    if last is not None and last > 0:
        return last
    return None


def iv_to_decimal(iv):
    """Futu quotes IV in percent (e.g. 26.8); some other sources use 0.268."""
    iv = _fnum(iv)
    if iv is None or iv <= 0:
        return None
    return iv / 100.0 if iv > 2.0 else iv


def product_of(code):
    code = str(code or '')
    if code.startswith('US.SPXW') or code.startswith('SPXW'):
        return 'SPXW'
    if code.startswith('US.SPX') or code.startswith('SPX'):
        return 'SPX'
    return code


def is_spxw(code):
    return product_of(code) == 'SPXW'


def is_spx_monthly(code):
    """True for the US.SPX monthly (not the SPXW weekly) contract."""
    c = str(code or '')
    return (c.startswith('US.SPX') and not c.startswith('US.SPXW')) or (
        c.startswith('SPX') and not c.startswith('SPXW')
    )


def third_friday(iso):
    d = datetime.fromisoformat(str(iso)[:10]).date()
    first = d.replace(day=1)
    # weekday() Monday=0 ... Friday=4
    offset = (4 - first.weekday()) % 7
    return first + timedelta(days=offset + 14)


def is_monthly_expiry(iso):
    try:
        return datetime.fromisoformat(str(iso)[:10]).date() == third_friday(iso)
    except ValueError:
        return False


def pick_expiries(dates, start_day, max_expiries=4, near_days=5, monthly_horizon=10):
    """Nearest weeklies within ``near_days``, plus the front monthly if it falls inside ``monthly_horizon``."""
    dates = list(dates)
    if not dates:
        return []
    if hasattr(start_day, 'isoformat'):
        start_iso = start_day.isoformat()
        start = start_day
    else:
        start_iso = str(start_day)[:10]
        start = datetime.fromisoformat(start_iso).date()
    horizon_near = (start + timedelta(days=near_days)).isoformat()
    horizon_m = (start + timedelta(days=monthly_horizon)).isoformat()
    pick = []
    for d in dates:
        if d < start_iso:
            continue
        if d <= horizon_near:
            pick.append(d)
        elif d <= horizon_m and is_monthly_expiry(d) and d not in pick:
            pick.append(d)
        if len(pick) >= max_expiries:
            break
    return pick or dates[:2]


def gex_unit(oi, gamma, spot, sign, multiplier=MULTIPLIER):
    """Dealer GEX for one contract type at one strike. ``sign`` is +1 call / -1 put."""
    if not oi or not gamma or not spot:
        return 0.0
    return float(oi) * float(gamma) * float(multiplier) * float(spot) * float(sign)


def aggregate_by_strike(quotes, spot, multiplier=MULTIPLIER):
    """Build the per-strike dict that strategyapi levels / blender expect.

    ``quotes`` is a list of normalized dicts with keys:
    expiry, strike, cp, oi, volume, iv, gamma, last, bid, ask, product (optional).
    Gamma should already be d(delta)/d(spot). IV is a decimal.
    """
    by = {}
    used = 0
    spot = float(spot)
    for q in quotes or []:
        k = _fnum(q.get('strike'))
        if k is None or k <= 0:
            continue
        cp = str(q.get('cp') or 'C')[0].upper()
        if cp not in ('C', 'P'):
            continue
        oi = int(_fnum(q.get('oi')) or 0)
        vol = float(_fnum(q.get('volume')) or 0)
        iv = _fnum(q.get('iv'))
        gamma = _fnum(q.get('gamma'))
        last = mid_px(q.get('bid'), q.get('ask'), q.get('last'))
        sign = 1 if cp == 'C' else -1
        gex = gex_unit(oi, gamma, spot, sign, multiplier)
        rec = by.setdefault(k, {
            'k': round(k, 2),
            'gex_oi': 0.0,
            'gex_vol': 0.0,
            'dex': 0.0,
            'oi_c': 0,
            'oi_p': 0,
            'vol_c': 0,
            'vol_p': 0,
            'iv_c': None,
            'iv_p': None,
            'last_c': None,
            'last_p': None,
            'raw_k': k,
            'gamma_c': None,
            'gamma_p': None,
        })
        rec['gex_oi'] += gex
        rec['gex_vol'] += gex_unit(vol, gamma, spot, sign, multiplier)
        if cp == 'C':
            rec['oi_c'] += oi
            rec['vol_c'] += vol
            rec['iv_c'] = iv if iv else rec['iv_c']
            rec['last_c'] = last if last is not None else rec['last_c']
            rec['gamma_c'] = gamma if gamma is not None else rec['gamma_c']
        else:
            rec['oi_p'] += oi
            rec['vol_p'] += vol
            rec['iv_p'] = iv if iv else rec['iv_p']
            rec['last_p'] = last if last is not None else rec['last_p']
            rec['gamma_p'] = gamma if gamma is not None else rec['gamma_p']
        used += 1
    return by, used


def gamma_flip(by, spot):
    """Interpolated cumulative-GEX zero crossing closest to ``spot``.

    Returns None if the cumulative sum never changes sign (no flip in
    the loaded window — often a truncated chain).
    """
    if not by:
        return None
    ks = sorted(by)
    cum = []
    total = 0.0
    for k in ks:
        total += by[k]['gex_oi']
        cum.append((k, total))
    crossings = []
    for (k0, g0), (k1, g1) in zip(cum, cum[1:]):
        if g0 == 0:
            crossings.append(k0)
        elif g0 * g1 < 0:
            # linear interpolation in strike
            frac = abs(g0) / (abs(g0) + abs(g1)) if (abs(g0) + abs(g1)) else 0.5
            crossings.append(k0 + frac * (k1 - k0))
        elif g1 == 0:
            crossings.append(k1)
    if not crossings:
        return None
    flip = min(crossings, key=lambda x: abs(x - float(spot)))
    return round(flip, 2)


def call_put_walls(by, spot=None, band=None):
    """Near-term call/put walls: largest call OI at or above spot, largest put OI
    at or below spot, inside a band around spot.

    Unrestricted max-OI strikes (often 7000/8000 round-number dumps at the
    edge of a ±10% window) are returned as ``far_call_wall`` / ``far_put_wall``
    so they stay visible without displacing the near-term walls the forecast uses.
    """
    if not by:
        return None, None, None, None

    def _rnd(k):
        return None if k is None else round(float(k), 2)

    far_call = max(by, key=lambda k: (by[k]['oi_c'], -abs(k)))
    far_put = max(by, key=lambda k: (by[k]['oi_p'], -abs(k)))
    far_call = far_call if by[far_call]['oi_c'] > 0 else None
    far_put = far_put if by[far_put]['oi_p'] > 0 else None

    if spot is None:
        return _rnd(far_call), _rnd(far_put), _rnd(far_call), _rnd(far_put)

    spot = float(spot)
    if band is None:
        band = max(300.0, 0.04 * spot)
    band = float(band)

    above = [k for k in by if k >= spot and k <= spot + band and by[k]['oi_c'] > 0]
    below = [k for k in by if k <= spot and k >= spot - band and by[k]['oi_p'] > 0]
    # If the band is empty (very sparse chain), fall back to any strike on that side.
    if not above:
        above = [k for k in by if k >= spot and by[k]['oi_c'] > 0]
    if not below:
        below = [k for k in by if k <= spot and by[k]['oi_p'] > 0]
    call_wall = max(above, key=lambda k: (by[k]['oi_c'], -abs(k - spot))) if above else far_call
    put_wall = max(below, key=lambda k: (by[k]['oi_p'], -abs(k - spot))) if below else far_put
    return _rnd(call_wall), _rnd(put_wall), _rnd(far_call), _rnd(far_put)


def atm_iv(by, spot):
    if not by:
        return None
    k0 = min(by, key=lambda k: abs(k - float(spot)))
    rec = by[k0]
    ivs = [v for v in (rec.get('iv_c'), rec.get('iv_p')) if v]
    return sum(ivs) / len(ivs) if ivs else None


def atm_straddle(by, spot):
    """ATM strike, call/put last-or-mid, straddle points, and 1-sigma move.

    The listed straddle price approximates the expected absolute move
    E[|S_T - K|] ≈ sigma * sqrt(2/pi). The 1-sigma (≈68% half-width) is
    therefore straddle / sqrt(2/pi). Returns None fields when a side is
    missing. Does not invent prices.
    """
    if not by:
        return None
    k0 = min(by, key=lambda k: abs(k - float(spot)))
    rec = by[k0]
    call = rec.get('last_c')
    put = rec.get('last_p')
    out = {
        'atm_strike': round(k0, 2),
        'call': None if call is None else round(float(call), 4),
        'put': None if put is None else round(float(put), 4),
        'straddle': None,
        'implied_move': None,       # expected |move| ≈ straddle
        'one_sigma_move': None,     # straddle / sqrt(2/pi)
        'atm_iv': atm_iv({k0: rec}, k0),
    }
    if call is not None and put is not None and call > 0 and put > 0:
        st = float(call) + float(put)
        out['straddle'] = round(st, 4)
        out['implied_move'] = round(st, 4)
        out['one_sigma_move'] = round(st / INV_SQRT_2_PI, 4)
    return out


def gex_rows(by, top=40):
    ks = sorted(by, key=lambda k: -abs(by[k]['gex_oi']))[:top]
    ks = sorted(ks)
    return [{
        'k': round(k, 2),
        'gex_oi': round(by[k]['gex_oi'], 2),
        'oi_c': by[k]['oi_c'],
        'oi_p': by[k]['oi_p'],
        'vol_c': by[k]['vol_c'],
        'vol_p': by[k]['vol_p'],
        'iv_c': by[k].get('iv_c'),
        'iv_p': by[k].get('iv_p'),
    } for k in ks]


def directional_bias(spot, prev_close, estimate, levels, by, vix=None, em=None):
    """Transparent component scores for the 02:00 forecast.

    Scores are roughly in [-2, +2] (positive = bullish). Being below the
    gamma flip is *not* scored as bearish by itself: negative-gamma only
    amplifies moves, it does not choose a direction. The flip still
    appears as a label. This is a structure summary, not a trade signal.
    """
    scores = {}
    ev = em if em and em > 0 else max(abs(float(spot)) * 0.006, 1.0)
    if prev_close:
        scores['structure'] = round(max(-2, min(2, (float(estimate) - float(prev_close)) / ev)), 3)
    else:
        scores['structure'] = 0.0
    if by:
        call_g = sum(max(v['gex_oi'], 0) for v in by.values())
        put_g = sum(max(-v['gex_oi'], 0) for v in by.values())
        energy = call_g / put_g if put_g > 1 else (2.0 if call_g > 0 else 1.0)
        # 1.0 = balanced; map 0.7..1.3 onto about -2..+2
        scores['gex_energy'] = round(max(-2, min(2, (energy - 1.0) / 0.15)), 3)
    else:
        energy = None
        scores['gex_energy'] = 0.0
    flip = (levels or {}).get('zero_gamma')
    put_wall = (levels or {}).get('put_wall')
    call_wall = (levels or {}).get('call_wall')
    # Near a put wall (support) slightly caps further downside in the score;
    # near a call wall (resistance) slightly caps further upside.
    wall_score = 0.0
    if put_wall is not None and abs(float(spot) - float(put_wall)) <= ev:
        wall_score += 0.3
    if call_wall is not None and abs(float(spot) - float(call_wall)) <= ev:
        wall_score -= 0.3
    scores['walls'] = round(wall_score, 3)
    if vix and vix.get('last') and vix.get('prev'):
        # Rising VIX is risk-off for the index
        scores['vix'] = round(max(-2, min(2, -(float(vix['last']) - float(vix['prev'])) / 1.5)), 3)
    else:
        scores['vix'] = 0.0
    total = (
        1.2 * scores['structure']
        + 0.8 * scores['gex_energy']
        + 0.5 * scores['walls']
        + 0.5 * scores['vix']
    )
    scores['total'] = round(total, 3)
    if total >= 1.0:
        label = '偏多'
    elif total >= 0.35:
        label = '中性偏多'
    elif total <= -1.0:
        label = '偏空'
    elif total <= -0.35:
        label = '中性偏空'
    else:
        label = '中性'
    return {
        'direction': label,
        'direction_scores': scores,
        'gex_energy': None if energy is None else round(energy, 3),
        'below_zero_gamma': (
            flip is not None and float(spot) < float(flip)
        ),
    }


def expected_move_to_close(straddle_info, expiry, now=None):
    """1-sigma move to the coming RTH close from an expiry-dated ATM straddle.

    If the nearest expiry is the coming close (0DTE / next-session PM),
    the straddle already is the remaining move and is used as-is. If the
    expiry is later, scale by sqrt(time_to_close / time_to_expiry).
    Returns (em_close, note) or (None, reason).
    """
    now = now or datetime.now(ET)
    if not straddle_info or not straddle_info.get('one_sigma_move'):
        return None, 'no ATM straddle'
    sigma = float(straddle_info['one_sigma_move'])
    if not expiry:
        return sigma, 'ATM-straddle 1-sigma (expiry unknown, used as-is)'
    try:
        exp_d = datetime.fromisoformat(str(expiry)[:10]).date()
    except ValueError:
        return sigma, 'ATM-straddle 1-sigma (expiry unparsed, used as-is)'
    # Coming RTH close date: today if before 16:00 ET on a weekday, else next weekday.
    close_dt = now.replace(hour=16, minute=0, second=0, microsecond=0)
    if now.weekday() >= 5 or now >= close_dt:
        # next weekday
        d = now.date() + timedelta(days=1)
        while d.weekday() >= 5:
            d += timedelta(days=1)
        close_dt = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
        now_for_t = close_dt.replace(hour=9, minute=30)  # full session remains
        t_close = (close_dt - now_for_t).total_seconds() + 16 * 3600  # include overnight roughly
        # Simpler: after hours, coming close is the next session; if expiry is that
        # same date (or next AM monthly that morning), use sigma as-is.
        if exp_d <= d:
            return sigma, 'ATM-straddle 1-sigma to nearest expiry (next session)'
        # expiry farther than the next close: scale by trading-day fraction
        # count weekdays from d to exp_d inclusive-ish
        days = 0
        x = d
        while x < exp_d:
            x += timedelta(days=1)
            if x.weekday() < 5:
                days += 1
        days = max(days, 1)
        em = sigma / math.sqrt(days)
        return em, 'ATM-straddle 1-sigma scaled to next close by 1/sqrt(%d trading days to expiry)' % days
    # In RTH, coming close is today 16:00
    if exp_d <= now.date():
        return sigma, 'ATM-straddle 1-sigma to today\'s expiry'
    days = 0
    x = now.date()
    while x < exp_d:
        x += timedelta(days=1)
        if x.weekday() < 5:
            days += 1
    days = max(days, 1)
    # remaining fraction of today plus later days
    rem = (close_dt - now).total_seconds()
    sess = 6.5 * 3600
    t_ratio = (rem / sess + (days - 1)) / days if days else 1.0
    t_ratio = max(0.05, min(1.0, t_ratio))
    em = sigma * math.sqrt(t_ratio)
    return em, 'ATM-straddle 1-sigma scaled by remaining time to close vs expiry'


def normalize_futu_row(row):
    """Map a Futu market-snapshot row (Series or dict) to the common schema."""
    if hasattr(row, 'to_dict'):
        r = row.to_dict()
    else:
        r = dict(row)
    code = r.get('code')
    cp_raw = str(r.get('option_type') or '')
    if cp_raw.upper().startswith('C'):
        cp = 'C'
    elif cp_raw.upper().startswith('P'):
        cp = 'P'
    else:
        return None
    strike = _fnum(r.get('option_strike_price') if 'option_strike_price' in r else r.get('strike_price'))
    expiry = r.get('strike_time') or r.get('option_expiry_date')
    if expiry is not None:
        expiry = str(expiry)[:10]
    iv = iv_to_decimal(r.get('option_implied_volatility'))
    return {
        'code': code,
        'product': product_of(code),
        'expiry': expiry,
        'strike': strike,
        'cp': cp,
        'oi': int(_fnum(r.get('option_open_interest')) or 0),
        'volume': _fnum(r.get('volume')) or 0.0,
        'iv': iv,
        'gamma': _fnum(r.get('option_gamma')),
        'delta': _fnum(r.get('option_delta')),
        'last': _fnum(r.get('last_price')),
        'bid': _fnum(r.get('bid_price')),
        'ask': _fnum(r.get('ask_price')),
        'update_time': r.get('update_time'),
        'multiplier': _fnum(r.get('option_contract_multiplier')) or MULTIPLIER,
    }


def _snapshot_batches(ctx, codes, batch=SNAPSHOT_BATCH):
    from futu import RET_OK
    frames, errors = [], []
    for i in range(0, len(codes), batch):
        part = codes[i:i + batch]
        ret, data = ctx.get_market_snapshot(part)
        if ret != RET_OK:
            errors.append({'batch_start': i, 'n': len(part), 'error': str(data)[:300]})
            continue
        frames.append(data)
    return frames, errors


def _expiry_list(ctx, today):
    from futu import RET_OK
    ret, data = ctx.get_option_expiration_date(code=UNDERLYING, index_option_type='NORMAL')
    if ret != RET_OK:
        return [], str(data)[:300]
    dates = []
    # DataFrame with a strike_time / time column, or a list of dates
    if hasattr(data, 'columns'):
        col = None
        for cand in ('strike_time', 'time', 'expiry_date', 'expiration_date', 'date'):
            if cand in data.columns:
                col = cand
                break
        if col is None and len(data.columns):
            col = data.columns[0]
        for v in data[col].tolist():
            s = str(v)[:10]
            if len(s) >= 10:
                dates.append(s)
    else:
        for v in data or []:
            s = str(v)[:10]
            if len(s) >= 10:
                dates.append(s)
    dates = sorted({d for d in dates if d >= today})
    return dates, None


def load_spx_chain(spot, host=HOST, port=PORT, window=DEFAULT_WINDOW,
                   batch=SNAPSHOT_BATCH, max_expiries=2, now=None):
    """Fetch the nearest SPXW weeklies (and SPX monthly if in range) from OpenD.

    Read-only. Returns a dict with ``by``, ``quotes``, ``expiries``,
    ``nearest_expiry``, ``straddle``, ``atm_iv``, walls, flip, or
    ``{'error': ...}`` on failure. Never raises into the forecast path
    if the caller wraps it; this function still raises only on unexpected
    bugs after OpenD has already failed cleanly.
    """
    ok, err = _ensure_futu()
    if not ok:
        return {'error': 'futu_sdk_unavailable: ' + err, 'by': None}
    from futu import OpenQuoteContext, RET_OK

    now = now or datetime.now(ET)
    # After 16:15 ET, today's 0DTE SPXW is expired; start from the next date.
    start_day = now.date()
    if now.hour > 16 or (now.hour == 16 and now.minute >= 15):
        start_day = start_day + timedelta(days=1)
        while start_day.weekday() >= 5:
            start_day += timedelta(days=1)
    today = start_day.isoformat()
    spot = float(spot)
    lo, hi = spot * (1 - window), spot * (1 + window)

    ctx = OpenQuoteContext(host=host, port=port)
    out = {
        'source': 'futu',
        'underlying': UNDERLYING,
        'spot': spot,
        'window': window,
        'fetched_at_bj': datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S'),
        'by': None,
        'quotes': [],
        'expiries': [],
        'error': None,
        'snapshot_errors': [],
        'gex_convention': (
            'Dealer GEX: call = +OI*gamma*100*spot, put = -OI*gamma*100*spot. '
            'Dealers assumed long calls (buy-writes) and short puts (hedges). '
            'Gamma flip = cumulative net GEX zero crossing closest to spot. '
            'Call/put walls = strikes with the largest call/put open interest.'
        ),
    }
    try:
        ret, state = ctx.get_global_state()
        if ret != RET_OK:
            out['error'] = 'futu_global_state: ' + str(state)[:200]
            return out
        qot_ok = True
        if isinstance(state, dict):
            qot_ok = bool(state.get('qot_logined', True))
            out['program_status'] = state.get('program_status_type') or state.get('program_status')
        if not qot_ok:
            out['error'] = 'futu_not_quote_logged_in'
            return out

        dates, exp_err = _expiry_list(ctx, today)
        if exp_err and not dates:
            # Fall back to a two-week chain query
            end = (start_day + timedelta(days=14)).isoformat()
            ret, chain = ctx.get_option_chain(code=UNDERLYING, start=today, end=end)
            if ret != RET_OK:
                out['error'] = 'futu_expiry_list: ' + exp_err + '; chain: ' + str(chain)[:200]
                return out
            if 'strike_time' in chain.columns:
                dates = sorted({str(x)[:10] for x in chain['strike_time'].tolist() if str(x)[:10] >= today})
            else:
                out['error'] = 'futu_expiry_list: ' + exp_err
                return out
        if not dates:
            out['error'] = 'futu_no_expiries'
            return out
        pick = pick_expiries(dates, start_day, max_expiries=max_expiries)
        out['expiries'] = dates
        out['expiries_used'] = pick

        all_codes = []
        chain_meta = {}
        for d in pick:
            ret, chain = ctx.get_option_chain(code=UNDERLYING, start=d, end=d)
            if ret != RET_OK:
                chain_meta[d] = {'error': str(chain)[:200]}
                continue
            if chain is None or len(chain) == 0:
                chain_meta[d] = {'n': 0}
                continue
            # Restrict to the strike window. Prefer keeping both SPXW and monthly SPX.
            if 'strike_price' in chain.columns:
                sub = chain[(chain['strike_price'] >= lo) & (chain['strike_price'] <= hi)]
            else:
                sub = chain
            codes = [c for c in sub['code'].tolist() if c]
            chain_meta[d] = {
                'n_chain': int(len(chain)),
                'n_window': int(len(sub)),
                'n_spxw': int(sum(1 for c in codes if is_spxw(c))),
                'n_spx': int(sum(1 for c in codes if is_spx_monthly(c))),
            }
            all_codes.extend(codes)
        out['chain_meta'] = chain_meta
        seen, uniq = set(), []
        for c in all_codes:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        out['n_codes'] = len(uniq)
        if not uniq:
            out['error'] = 'futu_no_contracts_in_window'
            return out

        frames, errors = _snapshot_batches(ctx, uniq, batch=batch)
        out['snapshot_errors'] = errors
        if not frames:
            out['error'] = 'futu_no_snapshots' + (('; ' + str(errors)[:200]) if errors else '')
            return out

        import pandas as pd
        snap = pd.concat(frames, ignore_index=True)
        quotes = []
        for _, row in snap.iterrows():
            q = normalize_futu_row(row)
            if q and q.get('strike') is not None:
                quotes.append(q)
        out['quotes'] = quotes
        out['n_quotes'] = len(quotes)
        if not quotes:
            out['error'] = 'futu_empty_normalized_quotes'
            return out

        # Prefer the nearest expiry for ATM IV / straddle; GEX uses all loaded expiries
        # so monthly OI walls are not dropped when a weekly is also listed.
        nearest = min(
            (q['expiry'] for q in quotes if q.get('expiry')),
            default=pick[0],
        )
        out['nearest_expiry'] = nearest
        near_quotes = [q for q in quotes if q.get('expiry') == nearest]
        # For the ATM straddle, prefer SPXW PM weeklies on that date (more liquid 0DTE/1DTE).
        spxw_near = [q for q in near_quotes if q.get('product') == 'SPXW']
        straddle_src = spxw_near or near_quotes

        by_all, n_all = aggregate_by_strike(quotes, spot)
        by_near, n_near = aggregate_by_strike(straddle_src, spot)
        out['by'] = by_all
        out['n_used'] = n_all
        out['n_near'] = n_near
        flip = gamma_flip(by_all, spot)
        call_wall, put_wall, far_call_wall, far_put_wall = call_put_walls(by_all, spot=spot)
        st = atm_straddle(by_near, spot)
        out['zero_gamma'] = flip
        out['call_wall'] = call_wall
        out['put_wall'] = put_wall
        out['far_call_wall'] = far_call_wall
        out['far_put_wall'] = far_put_wall
        out['atm_iv'] = (st or {}).get('atm_iv') or atm_iv(by_near, spot)
        out['straddle'] = st
        em, em_note = expected_move_to_close(st, nearest, now=now)
        out['em_close'] = None if em is None else round(float(em), 4)
        out['em_note'] = em_note
        return out
    except Exception as e:
        out['error'] = type(e).__name__ + ': ' + str(e)[:300]
        return out
    finally:
        try:
            ctx.close()
        except Exception:
            pass


# --- CBOE delayed-chain helpers (for honest historical evaluation only) ---

_OCC_RE = None


def parse_occ_symbol(sym):
    """Parse ``SPX261120C00400000`` / ``SPXW261119C07325000`` into expiry, cp, strike."""
    global _OCC_RE
    import re
    if _OCC_RE is None:
        _OCC_RE = re.compile(r'^(SPXW?)(\d{6})([CP])(\d{8})$')
    m = _OCC_RE.match(str(sym or '').upper())
    if not m:
        return None
    root, ymd, cp, k = m.groups()
    year = 2000 + int(ymd[0:2])
    month = int(ymd[2:4])
    day = int(ymd[4:6])
    try:
        expiry = datetime(year, month, day).date().isoformat()
    except ValueError:
        return None
    return {
        'root': root,
        'expiry': expiry,
        'cp': cp,
        'strike': int(k) / 1000.0,
    }


def normalize_cboe_option(opt):
    """Map one Cboe delayed options row to the common schema. Returns None if unusable."""
    parsed = parse_occ_symbol(opt.get('option') or opt.get('symbol') or '')
    if not parsed:
        return None
    iv = iv_to_decimal(opt.get('iv') or opt.get('implied_volatility'))
    gamma = _fnum(opt.get('gamma'))
    last = _fnum(opt.get('last_trade_price') or opt.get('last'))
    bid = _fnum(opt.get('bid'))
    ask = _fnum(opt.get('ask'))
    return {
        'code': opt.get('option') or opt.get('symbol'),
        'product': parsed['root'],
        'expiry': parsed['expiry'],
        'strike': parsed['strike'],
        'cp': parsed['cp'],
        'oi': int(_fnum(opt.get('open_interest')) or 0),
        'volume': _fnum(opt.get('volume')) or 0.0,
        'iv': iv,
        'gamma': gamma,
        'delta': _fnum(opt.get('delta')),
        'last': last,
        'bid': bid,
        'ask': ask,
        'update_time': opt.get('last_trade_time'),
        'multiplier': MULTIPLIER,
    }


def chain_from_normalized(quotes, spot, now=None, expiry=None):
    """Run GEX / flip / walls / ATM straddle on an already-normalized chain.

    Used both for a live Futu load and for a Cboe snapshot. Does not
    invent quotes. ``expiry`` defaults to the nearest expiry in ``quotes``.
    """
    now = now or datetime.now(ET)
    spot = float(spot)
    quotes = [q for q in (quotes or []) if q and q.get('strike')]
    if not quotes:
        return {'error': 'empty_chain', 'by': None, 'source': 'normalized'}
    expiries = sorted({q['expiry'] for q in quotes if q.get('expiry')})
    if expiry is None:
        # nearest on or after today if possible, else the soonest listed
        today = now.date().isoformat()
        future = [d for d in expiries if d >= today]
        expiry = (future or expiries)[0] if (future or expiries) else None
    near = [q for q in quotes if q.get('expiry') == expiry]
    spxw_near = [q for q in near if q.get('product') == 'SPXW']
    straddle_src = spxw_near or near or quotes
    # GEX: use all loaded quotes (caller should already have filtered the date window)
    by, n = aggregate_by_strike(quotes, spot)
    by_near, _ = aggregate_by_strike(straddle_src, spot)
    st = atm_straddle(by_near, spot)
    em, em_note = expected_move_to_close(st, expiry, now=now)
    call_wall, put_wall, far_call_wall, far_put_wall = call_put_walls(by, spot=spot)
    return {
        'source': 'normalized',
        'spot': spot,
        'by': by,
        'n_used': n,
        'expiries': expiries,
        'nearest_expiry': expiry,
        'zero_gamma': gamma_flip(by, spot),
        'call_wall': call_wall,
        'put_wall': put_wall,
        'far_call_wall': far_call_wall,
        'far_put_wall': far_put_wall,
        'atm_iv': (st or {}).get('atm_iv') or atm_iv(by_near, spot),
        'straddle': st,
        'em_close': None if em is None else round(float(em), 4),
        'em_note': em_note,
        'gex_convention': (
            'Dealer GEX: call = +OI*gamma*100*spot, put = -OI*gamma*100*spot. '
            'Gamma flip = cumulative net GEX zero crossing closest to spot. '
            'Call/put walls = largest call/put open interest.'
        ),
        'error': None,
    }


def quotes_from_cboe_payload(payload, spot, window=DEFAULT_WINDOW, max_expiries=2, now=None):
    """Extract normalized near-dated quotes from a Cboe delayed chain file."""
    now = now or datetime.now(ET)
    if isinstance(payload, dict) and 'data' in payload and isinstance(payload['data'], dict):
        opts = payload['data'].get('options') or []
        ts = payload.get('timestamp')
    elif isinstance(payload, dict) and 'options' in payload:
        opts = payload['options']
        ts = payload.get('timestamp')
    elif isinstance(payload, list):
        opts = payload
        ts = None
    else:
        return [], {'error': 'unrecognized_cboe_shape'}
    # If the snapshot has a timestamp, prefer it as 'now' so expiry picking is honest.
    meta = {'timestamp_raw': ts, 'n_raw': len(opts)}
    quotes = []
    for opt in opts:
        q = normalize_cboe_option(opt)
        if not q or not q['strike']:
            continue
        if window and spot and abs(q['strike'] / float(spot) - 1) > window:
            continue
        quotes.append(q)
    expiries = sorted({q['expiry'] for q in quotes if q.get('expiry')})
    start = now.date() if now else (datetime.fromisoformat(expiries[0]).date() if expiries else None)
    pick = set(pick_expiries(expiries, start, max_expiries=max_expiries) if start else expiries[:max_expiries])
    quotes = [q for q in quotes if q.get('expiry') in pick]
    meta['expiries_all'] = expiries
    meta['expiries_used'] = sorted(pick)
    meta['n_window'] = len(quotes)
    return quotes, meta


GEX_CONVENTION_SHORT = (
    'Dealer GEX = ± OI × gamma × 100 × spot (call +, put −). '
    'Flip = cumulative GEX zero crossing. Walls = max call/put OI.'
)
