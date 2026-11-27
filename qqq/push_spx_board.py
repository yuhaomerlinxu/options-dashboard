"""Compute the /spx/levels board on this box and POST it to Render. Read-only.

Runs as its own process so a hung Futu/levels call cannot block it.
On startup (and whenever Render reports no snapshot) it first re-posts the
last good local copy, then refreshes with a newly computed payload.
"""
import json, os, ssl, sys, time, urllib.error, urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BJ = ZoneInfo('Asia/Shanghai')
DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DIR)
if DIR not in sys.path:
    sys.path.insert(0, DIR)

RTH_SEC = int(os.environ.get('SPX_BOARD_RTH_SEC', '300'))   # 5 minutes
OFF_SEC = int(os.environ.get('SPX_BOARD_OFF_SEC', '1800'))  # 30 minutes
CHECK_SEC = int(os.environ.get('SPX_BOARD_CHECK_SEC', '60'))
ONCE_CAP_SEC = int(os.environ.get('SPX_BOARD_ONCE_CAP_SEC', '180'))
LOCAL_SNAP = os.environ.get('SPX_BOARD_LOCAL_FILE') or os.path.join(DIR, 'data', 'spx_levels_board_last.json')

def _log(msg):
    print(datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S') + ' BJ board-pusher ' + msg, flush=True)

def _load_env_files():
    for path in (
        os.environ.get('DASH_ENV_FILE'),
        '/workspace/options-dashboard/.env',
        os.path.join(ROOT, '.env'),
        '/workspace/.longport.env',
    ):
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#') or '=' not in line:
                        continue
                    k, _, v = line.partition('=')
                    k, v = k.strip(), v.strip().strip('"').strip("'")
                    if k and k not in os.environ:
                        os.environ[k] = v
        except OSError as e:
            _log('env file skip %s: %s' % (os.path.basename(path), type(e).__name__))

def _dash_token():
    return (os.environ.get('DASH_TOKEN') or '').strip()

def _dash_url():
    return (os.environ.get('RENDER_DASH_URL') or os.environ.get('DASH_URL') or 'https://options-dashboard-pp77.onrender.com').rstrip('/')

def _in_rth():
    try:
        import us_market_calendar as cal
        return bool(cal.in_rth())
    except Exception:
        try:
            import spx_snapshot as S
            return bool(S.in_rth())
        except Exception:
            return False

def _interval():
    return RTH_SEC if _in_rth() else OFF_SEC

def _auth_headers():
    token = _dash_token()
    if not token or len(token) < 16:
        return None
    return {
        'Authorization': 'Bearer ' + token,
        'Accept': 'application/json',
    }

def remote_status(timeout=15):
    headers = _auth_headers()
    if not headers:
        return {'present': False, 'error': 'DASH_TOKEN missing or too short'}
    url = _dash_url() + '/api/spx/levels_board'
    req = urllib.request.Request(url, headers=headers, method='GET')
    ctx = ssl.create_default_context()
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return json.loads(r.read().decode('utf-8', 'replace'))
    except Exception as e:
        return {'present': False, 'error': type(e).__name__ + ': ' + str(e)[:200]}

def load_local():
    if not LOCAL_SNAP or not os.path.isfile(LOCAL_SNAP):
        return None
    try:
        with open(LOCAL_SNAP) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and (doc.get('bars') or []):
            return doc
    except Exception as e:
        _log('local snap unreadable: ' + type(e).__name__)
    return None

def save_local(doc):
    if not isinstance(doc, dict) or not (doc.get('bars') or []):
        return
    try:
        os.makedirs(os.path.dirname(LOCAL_SNAP), exist_ok=True)
        tmp = LOCAL_SNAP + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(doc, f, ensure_ascii=False, default=str)
        os.replace(tmp, LOCAL_SNAP)
    except Exception as e:
        _log('local snap save failed: ' + type(e).__name__)

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

def restore_if_missing():
    """If Render has no snapshot, re-POST the last local copy immediately (no recompute)."""
    st = remote_status()
    if st.get('present'):
        return {'ok': True, 'action': 'already_present', 'pushed_bj': st.get('pushed_bj'), 'age_sec': st.get('age_sec')}
    if st.get('error'):
        _log('status check: ' + str(st.get('error')))
    doc = load_local()
    if not doc:
        return {'ok': False, 'action': 'missing_no_local', 'error': st.get('error') or 'no local snap'}
    res = post(doc)
    if res.get('ok'):
        _log('restored last board to Render last=%s session=%s pushed=%s' % (doc.get('last'), doc.get('session'), res.get('pushed_bj')))
        res['action'] = 'restored'
    else:
        _log('restore failed: ' + str(res.get('error')))
        res['action'] = 'restore_failed'
    return res

def once():
    t0 = time.time()
    doc = build()
    if doc.get('error') and not (doc.get('bars') or []):
        _log('build failed: ' + str(doc['error']))
        return doc
    save_local(doc)
    res = post(doc)
    dt = int(time.time() - t0)
    if res.get('ok'):
        _log('pushed ok last=%s session=%s bars=%s in %ss' % (doc.get('last'), doc.get('session'), len(doc.get('bars') or []), dt))
    else:
        _log('push failed: ' + str(res.get('error')) + ((' ' + str(res.get('body'))) if res.get('body') else ''))
    return res if res.get('ok') else {**res, 'build_ok': True}

def _run_capped(fn, timeout, label='board once'):
    import threading
    box = {}
    def _run():
        try:
            box['r'] = fn()
        except Exception as e:
            box['e'] = e
    th = threading.Thread(target=_run, name=label, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        _log(label + ' timed out after %ss' % timeout)
        return {'ok': False, 'error': 'timeout_%s' % timeout}
    if 'e' in box:
        _log(label + ' error: ' + type(box['e']).__name__ + ': ' + str(box['e'])[:200])
        return {'ok': False, 'error': str(box['e'])[:200]}
    return box.get('r') or {'ok': False, 'error': 'empty'}

def loop():
    _load_env_files()
    if not os.environ.get('LBSAFE') and os.path.isfile(os.path.join(ROOT, 'lbsafe')):
        os.environ['LBSAFE'] = os.path.join(ROOT, 'lbsafe')
    token = _dash_token()
    if not token or len(token) < 16:
        _log('refusing to start: DASH_TOKEN missing or too short')
        return 2
    _log('starting url=%s rth_sec=%s off_sec=%s check_sec=%s' % (_dash_url(), RTH_SEC, OFF_SEC, CHECK_SEC))
    # Always try to put *something* on Render first, then a fresh compute.
    restore_if_missing()
    _run_capped(once, ONCE_CAP_SEC, 'fresh board')
    last_push = time.time()
    while True:
        time.sleep(CHECK_SEC)
        st = remote_status()
        if not st.get('present'):
            _log('Render has no board snapshot; restoring now')
            restore_if_missing()
            _run_capped(once, ONCE_CAP_SEC, 'fresh board')
            last_push = time.time()
        elif time.time() - last_push >= _interval():
            _run_capped(once, ONCE_CAP_SEC, 'fresh board')
            last_push = time.time()
    return 0

if __name__ == '__main__':
    _load_env_files()
    if not os.environ.get('LBSAFE') and os.path.isfile(os.path.join(ROOT, 'lbsafe')):
        os.environ['LBSAFE'] = os.path.join(ROOT, 'lbsafe')
    if '--loop' in sys.argv:
        raise SystemExit(loop() or 0)
    if '--restore' in sys.argv:
        r = restore_if_missing()
        print(json.dumps({k: r.get(k) for k in ('ok', 'error', 'action', 'pushed_bj', 'age_sec') if k in r}, ensure_ascii=False))
        raise SystemExit(0 if r.get('ok') else 1)
    r = once()
    print(json.dumps({k: r.get(k) for k in ('ok', 'error', 'pushed_bj', 'last', 'n_bars', 'n_levels', 'session', 'build_ok') if k in r}, ensure_ascii=False))
