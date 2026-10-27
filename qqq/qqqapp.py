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
                lp=float(best[1]['last']);pc=float(d['prev_close']) if best[0]=='pre_market' else float(d['last'])
                d['session_label']=nm+' 实时';d['last']=best[1]['last'];d['change_value']='%+.3f'%(lp-pc);d['change_percentage']='%+.2f'%((lp/pc-1)*100)
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
DCL={'t':0,'m':{}}
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
        try:
            if time.time()-OLD['t']>120 or not OLD['b']:
                sd=str(datetime.fromtimestamp(raw[-1]['ts'],ET).date()-__import__('datetime').timedelta(days=1 if datetime.now(ET).weekday()!=0 else 3))
                r3=subprocess.run([os.environ['LBSAFE'],'kline','history','QQQ.US','--period','1m','--start',sd,'--end',str(day),'--session','all','--format','json'],capture_output=True,text=True,timeout=25)
                OLD['b']=json.loads(r3.stdout);OLD['t']=time.time()
            m={x['time']:x for x in OLD['b']};m.update({x['time']:x for x in bars});bars=[m[k] for k in sorted(m)]
        except Exception:pass
        if not bars:bars=sess
        for b in bars:b['ts']=int(datetime.fromisoformat(b['time'].replace('Z','+00:00')).timestamp())
        bars=[b for b in bars if not(float(b['volume'])==0 and float(b['high'])==float(b['low']))]
        c=[float(b['close']) for b in bars];out={}
        for n in(10,20,60,120):out['ema%d'%n]=ema(c,n)
        pv=vv=0;vw=[]
        for b in bars:
            dt=datetime.fromtimestamp(b['ts'],ET)
            if dt.hour==9 and dt.minute==30:pv=vv=0
            h,l,cl,v=float(b['high']),float(b['low']),float(b['close']),float(b['volume'])
            pv+=(h+l+cl)/3*v;vv+=v;vw.append(round(pv/vv,4) if vv else cl)
        d={'symbol':'QQQ.US','session_date_et':str(day),'bars':[{'t':b['ts'],'o':float(b['open']),'h':float(b['high']),'l':float(b['low']),'c':float(b['close']),'v':float(b['volume'])} for b in bars],'vwap':vw,**out,
           'day_high':max(float(b['high']) for b in sess),'day_low':min(float(b['low']) for b in sess),'error':None,'replay_signals':[]}
        try:
            if time.time()-DCL['t']>600:
                rd=subprocess.run([os.environ['LBSAFE'],'kline','QQQ.US','--period','day','--count','10','--format','json'],capture_output=True,text=True,timeout=15)
                DCL['m']={__import__('datetime').date.fromisoformat(x['time'][:10]):float(x['close']) for x in json.loads(rd.stdout)};DCL['t']=time.time()
        except Exception as e:print('daily kline failed:',e,flush=True)
        d['mtf_signals']=__import__('mtf').compute(d['bars'],daily_close=DCL['m'])
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
            try:r=vol.api(sy)
            except Exception as e:r={'error':str(e)}
            self.send(200,json.dumps(r,ensure_ascii=False).encode(),'application/json; charset=utf-8')
        elif p.startswith('/qqq'+'/radar'):radar.handle(self,p,'')
        elif p=='/qqq'+'/static/lwc.js':self.send(200,LWC,'application/javascript')
        else:self.send(404,b'not found','text/plain')
import radar,vol
def _volwarm():
    while True:
        for x in ('QQQ','MU','SNDK'):
            try:vol.api(x)
            except Exception:pass
        time.sleep(240)
def start():
    threading.Thread(target=_volwarm,daemon=True).start()
    radar.start()  # radar scheduler thread (was radar_run.sh)
    threading.Thread(target=chartapi.prewarm,daemon=True).start()
    threading.Thread(target=spread_engine.prewarm,daemon=True).start()
    def mon():  # QQQ 1/2/3m signal monitor (was qqq_rt/run.sh watchdog)
        while True:
            subprocess.run([sys.executable,'-u',os.path.join(D,'monitor.py')],cwd=D)
            time.sleep(10)
    threading.Thread(target=mon,daemon=True).start()
