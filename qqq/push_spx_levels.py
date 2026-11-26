"""Push Futu 0DTE/near-expiry SPXW strikes to /api/spx/levels_snapshot. Read-only."""
import json, os, ssl, time, urllib.error, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BJ = ZoneInfo('Asia/Shanghai')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def _log(msg):
    print(datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S') + ' BJ levels-pusher ' + msg, flush=True)

def _dash_token():
    return (os.environ.get('DASH_TOKEN') or '').strip()

def _dash_url():
    return (os.environ.get('RENDER_DASH_URL') or os.environ.get('DASH_URL') or 'https://options-dashboard-pp77.onrender.com').rstrip('/')

def _spot():
    import subprocess
    lb = os.environ.get('LBSAFE') or os.path.join(ROOT, 'lbsafe')
    r = subprocess.run([lb, 'quote', '.SPX.US', '--format', 'json'], capture_output=True, text=True, timeout=15)
    d = json.loads(r.stdout)[0]
    return float(d['last']), d

def build():
    import futu_spx as F
    if not F.opend_reachable():
        return {'error': 'opend_unreachable'}
    try:
        spot, _q = _spot()
    except Exception as e:
        return {'error': 'spot_failed: ' + type(e).__name__ + ': ' + str(e)[:160]}
    data = F.load_spx_chain(spot=spot)
    if not isinstance(data, dict) or data.get('error') or not data.get('by'):
        return {'error': 'futu_chain: ' + str((data or {}).get('error') or 'empty')}
    by = data['by']
    st = data.get('straddle') or F.atm_straddle(by, spot)
    walls = (data.get('call_wall'), data.get('put_wall'))
    if walls[0] is None:
        cw, pw, _, _ = F.call_put_walls(by, spot)
        walls = (cw, pw)
    rows = F.gex_rows(by, top=30)
    # Keep strikes with meaningful OI near spot for dotted rows.
    strikes = []
    for r in rows:
        if (r.get('oi_c') or 0) + (r.get('oi_p') or 0) <= 0:
            continue
        strikes.append({
            'k': r['k'],
            'oi_c': r.get('oi_c') or 0,
            'oi_p': r.get('oi_p') or 0,
            'gex_oi': r.get('gex_oi') or 0,
        })
    expiry = data.get('nearest_expiry') or (data.get('expiries_used') or [None])[0]
    return {
        'schema': 1,
        'chain_source': 'futu',
        'spot': spot,
        'expiry': expiry,
        'expiries_used': data.get('expiries_used'),
        'call_wall': walls[0] if not isinstance(walls[0], dict) else walls[0],
        'put_wall': walls[1] if not isinstance(walls[1], dict) else walls[1],
        'zero_gamma': data.get('zero_gamma') or data.get('gamma_flip'),
        'atm_iv': data.get('atm_iv') if not isinstance(data.get('atm_iv'), dict) else (data.get('atm_iv') or {}).get('iv'),
        'straddle': (st or {}).get('straddle') if isinstance(st, dict) else data.get('straddle_move'),
        'straddle_move': (st or {}).get('implied_move') if isinstance(st, dict) else data.get('straddle_move'),
        'one_sigma_move': (st or {}).get('one_sigma_move') if isinstance(st, dict) else None,
        'strikes': strikes,
        'fetched_at_bj': data.get('fetched_at_bj') or datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S'),
        'n_quotes': len(data.get('quotes') or []),
    }

def post(doc, url=None, token=None, timeout=45):
    url = (url or _dash_url()) + '/api/spx/levels_snapshot'
    token = token or _dash_token()
    if not token or len(token) < 16:
        return {'ok': False, 'error': 'DASH_TOKEN missing or too short'}
    body = json.dumps(doc, ensure_ascii=False, default=str).encode()
    req = urllib.request.Request(url, data=body, method='POST', headers={
        'Content-Type': 'application/json; charset=utf-8',
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json',
    })
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read().decode('utf-8', 'replace')
            try:
                return json.loads(raw)
            except ValueError:
                return {'ok': False, 'error': 'non-json response', 'status': r.status}
    except urllib.error.HTTPError as e:
        raw = e.read().decode('utf-8', 'replace')[:400]
        return {'ok': False, 'error': f'HTTP {e.code}', 'body': raw}
    except Exception as e:
        return {'ok': False, 'error': type(e).__name__ + ': ' + str(e)[:200]}

def once():
    doc = build()
    if doc.get('error'):
        _log('build failed: ' + str(doc['error']))
        return doc
    res = post(doc)
    if res.get('ok'):
        _log('pushed ok expiry=%s strikes=%s sigma=%s' % (doc.get('expiry'), len(doc.get('strikes') or []), doc.get('one_sigma_move')))
    else:
        _log('push failed: ' + str(res.get('error')) + ((' ' + str(res.get('body'))) if res.get('body') else ''))
    return res if res.get('ok') else {**res, 'build_ok': True}

if __name__ == '__main__':
    r = once()
    print(json.dumps({k: r.get(k) for k in ('ok', 'error', 'pushed_bj', 'expiry', 'n_strikes', 'build_ok') if k in r}, ensure_ascii=False))
