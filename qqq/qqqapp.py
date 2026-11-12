"""QQQ real-time (/qqq), multi-symbol K-line + key levels (/qqq/chart), options radar (/qqq/radar). Market data only via ../lbsafe."""
import os, sys, json, threading, subprocess, time
D = os.path.dirname(os.path.abspath(__file__)); sys.path.insert(0, D)
os.environ.setdefault("LBSAFE", os.path.join(os.path.dirname(D), "lbsafe"))
import chartapi,spread_engine
from urllib.parse import urlparse,parse_qs
import json,subprocess,time,threading,os
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from datetime import datetime,timezone,timedelta
TOKEN=''
ALERTS=os.path.join(D,'alerts.jsonl')
BJ=timezone(timedelta(hours=8))
lock=threading.Lock();cache={'t':0,'d':None,'err':None}
def bj(ts):
    try:return datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(BJ).strftime('%Y-%m-%d %H:%M:%S')
    except Exception:return ts
def quote():
    with lock:
        if time.time()-cache['t']<1:return cache['d'],cache['err']
        try:
            r=subprocess.run([os.environ['LBSAFE'],'quote','QQQ.US','--format','json'],capture_output=True,text=True,timeout=10)
            d=json.loads(r.stdout)[0]
            for k in('pre_market','post_market','overnight'):
                if d.get(k) and d[k].get('timestamp'):d[k]['time_bj']=bj(d[k]['timestamp'])
            best=None
            for k in('pre_market','post_market','overnight'):
                x=d.get(k)
                if x and x.get('timestamp') and (best is None or x['timestamp']>best[1]['timestamp']):best=(k,x)
            d['session_label']='常规时段'
            _n=datetime.now(__import__('zoneinfo').ZoneInfo('America/New_York'));_reg=_n.weekday()<5 and (9*60+30)<=_n.hour*60+_n.minute<16*60
            if best and not _reg:
                nm={'pre_market':'盘前','post_market':'盘后','overnight':'夜盘'}[best[0]]
                x=best[1];lp=float(x['last']);reg_last=d['last']
                # Longbridge keeps top-level last=last regular close and prev_close=prior day
                # until the next regular session. Extended-hours change must use last regular
                # close (session prev_close or d['last']), not d['prev_close'] (one day too old).
                try:pc=float(x['prev_close']) if x.get('prev_close') not in (None,'') else float(reg_last)
                except (TypeError,ValueError):pc=float(reg_last)
                d['session_label']=nm+' 实时';d['last']=x['last']
                d['prev_close']=x.get('prev_close') or reg_last
                if x.get('high') is not None:d['high']=x['high']
                if x.get('low') is not None:d['low']=x['low']
                d['change_value']='%+.3f'%(lp-pc)
                if pc:d['change_percentage']='%+.2f'%((lp/pc-1)*100)
            cache['d'],cache['err']=d,None
        except Exception as e:cache['err']=str(e)
        cache['t']=time.time();return cache['d'],cache['err']
def alerts(n=50):
    out=[]
    if os.path.exists(ALERTS):
        with open(ALERTS,errors='ignore') as f:lines=f.readlines()[-n:]
        for l in lines:
            try:out.append(json.loads(l))
            except Exception:pass
    return out[::-1]
kc={'t':0,'d':None};rp={'k':None,'v':[]}
import sys;pass
def replay(raw,day):
    key=(raw[-1]['time'],len(raw))
    if rp['k']==key:return rp['v']
    try:
        from signals import parse_bars,resample,Confirmer
        from zoneinfo import ZoneInfo
        ETz=ZoneInfo('America/New_York');cfg=json.load(open(os.path.join(D,'config.json')))
        ba=parse_bars(raw);warm=[b for b in ba if b['t'].astimezone(ETz).date()<day];bars=[b for b in ba if b['t'].astimezone(ETz).date()==day]
        c=Confirmer(cfg);out=[]
        for k in range(1,len(bars)+1):
            seen=warm+bars[:k];now=seen[-1]['t']+timedelta(minutes=1)
            for tf in cfg['timeframes']:
                rb=resample(seen,tf)
                if rb and rb[-1]['t']+timedelta(minutes=tf)==now:c.update(tf,rb)
            r=c.check(now)
            if r:out.append({'direction':r[0],'trigger_time_bj':now.astimezone(BJ).strftime('%Y-%m-%d %H:%M:%S'),'price':round(seen[-1]['c'],3),'replay':True,'timeframes':{k:v.get('trigger') if isinstance(v,dict) else v for k,v in r[1].items()}})
        rp['k'],rp['v']=key,out
    except Exception as e:rp['v']=[{'error':str(e)}];rp['k']=key
    return rp['v']
