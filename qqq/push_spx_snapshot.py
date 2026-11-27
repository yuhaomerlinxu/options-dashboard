#!/usr/bin/env python3
"""Compute the Futu SPX close estimate on this box and POST it to Render.

Read-only: opens a Futu quote context only, never a trade context, and
never places, modifies or cancels orders.

Intended to run as a long-lived process on the box (OpenD is local).
Refresh every few minutes during the US session, and more slowly
off-hours so a sleeping Render instance picks up the last close after
it wakes. The supervisor script restarts this process if it exits.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo('America/New_York')
BJ = ZoneInfo('Asia/Shanghai')
DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(DIR)
if DIR not in sys.path:
    sys.path.insert(0, DIR)

DEFAULT_URL = 'https://options-dashboard-pp77.onrender.com'
RTH_INTERVAL = int(os.environ.get('SPX_PUSH_RTH_SEC', '180'))
OFF_INTERVAL = int(os.environ.get('SPX_PUSH_OFF_SEC', '900'))
FAIL_BACKOFF_CAP = 300


def _log(msg):
    ts = datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')
    print(f'{ts} BJ {msg}', flush=True)


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
            _log(f'env file skip {os.path.basename(path)}: {type(e).__name__}')


def _dash_token():
    return (os.environ.get('DASH_TOKEN') or '').strip()


def _dash_url():
    return (os.environ.get('RENDER_DASH_URL') or os.environ.get('DASH_URL') or DEFAULT_URL).rstrip('/')


def _interval():
    # Late import so tests can stub; strategyapi.in_rth uses echoapi.
    try:
        import spx_snapshot as S
        return RTH_INTERVAL if S.in_rth() else OFF_INTERVAL
    except Exception:
        return OFF_INTERVAL


def build_snapshot():
    """Return a Futu-sourced snapshot dict, or a dict with 'error'."""
    import strategyapi
    est = strategyapi.close_estimate('SPX')
    if not isinstance(est, dict) or est.get('error'):
        return {'error': 'close_estimate_failed: ' + str((est or {}).get('error') or 'empty')}
    if est.get('chain_source') != 'futu':
        return {
            'error': 'not_futu',
            'chain_source': est.get('chain_source'),
            'source': est.get('source'),
        }
    payload = None
    payload_err = None
    try:
        payload = strategyapi.payload('SPX')
        if not isinstance(payload, dict) or payload.get('error'):
            payload_err = (payload or {}).get('error') or 'empty'
            payload = None
        elif payload.get('chain_source') and payload.get('chain_source') != 'futu':
            payload_err = 'payload_chain_source=' + str(payload.get('chain_source'))
            payload = None
    except Exception as e:
        payload_err = type(e).__name__ + ': ' + str(e)[:200]
        payload = None
    doc = {
        'schema': 1,
        'chain_source': 'futu',
        'spot_source': 'longbridge',
        'close_estimate': est,
        'payload': payload,
        'computed_bj': est.get('computed_bj') or datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S'),
        'computed_ts': time.time(),
        'asof_bj': est.get('asof_bj'),
        'asof_et': est.get('asof_et'),
        'session_date': (payload or {}).get('session_date'),
        'expiry': est.get('expiry') or (payload or {}).get('expiry'),
        'call_wall': est.get('call_wall'),
        'put_wall': est.get('put_wall'),
        'zero_gamma': est.get('zero_gamma'),
        'atm_iv': est.get('atm_iv'),
        'straddle_move': est.get('straddle_move'),
        'em_close': est.get('em_close'),
        'spot': est.get('spot'),
        'n_quotes': est.get('n_quotes'),
        'em_note': est.get('em_note'),
    }
    if payload_err:
        doc['payload_error'] = payload_err
    return doc


def post_snapshot(doc, url=None, token=None, timeout=45):
    url = (url or _dash_url()) + '/api/strategy/spx_snapshot'
    token = token or _dash_token()
    if not token or len(token) < 16:
        return {'ok': False, 'error': 'DASH_TOKEN missing or too short'}
    body = json.dumps(doc, ensure_ascii=False, default=str).encode()
    req = urllib.request.Request(
        url, data=body, method='POST',
        headers={
            'Content-Type': 'application/json; charset=utf-8',
            'Authorization': 'Bearer ' + token,
            'Accept': 'application/json',
        },
    )
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
    doc = build_snapshot()
    if doc.get('error'):
        return doc
    est = doc.get('close_estimate') or {}
    _log(
        'built futu snapshot estimate=%s range50=%s expiry=%s zg=%s quotes=%s payload=%s'
        % (est.get('estimate'), est.get('range_50'), doc.get('expiry'),
           est.get('zero_gamma'), est.get('n_quotes'),
           'yes' if doc.get('payload') else doc.get('payload_error') or 'no')
    )
    res = post_snapshot(doc)
    if res.get('ok'):
        _log('pushed ok pushed_bj=%s estimate=%s' % (res.get('pushed_bj'), res.get('estimate')))
    else:
        _log('push failed: ' + str(res.get('error')) + ((' ' + str(res.get('body'))) if res.get('body') else ''))
    def _run_capped(label, fn, timeout):
        """Run a side push with a hard cap so a hung Futu/Longbridge call cannot stall the loop overnight."""
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
            _log(label + ' skipped: ' + type(box['e']).__name__ + ': ' + str(box['e'])[:200])
            return {'ok': False, 'error': str(box['e'])[:200]}
        return box.get('r') or {'ok': False, 'error': 'empty'}

    res = dict(res)
    try:
        import push_spx_levels
        lv = _run_capped('levels push', push_spx_levels.once, 120)
        res['levels_ok'] = bool(lv.get('ok'))
        if not lv.get('ok'):
            res['levels_error'] = lv.get('error')
    except Exception as e:
        _log('levels push skipped: ' + type(e).__name__ + ': ' + str(e)[:200])
    # Board push runs in its own process (push_spx_board.py --loop) so a hung
    # Futu/levels call cannot delay it.
    return res if res.get('ok') else {**res, 'build_ok': True}


def loop():
    _load_env_files()
    if not os.environ.get('LBSAFE') and os.path.isfile(os.path.join(ROOT, 'lbsafe')):
        os.environ['LBSAFE'] = os.path.join(ROOT, 'lbsafe')
    token = _dash_token()
    if not token or len(token) < 16:
        _log('refusing to start: DASH_TOKEN missing or too short')
        return 2
    _log('pusher starting url=%s rth_sec=%s off_sec=%s' % (_dash_url(), RTH_INTERVAL, OFF_INTERVAL))
    fail = 0
    while True:
        t0 = time.time()
        try:
            res = once()
            if res.get('ok'):
                fail = 0
                sleep = _interval()
            elif res.get('error') == 'not_futu':
                fail += 1
                sleep = min(FAIL_BACKOFF_CAP, 60 * fail)
                _log('OpenD/Futu unavailable (source=%s), retry in %ss' % (res.get('chain_source'), sleep))
            else:
                fail += 1
                sleep = min(FAIL_BACKOFF_CAP, 30 * fail)
        except Exception:
            fail += 1
            sleep = min(FAIL_BACKOFF_CAP, 30 * fail)
            _log('pusher cycle error:\n' + traceback.format_exc())
        # Don't drift too far if a cycle was slow.
        spent = time.time() - t0
        time.sleep(max(5, sleep - spent))


if __name__ == '__main__':
    if '--once' in sys.argv:
        _load_env_files()
        if not os.environ.get('LBSAFE') and os.path.isfile(os.path.join(ROOT, 'lbsafe')):
            os.environ['LBSAFE'] = os.path.join(ROOT, 'lbsafe')
        res = once()
        # Never print secrets; this result has no token.
        print(json.dumps({k: res.get(k) for k in ('ok', 'error', 'pushed_bj', 'estimate', 'chain_source', 'source', 'fresh', 'expiry', 'build_ok') if k in res or res.get(k) is not None}, ensure_ascii=False))
        sys.exit(0 if res.get('ok') else 1)
    sys.exit(loop() or 0)
