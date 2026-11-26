"""Pushed full /spx/levels payload. Render serves this instead of computing on request.

The box has Longbridge and Futu; Render does not reliably finish that work
before the browser or a 60s proxy gives up. Disk on Render is ephemeral, so
the pusher refreshes the snapshot every cycle.
"""
import json, os, threading, time
from datetime import datetime
from zoneinfo import ZoneInfo

BJ = ZoneInfo('Asia/Shanghai')
SCHEMA = 1
_lk = threading.Lock()
_snap = None
_PATHS = (
    os.environ.get('SPX_LEVELS_BOARD_FILE'),
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'spx_levels_board.json'),
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
    bars = doc.get('bars')
    if not isinstance(bars, list) or not bars:
        return None, 'bars required'
    levels = doc.get('levels')
    if levels is not None and not isinstance(levels, list):
        return None, 'levels must be a list'
    # Keep the computed board as-is; only stamp push metadata.
    return doc, None

def ingest(doc, received_ts=None, persist=True):
    clean, err = validate(doc)
    if err:
        return {'ok': False, 'error': err}
    ts = received_ts or time.time()
    clean = dict(clean)
    clean['schema'] = SCHEMA
    clean['board_source'] = 'box-push'
    clean['board_pushed_ts'] = ts
    clean['board_pushed_bj'] = now_bj(ts)
    clean['from_snapshot'] = True
    clean['ok'] = True
    if 'error' in clean:
        clean.pop('error', None)
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
    return {
        'ok': True,
        'pushed_bj': clean['board_pushed_bj'],
        'n_bars': len(clean.get('bars') or []),
        'n_levels': len(clean.get('levels') or []),
        'session': clean.get('session'),
        'last': clean.get('last'),
    }

def get_snap():
    with _lk:
        s = _snap
    return s

def status():
    s = get_snap()
    if not s:
        return {'present': False, 'fresh': False}
    age = int(time.time() - float(s.get('board_pushed_ts') or 0))
    return {
        'present': True,
        'fresh': age < 6 * 3600,
        'age_sec': age,
        'pushed_bj': s.get('board_pushed_bj'),
        'session': s.get('session'),
        'last': s.get('last'),
        'n_bars': len(s.get('bars') or []),
    }

def load_persisted():
    global _snap
    path = _file_path()
    if not path or not os.path.isfile(path):
        return
    try:
        with open(path) as f:
            doc = json.load(f)
        if isinstance(doc, dict) and doc.get('bars') and doc.get('board_pushed_ts'):
            with _lk:
                if _snap is None or float(doc['board_pushed_ts']) >= float((_snap or {}).get('board_pushed_ts') or 0):
                    _snap = doc
    except Exception:
        pass

load_persisted()