def ema(v,n):
    k=2/(n+1);o=[];e=None
    for x in v:e=x if e is None else x*k+e*(1-k);o.append(round(e,4))
    return o
OLD={'t':0,'b':[]}
DCL={'t':0,'rows':[]}

def _bar_date(s, ET):
    s=str(s)
    if len(s)>=10 and s[4]=='-' and 'T' not in s[:11] and ' ' not in s[:11]:
        return __import__('datetime').date.fromisoformat(s[:10])
    return datetime.fromisoformat(s.replace('Z','+00:00')).astimezone(ET).date()

def load_daily(ET):
    if time.time()-DCL['t']<=600 and DCL.get('rows'):
        return DCL['rows']
    try:
        rd=subprocess.run([os.environ['LBSAFE'],'kline','QQQ.US','--period','day','--count','20','--format','json'],capture_output=True,text=True,timeout=15)
        rows=[]
        for x in json.loads(rd.stdout):
            rows.append({'d':_bar_date(x['time'],ET),'o':float(x['open']),'h':float(x['high']),'l':float(x['low']),'c':float(x['close'])})
        DCL['rows']=rows;DCL['t']=time.time()
    except Exception as e:
        print('daily kline failed:',e,flush=True)
    return DCL.get('rows') or []

def key_levels(bars, daily, now_et, last_rth):
    """Prem / Prev / Open 5min / Prev Week levels for the anchor session (ET)."""
    ET=now_et.tzinfo
    hm=now_et.hour*60+now_et.minute
    if now_et.weekday()<5 and hm>=4*60:
        anchor=now_et.date()
    else:
        anchor=last_rth
    if not anchor:
        return {'anchor_date':None}
    def hm_of(b):
        dt=datetime.fromtimestamp(b['ts'],ET);return dt.date(),dt.hour*60+dt.minute
    def rth_days():
        days=sorted({r['d'] for r in daily})
        for b in bars:
            d,m=hm_of(b)
            if 9*60+30<=m<16*60:days.append(d)
        return sorted(set(days))
    days=rth_days()
    prev=None
    for d in days:
        if d<anchor:prev=d
    out={'anchor_date':str(anchor)}
    prem=[b for b in bars if hm_of(b)[0]==anchor and 4*60<=hm_of(b)[1]<9*60+30]
    if prem:
        out['prem_high']=max(float(b['high']) for b in prem)
        out['prem_low']=min(float(b['low']) for b in prem)
    if prev is not None:
        row=next((r for r in daily if r['d']==prev),None)
        if row:
            out['prev_high']=row['h'];out['prev_low']=row['l']
        else:
            pb=[b for b in bars if hm_of(b)[0]==prev and 9*60+30<=hm_of(b)[1]<16*60]
            if pb:
                out['prev_high']=max(float(b['high']) for b in pb)
                out['prev_low']=min(float(b['low']) for b in pb)
    o5=[b for b in bars if hm_of(b)[0]==anchor and 9*60+30<=hm_of(b)[1]<=9*60+34]
    have34=any(hm_of(b)[1]==9*60+34 for b in o5)
    closed=anchor<now_et.date() or hm>=9*60+35 or (last_rth and last_rth>anchor)
    if o5 and (have34 or closed):
        out['open5_high']=max(float(b['high']) for b in o5)
        out['open5_low']=min(float(b['low']) for b in o5)
    mon=anchor-__import__('datetime').timedelta(days=anchor.weekday())
    w0,w1=mon-__import__('datetime').timedelta(days=7),mon-__import__('datetime').timedelta(days=3)
    week=[r for r in daily if w0<=r['d']<=w1]
    if week:
        out['prev_week_high']=max(r['h'] for r in week)
        out['prev_week_low']=min(r['l'] for r in week)
        out['prev_week_range']=[str(w0),str(w1)]
    return out

def vwap_1m(bars, ET):
    pv=vv=0;out=[]
    for b in bars:
        dt=datetime.fromtimestamp(b['ts'],ET)
        hm=dt.hour*60+dt.minute
        typ=(float(b['high'])+float(b['low'])+float(b['close']))/3*float(b['volume'])
        if dt.hour==9 and dt.minute==30:
            pv=vv=0
        if 9*60+30<=hm<16*60:
            pv+=typ;vv+=float(b['volume'])
            out.append(round(pv/vv,4) if vv else None)
        elif hm>=16*60 and vv:
            out.append(round(pv/vv,4))
        else:
            out.append(None)
    return out

