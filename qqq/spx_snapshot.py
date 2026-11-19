"""In-memory (and on-disk) Futu SPX snapshot for the Render dashboard.

The box, which can reach Futu OpenD, computes the SPX close estimate and
strategy payload, then POSTs them here. Render cannot reach OpenD, so it
serves a fresh pushed snapshot and falls back to the existing SPY-scaled
path when the snapshot is stale or missing.

Never invents market data: ingest rejects payloads that are not marked as
Futu-sourced or that lack the required estimate fields.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
BJ = ZoneInfo('Asia/Shanghai')

# During US RTH the pusher should refresh every few minutes; after this
# age we fall back so a dead pusher is visible. Off-hours (including the
# weekend) the last Futu close stays valid until the next session.
FRESH_RTH_SEC = 15 * 60
FRESH_OFF_MAX_SEC = 5 * 24 * 3600
SCHEMA = 1
MAX_BODY = 2_000_000
SPX_LO, SPX_HI = 1000.0, 20000.0

_lock = threading.Lock()
_snap = None  # type: dict | None

_DEFAULT_FILE = os.environ.get(
    'SPX_SNAPSHOT_FILE',
    os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'spx_snapshot.json'),
)


def now_bj(ts=None):
    dt = datetime.now(BJ) if ts is None else datetime.fromtimestamp(float(ts), BJ)
    return dt.strftime('%Y-%m-%d %H:%M:%S')


def in_rth(now=None):
    now = now or datetime.now(ET)
    if now.weekday() >= 5:
        return False
    t = now.hour * 60 + now.minute
    return (9 * 60 + 30) <= t < (16 * 60)


def _file_path():
    return os.environ.get('SPX_SNAPSHOT_FILE') or _DEFAULT_FILE


def _valid_number(v, lo=None, hi=None):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if lo is not None and x < lo:
        return None
    if hi is not None and x > hi:
        return None
    return x


def _valid_range(rng, lo=SPX_LO, hi=SPX_HI):
    if not isinstance(rng, (list, tuple)) or len(rng) != 2:
        return None
    a, b = _valid_number(rng[0], lo, hi), _valid_number(rng[1], lo, hi)
    if a is None or b is None or a > b:
        return None
    return [a, b]


def validate(doc):
    """Return (clean_doc, error). clean_doc is None on failure."""
    if not isinstance(doc, dict):
        return None, 'body must be a JSON object'
    src = str(doc.get('chain_source') or '').strip().lower()
    if src != 'futu':
        return None, 'chain_source must be futu'
    est = doc.get('close_estimate')
    if not isinstance(est, dict):
        return None, 'close_estimate is required'
    estimate = _valid_number(est.get('estimate'), SPX_LO, SPX_HI)
    if estimate is None:
        return None, 'close_estimate.estimate is missing or out of range'
    r50 = _valid_range(est.get('range_50'))
    r75 = _valid_range(est.get('range_75'))
    if r50 is None or r75 is None:
        return None, 'close_estimate ranges are missing or invalid'
    payload = doc.get('payload')
    if payload is not None and not isinstance(payload, dict):
        return None, 'payload must be an object when present'
    if payload:
        sym = str(payload.get('symbol') or payload.get('bar_symbol') or 'SPX').upper()
        if 'SPX' not in sym and payload.get('symbol') not in (None, ''):
            return None, 'payload.symbol must be SPX'
        levels = payload.get('levels')
        if levels is not None and not isinstance(levels, dict):
            return None, 'payload.levels must be an object'
    clean = {
        'schema': SCHEMA,
        'chain_source': 'futu',
        'spot_source': doc.get('spot_source') or 'longbridge',
        'close_estimate': est,
        'payload': payload,
        'computed_bj': doc.get('computed_bj') or est.get('computed_bj'),
        'asof_bj': doc.get('asof_bj') or est.get('asof_bj'),
        'asof_et': doc.get('asof_et') or est.get('asof_et') or (payload or {}).get('asof_et'),
        'session_date': doc.get('session_date') or (payload or {}).get('session_date'),
        'expiry': doc.get('expiry') or est.get('expiry') or (payload or {}).get('expiry'),
    }
    # Optional extras copied through when present (never invented).
    for k in ('call_wall', 'put_wall', 'zero_gamma', 'atm_iv', 'straddle_move',
              'em_close', 'spot', 'n_quotes', 'gex_by_strike', 'em_note'):
        if k in doc:
            clean[k] = doc[k]
        elif k in est:
            clean[k] = est[k]
    return clean, None


def is_fresh(snap, now=None, now_ts=None):
    if not snap:
        return False
    pushed = snap.get('pushed_ts')
    if not pushed:
        return False
    ts = time.time() if now_ts is None else float(now_ts)
    age = ts - float(pushed)
    if age < 0:
        return False
    if in_rth(now):
        return age <= FRESH_RTH_SEC
    return age <= FRESH_OFF_MAX_SEC


def expiry_ok(snap, expiry=None):
    """True when the caller did not ask for a different expiry than the snapshot."""
    if not expiry:
        return True
    got = (snap or {}).get('expiry') or ((snap or {}).get('close_estimate') or {}).get('expiry')
    if not got:
        return True
    return str(got)[:10] == str(expiry)[:10]


def ingest(doc, received_ts=None, persist=True):
    clean, err = validate(doc)
    if err:
        return {'ok': False, 'error': err}
    ts = time.time() if received_ts is None else float(received_ts)
    clean['pushed_ts'] = ts
    clean['pushed_bj'] = now_bj(ts)
    # The box may send its own pushed_ts meaning "computed at"; keep both.
    if doc.get('computed_ts'):
        try:
            clean['computed_ts'] = float(doc['computed_ts'])
        except (TypeError, ValueError):
            pass
    with _lock:
        global _snap
        _snap = clean
        if persist:
            try:
                _save(clean)
            except Exception as e:
                clean['persist_error'] = type(e).__name__
    return {
        'ok': True,
        'pushed_bj': clean['pushed_bj'],
        'expiry': clean.get('expiry'),
        'estimate': clean['close_estimate'].get('estimate'),
        'fresh': True,
    }


def get_snap():
    with _lock:
        return _snap


def get_fresh(expiry=None, now=None, now_ts=None):
    snap = get_snap()
    if not is_fresh(snap, now=now, now_ts=now_ts):
        return None
    if not expiry_ok(snap, expiry):
        return None
    return snap


def clear():
    global _snap
    with _lock:
        _snap = None


def annotate_estimate(est, snap):
    out = dict(est or {})
    out['chain_source'] = 'futu'
    out['source'] = out.get('source') or 'futu-spx+longbridge-spot'
    out['pushed_bj'] = snap.get('pushed_bj')
    out['pushed_ts'] = snap.get('pushed_ts')
    out['snapshot_fresh'] = True
    note = out.get('note') or 'SPX 期权来自富途 OpenAPI（SPXW/SPX），现价来自长桥 .SPX.US'
    if snap.get('pushed_bj') and '北京' not in str(note):
        note = note + '；推送时间 %s 北京时间' % snap['pushed_bj']
    out['note'] = note
    return out


def annotate_payload(payload, snap):
    out = dict(payload or {})
    out['chain_source'] = 'futu'
    out['pushed_bj'] = snap.get('pushed_bj')
    out['pushed_ts'] = snap.get('pushed_ts')
    out['snapshot_fresh'] = True
    if snap.get('close_estimate') and not out.get('close_estimate'):
        out['close_estimate'] = annotate_estimate(snap['close_estimate'], snap)
    elif out.get('close_estimate'):
        out['close_estimate'] = annotate_estimate(out['close_estimate'], snap)
    src = out.get('source')
    if isinstance(src, dict):
        src = dict(src)
        src['gex'] = (
            '富途 OpenAPI SPX/SPXW 期权链 OI × Gamma × 100 × spot，Call 为正、Put 为负'
            '（本机计算后推送到看板）'
        )
        src['pushed'] = '推送时间 %s 北京时间' % (snap.get('pushed_bj') or '—')
        if snap.get('asof_bj'):
            src['asof'] = '数据截至 %s 北京时间' % snap['asof_bj']
        out['source'] = src
    approx = out.get('approx')
    if approx and 'SPY' in str(approx) and '富途' not in str(approx):
        out['approx'] = 'SPX 期权来自富途 OpenAPI（SPXW/SPX），现价来自长桥 .SPX.US'
    return out


def status():
    snap = get_snap()
    if not snap:
        return {'present': False, 'fresh': False}
    est = snap.get('close_estimate') or {}
    levels = ((snap.get('payload') or {}).get('levels')) or {}
    age = None
    if snap.get('pushed_ts') is not None:
        age = int(time.time() - float(snap['pushed_ts']))
    rth = in_rth()
    return {
        'present': True,
        'fresh': is_fresh(snap),
        'in_rth': rth,
        'stale_after_sec': FRESH_RTH_SEC if rth else FRESH_OFF_MAX_SEC,
        'age_sec': age,
        'pushed_bj': snap.get('pushed_bj'),
        'computed_bj': snap.get('computed_bj') or est.get('computed_bj'),
        'asof_bj': snap.get('asof_bj') or est.get('asof_bj'),
        'asof_et': snap.get('asof_et'),
        'session_date': snap.get('session_date'),
        'chain_source': snap.get('chain_source'),
        'spot_source': snap.get('spot_source'),
        'expiry': snap.get('expiry'),
        'estimate': est.get('estimate'),
        'range_50': est.get('range_50'),
        'range_75': est.get('range_75'),
        'spot': est.get('spot'),
        'zero_gamma': est.get('zero_gamma') or levels.get('zero_gamma'),
        'call_wall': est.get('call_wall') or levels.get('call_wall'),
        'put_wall': est.get('put_wall') or levels.get('put_wall'),
        'atm_iv': est.get('atm_iv'),
        'straddle_move': est.get('straddle_move'),
        'em_close': est.get('em_close'),
        'blend_mode': est.get('blend_mode'),
    }


def _save(clean):
    path = _file_path()
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='spx_snap_', dir=folder or None)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(clean, f, ensure_ascii=False, default=str)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_persisted():
    """Load a previously ingested snapshot from disk (e.g. after a process restart)."""
    path = _file_path()
    try:
        with open(path) as f:
            doc = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or not doc.get('pushed_ts'):
        return None
    # Already ingested once; trust the stored shape but re-validate.
    clean, err = validate(doc)
    if err:
        return None
    clean['pushed_ts'] = float(doc['pushed_ts'])
    clean['pushed_bj'] = doc.get('pushed_bj') or now_bj(clean['pushed_ts'])
    with _lock:
        global _snap
        _snap = clean
    return clean


# Load any leftover file at import so a Render restart can reuse it
# until the next box push (disk is ephemeral on free plan, but this
# still helps a normal process bounce).
try:
    load_persisted()
except Exception:
    pass
