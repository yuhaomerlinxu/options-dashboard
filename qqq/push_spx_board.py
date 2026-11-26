"""Compute the /spx/levels board on this box and POST it to Render. Read-only."""
import json, os, ssl, sys, time, urllib.error, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BJ = ZoneInfo('Asia/Shanghai')
DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DIR)
if DIR not in sys.path:
    sys.path.insert(0, DIR)

def _log(msg):
    print(datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S') + ' BJ board-pusher ' + msg, flush=True)

def _dash_token():
    return (os.environ.get('DASH_TOKEN') or '').strip()

def _dash_url():
    return (os.environ.get('RENDER_DASH_URL') or os.environ.get('DASH_URL') or 'https://options-dashboard-pp77.onrender.com').rstrip('/')

def build():
    import spxlevelsapi
    # Drop any in-process cache so the push is a fresh compute on this box.
    with spxlevelsapi._lk:
        spxlevelsapi._c.pop('payload', None)
    doc = spxlevelsapi.payload()
    if not isinstance(doc, dict):
        return {'error': 'payload_not_dict'}
    if doc.get('error') and not (doc.get('bars') or []):
        return {'error': 'payload: ' + str(doc.get('error'))[:200]}
    if not (doc.get('bars') or []):
        return {'error': 'payload_empty_bars'}
    return doc

def post(doc, url=None, token=None, timeout=45):
    url = (url or _dash_url()) + '/api/spx/levels_board'
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
    t0 = time.time()
    doc = build()
    if doc.get('error') and not (doc.get('bars') or []):
        _log('build failed: ' + str(doc['error']))
        return doc
    res = post(doc)
    dt = int(time.time() - t0)
    if res.get('ok'):
        _log('pushed ok last=%s session=%s bars=%s in %ss' % (doc.get('last'), doc.get('session'), len(doc.get('bars') or []), dt))
    else:
        _log('push failed: ' + str(res.get('error')) + ((' ' + str(res.get('body'))) if res.get('body') else ''))
    return res if res.get('ok') else {**res, 'build_ok': True}

if __name__ == '__main__':
    r = once()
    print(json.dumps({k: r.get(k) for k in ('ok', 'error', 'pushed_bj', 'last', 'n_bars', 'n_levels', 'session', 'build_ok') if k in r}, ensure_ascii=False))
