"""Standalone QQQ + SPX 15-minute candle page data (/qqq/m15). Read-only Longbridge market data via chartapi/lbsafe."""
import time,threading
from datetime import datetime
import chartapi
ET=chartapi.ET
DAYS=5
SYMS=(('QQQ','QQQ.US'),('SPX','.SPX.US'))
_lk=threading.Lock();_c={}
def _f(x):
    try:return float(x)
    except (TypeError,ValueError):return None
def _one(name,sym):
    out={'name':name,'symbol':sym,'source':'Longbridge'}
    try:
        bars=chartapi.kline(sym,'15m')
        rd=sorted({datetime.fromtimestamp(b['t'],ET).date() for b in bars if not b['ext']})
        keep=set(rd[-DAYS:])
        bars=[b for b in bars if datetime.fromtimestamp(b['t'],ET).date() in keep or (rd and datetime.fromtimestamp(b['t'],ET).date()>rd[-1])]
        out['bars']=bars;out['has_ext']=any(b['ext'] for b in bars)
    except Exception as e:
        out['bars']=[];out['bars_error']=str(e)[:200]
    try:
        q=chartapi.quote(sym)
        last=_f(q.get('last'));pc=_f(q.get('prev_close'))
        out['last']=last;out['prev_close']=pc;out['ts']=q.get('timestamp')
        out['chg_pct']=(last/pc-1)*100 if last and pc else _f(q.get('change_percentage'))
        out['day_high']=_f(q.get('high'));out['day_low']=_f(q.get('low'))
        ext=None
        for k in('pre_market','post_market','overnight'):
            v=q.get(k)
            if v and v.get('timestamp') and _f(v.get('last')) and (ext is None or v['timestamp']>ext['ts']):
                ext={'k':k,'last':_f(v['last']),'ts':v['timestamp']}
        if ext and last:ext['chg_pct']=(ext['last']/last-1)*100
        out['ext']=ext
    except Exception as e:
        out['quote_error']=str(e)[:200]
    return out
def payload():
    with _lk:
        e=_c.get('p')
        if e and time.time()-e[0]<10:return e[1]
    r={'server_time':int(time.time()),'symbols':[_one(n,s) for n,s in SYMS]}
    with _lk:_c['p']=(time.time(),r)
    return r
