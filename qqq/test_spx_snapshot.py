"""Unit tests for the Futu SPX snapshot store and HTTP ingest.

No live market data and no OpenD required. Fixture numbers are only
used to exercise validation and freshness, not as forecasts.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo
from http.server import ThreadingHTTPServer
from threading import Thread
import urllib.request
import urllib.error

import spx_snapshot as S

ET = ZoneInfo('America/New_York')
BJ = ZoneInfo('Asia/Shanghai')


def fixture(estimate=7325.0, expiry='2026-11-20'):
    return {
        'schema': 1,
        'chain_source': 'futu',
        'spot_source': 'longbridge',
        'computed_bj': '2026-11-19 08:00:00',
        'asof_bj': '2026-11-19 05:00:00',
        'asof_et': '2026-11-18 16:00:00 ET',
        'session_date': '2026-11-18',
        'expiry': expiry,
        'close_estimate': {
            'estimate': estimate,
            'range_50': [estimate - 50, estimate + 50],
            'range_75': [estimate - 90, estimate + 90],
            'spot': estimate,
            'zero_gamma': 7450.0,
            'call_wall': 7500.0,
            'put_wall': 7100.0,
            'atm_iv': 0.24,
            'straddle_move': 80.0,
            'em_close': 80.0,
            'expiry': expiry,
            'chain_source': 'futu',
            'source': 'futu-spx+longbridge-spot',
            'computed_bj': '2026-11-19 08:00:00',
            'asof_bj': '2026-11-19 05:00:00',
            'blend_mode': 'spot_em',
        },
        'payload': {
            'symbol': 'SPX',
            'expiry': expiry,
            'last': estimate,
            'levels': {'zero_gamma': 7450.0, 'call_wall': 7500.0, 'put_wall': 7100.0},
            'session_date': '2026-11-18',
            'source': {'gex': 'placeholder', 'flow': 'flow note'},
            'approx': 'SPY期权按 SPX/SPY=10 换算',
        },
    }


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        S.clear()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['SPX_SNAPSHOT_FILE'] = os.path.join(self.tmp.name, 'snap.json')

    def tearDown(self):
        S.clear()
        self.tmp.cleanup()
        os.environ.pop('SPX_SNAPSHOT_FILE', None)

    def test_rejects_non_futu(self):
        doc = fixture()
        doc['chain_source'] = 'spy-proxy'
        clean, err = S.validate(doc)
        self.assertIsNone(clean)
        self.assertIn('futu', err)

    def test_rejects_bad_estimate(self):
        doc = fixture()
        doc['close_estimate']['estimate'] = 0
        clean, err = S.validate(doc)
        self.assertIsNone(clean)
        doc = fixture()
        doc['close_estimate']['range_50'] = [8000, 7000]
        clean, err = S.validate(doc)
        self.assertIsNone(clean)

    def test_ingest_and_fresh_off_hours(self):
        # Friday 17:00 ET is after the close, so the off-hours TTL applies.
        fri_after = datetime(2026, 11, 20, 17, 0, tzinfo=ET)
        res = S.ingest(fixture(), received_ts=fri_after.timestamp(), persist=True)
        self.assertTrue(res['ok'])
        self.assertEqual(res['estimate'], 7325.0)
        # Saturday afternoon is still off-hours and fresh
        later = datetime(2026, 11, 21, 15, 0, tzinfo=ET).timestamp()
        snap = S.get_fresh(now=datetime.fromtimestamp(later, ET), now_ts=later)
        self.assertIsNotNone(snap)
        self.assertEqual(snap['close_estimate']['estimate'], 7325.0)
        # 6 days later is stale even off-hours
        stale_ts = fri_after.timestamp() + 6 * 24 * 3600
        self.assertIsNone(S.get_fresh(now=datetime.fromtimestamp(stale_ts, ET), now_ts=stale_ts))

    def test_stale_during_rth(self):
        rth = datetime(2026, 11, 18, 11, 0, tzinfo=ET)
        S.ingest(fixture(), received_ts=rth.timestamp(), persist=False)
        still = rth.timestamp() + 5 * 60
        self.assertIsNotNone(S.get_fresh(now=datetime.fromtimestamp(still, ET), now_ts=still))
        later = rth.timestamp() + 16 * 60
        self.assertIsNone(S.get_fresh(now=datetime.fromtimestamp(later, ET), now_ts=later))

    def test_expiry_filter(self):
        S.ingest(fixture(expiry='2026-11-20'), received_ts=time.time(), persist=False)
        self.assertIsNotNone(S.get_fresh(expiry=None))
        self.assertIsNotNone(S.get_fresh(expiry='2026-11-20'))
        self.assertIsNone(S.get_fresh(expiry='2026-11-27'))

    def test_annotate_payload(self):
        S.ingest(fixture(), received_ts=time.time(), persist=False)
        snap = S.get_snap()
        out = S.annotate_payload(snap['payload'], snap)
        self.assertEqual(out['chain_source'], 'futu')
        self.assertTrue(out['snapshot_fresh'])
        self.assertIn('北京时间', out['source']['pushed'])
        self.assertIn('富途', out['source']['gex'])
        self.assertIn('富途', out['approx'])
        self.assertEqual(out['close_estimate']['estimate'], 7325.0)
        self.assertIn('北京时间', out['close_estimate']['note'])

    def test_status_and_persist(self):
        S.ingest(fixture(), received_ts=time.time(), persist=True)
        st = S.status()
        self.assertTrue(st['present'])
        self.assertEqual(st['chain_source'], 'futu')
        self.assertEqual(st['estimate'], 7325.0)
        self.assertTrue(st['pushed_bj'])
        # reload from disk
        S.clear()
        self.assertIsNone(S.get_snap())
        loaded = S.load_persisted()
        self.assertIsNotNone(loaded)
        self.assertEqual(S.status()['estimate'], 7325.0)

    def test_pushed_bj_is_shanghai(self):
        ts = datetime(2026, 11, 19, 0, 0, tzinfo=ET).timestamp()  # 13:00 BJ
        S.ingest(fixture(), received_ts=ts, persist=False)
        self.assertEqual(S.get_snap()['pushed_bj'], '2026-11-19 13:00:00')


class HttpIngestTests(unittest.TestCase):
    """Stand-alone handler: same auth idea (Bearer DASH_TOKEN) and ingest."""

    def setUp(self):
        S.clear()
        self.tmp = tempfile.TemporaryDirectory()
        os.environ['SPX_SNAPSHOT_FILE'] = os.path.join(self.tmp.name, 'snap.json')
        self.token = 't' * 20
        from http.server import BaseHTTPRequestHandler
        import hmac as _hmac

        token = self.token
        class H(BaseHTTPRequestHandler):
            def _auth(self):
                h = self.headers.get('Authorization') or ''
                if h.startswith('Bearer '):
                    return _hmac.compare_digest(h.split(' ', 1)[1].strip(), token)
                return False
            def _j(self, o, c=200):
                b = json.dumps(o).encode()
                self.send_response(c)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(b)))
                self.end_headers()
                self.wfile.write(b)
            def do_GET(self):
                if not self._auth():
                    self._j({'error': 'unauthorized'}, 401); return
                if self.path.split('?')[0] == '/api/strategy/spx_snapshot':
                    self._j(S.status()); return
                self._j({'error': 'nf'}, 404)
            def do_POST(self):
                if not self._auth():
                    self._j({'error': 'unauthorized'}, 401); return
                n = int(self.headers.get('Content-Length') or 0)
                doc = json.loads(self.rfile.read(n))
                self._j(S.ingest(doc, persist=False), 200 if True else 400)
            def log_message(self, *a):
                pass

        self.httpd = ThreadingHTTPServer(('127.0.0.1', 0), H)
        self.port = self.httpd.server_address[1]
        self.th = Thread(target=self.httpd.serve_forever, daemon=True)
        self.th.start()

    def tearDown(self):
        self.httpd.shutdown()
        S.clear()
        self.tmp.cleanup()

    def _req(self, method, path, body=None, token=None):
        headers = {'Authorization': 'Bearer ' + (token if token is not None else self.token)}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers['Content-Type'] = 'application/json'
        req = urllib.request.Request(
            f'http://127.0.0.1:{self.port}{path}', data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode())

    def test_rejects_bad_token(self):
        code, _ = self._req('GET', '/api/strategy/spx_snapshot', token='nope' + 'x' * 16)
        self.assertEqual(code, 401)

    def test_post_and_get(self):
        code, res = self._req('POST', '/api/strategy/spx_snapshot', fixture())
        self.assertEqual(code, 200)
        self.assertTrue(res['ok'])
        code, st = self._req('GET', '/api/strategy/spx_snapshot')
        self.assertEqual(code, 200)
        self.assertTrue(st['fresh'])
        self.assertEqual(st['estimate'], 7325.0)
        self.assertEqual(st['chain_source'], 'futu')
        self.assertTrue(st['pushed_bj'])


if __name__ == '__main__':
    unittest.main()
