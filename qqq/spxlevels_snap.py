"""Box-pushed Futu 0DTE strike snapshot for /spx/levels. Render cannot reach OpenD."""
import json, os, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo

BJ = ZoneInfo('Asia/Shanghai')
SCHEMA = 1
_lk = threading.Lock()
_snap = None
_PATHS = (
    os.environ.get('SPX_LEVELS_SNAP_FILE'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'spx_levels_snapshot.json'),
)

def now_bj(ts=None):
    t = datetime.fromtimestamp(ts if ts is not None else time.time(), BJ)
    return t.strftime('%Y-%m-%d %H:%M:%S')

def _file_path():
    for p in _PATHS:
        if p:
            return p
    return _PATHS[-1]

def validate(doc):
    if not isinstance(doc, dict):
        return None, 'body must be a JSON object'
    if str(doc.get('chain_source') or '').strip().lower() != 'futu':
        return None, 'chain_source must be futu'
    strikes = doc.get('strikes')
    if strikes is not None and not isinstance(strikes, list):
        return None, 'strikes must be a list'
    clean = {
        'schema': SCHEMA,
        'chain_source': 'futu',
        'spot': doc.get('spot'),
        'expiry': doc.get('expiry'),
        'expiries_used': doc.get('expiries_used'),
        'call_wall': doc.get('call_wall'),
        'put_wall': doc.get('put_wall'),
        'zero_gamma': doc.get('zero_gamma'),
        'atm_iv': doc.get('atm_iv'),
        'straddle': doc.get('straddle'),
        'straddle_move': doc.get('straddle_move'),
        'one_sigma_move': doc.get('one_sigma_move'),
        'strikes': strikes or [],
        'fetched_at_bj': doc.get('fetched_at_bj'),
        'asof_bj': doc.get('asof_bj') or doc.get('fetched_at_bj'),
    }
    return clean, None

def ingest(doc, received_ts=None, persist=True):
    clean, err = validate(doc)
    if err:
        return {'ok': False, 'error': err}
    ts = received_ts or time.time()
    clean['pushed_ts'] = ts
    clean['pushed_bj'] = now_bj(ts)
    global _snap
    with _lk:
        _snap = clean
        if persist:
            try:
                path = _file_path()
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + '.tmp'
                with open(tmp, 'w') as f:
                    json.dump(clean, f, ensure_ascii=False, default=str)
                os.replace(tmp, path)
            except Exception:
                pass
    return {'ok': True, 'pushed_bj': clean['pushed_bj'], 'expiry': clean.get('expiry'), 'n_strikes': len(clean.get('strikes') or [])}

def get_snap(max_age=5 * 24 * 3600):
    with _lk:
        s = _snap
    if s and s.get('pushed_ts') and (time.time() - float(s['pushed_ts'])) <= max_age:
        return s
    return s if s else None

def status():
    s = get_snap()
    if not s:
        return {'present': False, 'fresh': False}
    age = int(time.time() - float(s.get('pushed_ts') or 0))
    return {'present': True, 'fresh': age < 6 * 3600, 'age_sec': age, 'pushed_bj': s.get('pushed_bj'), 'expiry': s.get('expiry'), 'n_strikes': len(s.get('strikes') or [])}

def load_persisted():
    global _snap
    path = _file_path()
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and doc.get('pushed_ts'):
            with _lk:
                if _snap is None or float(doc['pushed_ts']) >= float((_snap or {}).get('pushed_ts') or 0):
                    _snap = doc
    except Exception:
        pass

load_persisted()