def kline():
    with lock:
        if time.time()-kc['t']<3 and kc['d']:return kc['d']
    try:
        from zoneinfo import ZoneInfo;ET=ZoneInfo('America/New_York')
        def _lb(sess):
            try:
                r=subprocess.run([os.environ['LBSAFE'],'kline','QQQ.US','--period','1m','--count','1000','--session',sess,'--format','json'],capture_output=True,text=True,timeout=25)
                v=json.loads(r.stdout);return v if isinstance(v,list) else []
            except Exception as e:print('kline',sess,'failed:',e,flush=True);return []
        raw=_lb('intraday');bars=_lb('all')
        if not raw:raw=list(bars)  # fallback: derive regular session from all-session bars
        if not raw:raise RuntimeError('kline returned no bars (intraday and all)')
        for b in raw:b['ts']=int(datetime.fromisoformat(b['time'].replace('Z','+00:00')).timestamp())
        day=datetime.fromtimestamp(raw[-1]['ts'],ET).date()
        sess=[b for b in raw if datetime.fromtimestamp(b['ts'],ET).date()==day]
        now_et=datetime.now(ET)
        try:
            if time.time()-OLD['t']>120 or not OLD['b']:
                sd=str(day-__import__('datetime').timedelta(days=2))
                ed=str(max(day,now_et.date())+__import__('datetime').timedelta(days=1))
                r3=subprocess.run([os.environ['LBSAFE'],'kline','history','QQQ.US','--period','1m','--start',sd,'--end',ed,'--session','all','--format','json'],capture_output=True,text=True,timeout=40)
                v=json.loads(r3.stdout) if r3.stdout else []
                if not isinstance(v,list):
                    print('kline history unexpected:', (r3.stderr or '')[:300], flush=True);v=[]
                OLD['b']=v;OLD['t']=time.time()
            m={x['time']:x for x in OLD['b']};m.update({x['time']:x for x in bars});bars=[m[k] for k in sorted(m)]
        except Exception as e:
            print('kline history failed:',repr(e),flush=True)
        if not bars:bars=sess
        for b in bars:b['ts']=int(datetime.fromisoformat(b['time'].replace('Z','+00:00')).timestamp())
        bars=[b for b in bars if not(float(b['volume'])==0 and float(b['high'])==float(b['low']))]
        last_rth=None
        for b in bars:
            dt=datetime.fromtimestamp(b['ts'],ET);hm=dt.hour*60+dt.minute
            if 9*60+30<=hm<16*60:last_rth=dt.date()
        if last_rth is None:last_rth=day
        daily=load_daily(ET)
        lv=key_levels(bars,daily,now_et,last_rth)
        anchor=__import__('datetime').date.fromisoformat(lv['anchor_date']) if lv.get('anchor_date') else last_rth
        sess=[b for b in bars if datetime.fromtimestamp(b['ts'],ET).date()==anchor]
        if not sess:sess=bars[-1:]
        vw1=vwap_1m(bars,ET)
        bars1=[{'t':b['ts'],'o':float(b['open']),'h':float(b['high']),'l':float(b['low']),'c':float(b['close']),'v':float(b['volume']),'vwap':vw1[i]} for i,b in enumerate(bars)]
        d={'symbol':'QQQ.US','session_date_et':str(anchor),'bars':bars1,'error':None,'replay_signals':[],
           'day_high':max(float(b['high']) for b in sess),'day_low':min(float(b['low']) for b in sess),
           'levels':lv}
        d['mtf_signals']=[]  # old 2/3/4/5m MTF rule retired 2026-10-28 per user; only the new rule is shown
        try:
            import importlib.util as U;sp=U.spec_from_file_location('v3live',os.path.join(D,'v3','live.py'));V=U.module_from_spec(sp);sp.loader.exec_module(V)
            d['mtf_signals']=V.compute([{'t':b['t'],'o':b['o'],'h':b['h'],'l':b['l'],'c':b['c'],'v':b['v']} for b in bars1])
        except Exception as e:print('v3 signals failed:',repr(e),flush=True)
        # chart shows 3-minute bars (signals above stay on 1m); EMA/VWAP on 3m (VWAP from 1m)
        b2=[]
        for b in bars1:
            t0=b['t']-b['t']%180
            if b2 and b2[-1]['t']==t0:
                x=b2[-1];x['h']=max(x['h'],b['h']);x['l']=min(x['l'],b['l']);x['c']=b['c'];x['v']+=b['v']
                if b.get('vwap') is not None:x['vwap']=b['vwap']
            else:
                b2.append({'t':t0,'o':b['o'],'h':b['h'],'l':b['l'],'c':b['c'],'v':b['v'],'vwap':b.get('vwap')})
        d['bars']=b2;d['period']='3m'
        d['vwap']=[b.get('vwap') for b in b2]
        with lock:kc['d'],kc['t']=d,time.time()
        return d
    except Exception as e:
        print('kline error:',repr(e),flush=True)
        return kc['d'] or {'error':str(e),'bars':[]}
