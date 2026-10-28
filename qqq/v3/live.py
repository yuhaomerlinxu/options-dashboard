"""Live adapter: v3 rule on the dashboard's real-time 1m bars (closed bars only, regular session)."""
import os, sys, time
sys.path.insert(0, os.path.dirname(__file__))
from datetime import datetime
from feat import features, BJ, ET
import rule
def compute(bars, now=None):
    now = now or time.time()
    b1 = [b for b in bars if b['t'] + 60 <= now]
    out = []
    for s in rule.signals(features(b1)):
        end = int(datetime.strptime('%s %s' % (s['day'], s['et']), '%Y-%m-%d %H:%M').replace(tzinfo=ET).timestamp())
        out.append({'strategy': '新规则v3', 'setup': s['setup'], 'direction': 'bullish' if s['dir'] == 'CALL' else 'bearish',
                    'price': s['price'], 'ts': end, 'session': '盘中',
                    'trigger_time_bj': datetime.fromtimestamp(end, BJ).strftime('%Y-%m-%d %H:%M:%S'),
                    'trigger_time_et': datetime.fromtimestamp(end, ET).strftime('%Y-%m-%d %H:%M')})
    return out