LWC=open(os.path.join(D,'static','lwc.js'),'rb').read()
PAGE=open(os.path.join(D,'index.html'),'rb').read()
CPAGE=open(os.path.join(D,'chart.html'),'rb').read()
class Mixin:
    def send(self,code,body,ct):
        self.send_response(code);self.send_header('Content-Type',ct);self.send_header('Cache-Control','no-store, no-cache, must-revalidate, max-age=0');self.send_header('Pragma','no-cache');self.send_header('Expires','0');self.end_headers();self.wfile.write(body)
    def qqq_get(self):
        try:self._get()
        except (BrokenPipeError,ConnectionResetError):pass
        except Exception as e:
            try:self.send(500,json.dumps({'error':'server: %s'%e}).encode(),'application/json')
            except Exception:pass
    def _get(self):
        p=self.path.split('?')[0].rstrip('/')
        if p=='/qqq':self.send(200,PAGE,'text/html; charset=utf-8')
        elif p=='/qqq'+'/api':
            d,e=quote()
            self.send(200,json.dumps({'quote':d,'quote_error':e,'server_time_bj':datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S'),'alerts':alerts()},ensure_ascii=False).encode(),'application/json; charset=utf-8')
        elif p=='/qqq'+'/kline':self.send(200,json.dumps(kline()).encode(),'application/json')
        elif p=='/qqq'+'/chart':self.send(200,CPAGE,'text/html; charset=utf-8')
        elif p in('/qqq'+'/chartdata','/qqq'+'/last','/qqq'+'/candles','/qqq'+'/levels','/qqq'+'/signals','/qqq'+'/spreads'):
            qs=parse_qs(urlparse(self.path).query);sy=qs.get('s',['QQQ'])[0];pe=qs.get('p',['5m'])[0]
            try:r={'chartdata':lambda:chartapi.chart(sy,pe),'last':lambda:chartapi.last(sy),'candles':lambda:chartapi.candles(sy,pe),'levels':lambda:chartapi.levels_async(sy),'signals':lambda:spread_engine.signals_api(sy),'spreads':lambda:spread_engine.spreads_async(sy)}[p.rsplit('/',1)[1]]()
            except Exception as e:r={'error':str(e)}
            self.send(200,json.dumps(r,ensure_ascii=False).encode(),'application/json; charset=utf-8')
        elif p=='/qqq'+'/vol':
            sy=parse_qs(urlparse(self.path).query).get('s',['QQQ'])[0]
            try:r=vollive.api(sy)
            except Exception as e:r={'error':str(e)}
            self.send(200,json.dumps(r,ensure_ascii=False).encode(),'application/json; charset=utf-8')
        elif p.startswith('/qqq'+'/radar'):radar.handle(self,p,'')
        elif p=='/qqq'+'/static/lwc.js':self.send(200,LWC,'application/javascript')
        else:self.send(404,b'not found','text/plain')
import radar,vol,vollive
def _volwarm():
    while True:
        for x in ('QQQ','MU','SNDK'):
            try:vol.api(x)
            except Exception:pass
        time.sleep(240)
def start():
    vollive.start()
    radar.start()  # radar scheduler thread (was radar_run.sh)
    threading.Thread(target=chartapi.prewarm,daemon=True).start()
    threading.Thread(target=spread_engine.prewarm,daemon=True).start()
    def mon():  # QQQ 1/2/3m signal monitor (was qqq_rt/run.sh watchdog)
        while True:
            subprocess.run([sys.executable,'-u',os.path.join(D,'monitor.py')],cwd=D)
            time.sleep(10)
    threading.Thread(target=mon,daemon=True).start()
