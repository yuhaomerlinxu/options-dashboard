# Real-time (1-min) ATM IV sampler from bid/ask mid. Market data only (quote / option chain / depth).
import math,time,threading,json,os
from datetime import datetime,date,timedelta
from concurrent.futures import ThreadPoolExecutor
import chartapi as C, vol as V
ET=C.ET;D=os.path.join(os.path.dirname(os.path.abspath(__file__)),'vol_data');os.makedirs(D,exist_ok=True)
SYMS=['QQQ.US','MU.US','SNDK.US'];R=0.04
_lk=threading.Lock();SER={};LAST={};REQ={};POOL=ThreadPoolExecutor(8);SP=ThreadPoolExecutor(4)
def is_open(n=None):
    n=n or datetime.now(ET)
    return n.weekday()<5 and (9,30)<=(n.hour,n.minute)<(16,0)
def bs(S,K,T,v,cp):
    if v<=0 or T<=0:return max(0,(S-K) if cp=='C' else (K-S))
    d1=(math.log(S/K)+(R+v*v/2)*T)/(v*math.sqrt(T));d2=d1-v*math.sqrt(T)
    return S*C.ncdf(d1)-K*math.exp(-R*T)*C.ncdf(d2) if cp=='C' else K*math.exp(-R*T)*C.ncdf(-d2)-S*C.ncdf(-d1)
def impv(p,S,K,T,cp):
    if not p or T<=0 or p<=max(0,(S-K) if cp=='C' else (K-S))*0.999:return None
    lo,hi=0.005,6.0
    for _ in range(60):
        m=(lo+hi)/2
        if bs(S,K,T,m,cp)>p:hi=m
        else:lo=m
    return (lo+hi)/2
def T_of(e):
    x=datetime.combine(date.fromisoformat(e),datetime.min.time()).replace(hour=16,tzinfo=ET)
    return max((x-datetime.now(ET)).total_seconds(),60)/(365*86400)
def strikes(s,e):return C.cached(('vks',s,e),1800,lambda:sorted({float(r['strike']) for r in C.lb(['option','chain',s,'--date',e],20) if r.get('standard','true')=='true'}))
def exps(s):
    def f():
        n=datetime.now(ET);td=n.date().isoformat()
        ex=[x['expiry_date'] for x in C.lb(['option','chain',s],15)]
        ex=[e for e in ex if e>td or (e==td and n.hour<16)]
        return {'near':ex[0],'d30':min(ex,key=lambda e:abs(V.dte(e)-30))}
    return C.cached(('vex',s,datetime.now(ET).date()),900,f)
def mid(sym):
    try:
        d=C.lb(['depth',sym],12);b=float(d['bids'][0]['price']) if d.get('bids') else 0;a=float(d['asks'][0]['price']) if d.get('asks') else 0
        if b>0 and a>0 and a>=b:return (a+b)/2,b,a
    except Exception:pass
    return None,None,None
def leg(s,e,spot):
    k=min(strikes(s,e),key=lambda k:abs(k-spot));T=T_of(e)
    fc=POOL.submit(mid,C.occ(s,e,'C',k));fp=POOL.submit(mid,C.occ(s,e,'P',k))
    (mc,bc,ac),(mp,bp,ap)=fc.result(),fp.result()
    ivs=[x for x in (impv(mc,spot,k,T,'C'),impv(mp,spot,k,T,'P')) if x]
    src='mid'
    if not ivs:  # fallback: Longbridge IV (from last trade)
        try:ivs=[float(x['implied_volatility']) for x in C.lb(['option','quote',C.occ(s,e,'C',k),C.occ(s,e,'P',k)],15) if float(x.get('implied_volatility') or 0)>0];src='last'
        except Exception:pass
    return {'exp':e,'dte':V.dte(e),'k':k,'iv':round(sum(ivs)/len(ivs),4) if ivs else None,'strad':round(mc+mp,2) if mc and mp else None,'src':src,
            'spr':round(((ac-bc)+(ap-bp))/(mc+mp)*100,1) if mc and mp else None}
def vix():
    def f():
        q=C.lb(['quote','.VIX.US'],10)[0];return {'last':float(q['last']),'open':float(q['open'] or 0) or None,'prev':float(q['prev_close'])}
    return C.cached('vixrt',20,f)
def vxn():
    try:return V.cboe_now('VXN')
    except Exception:return None
def fpath(s,d):return os.path.join(D,'%s_%s.json'%(s.replace('.','_'),d))
def load_disk(s):
    fs=sorted(f for f in os.listdir(D) if f.startswith(s.replace('.','_')+'_'))
    if not fs:return []
    try:return json.load(open(os.path.join(D,fs[-1])))
    except Exception:return []
def sample(s):
    q=C.lb(['quote',s],10)[0];spot=float(q['last']);ex=exps(s)
    ln=POOL.submit(leg,s,ex['near'],spot);l3=POOL.submit(leg,s,ex['d30'],spot) if ex['d30']!=ex['near'] else None
    n=ln.result();t=l3.result() if l3 else n
    vx=None
    try:vx=vix()
    except Exception:pass
    vn=vxn() if s=='QQQ.US' else None
    n_=datetime.now(ET);pt={'t':int(time.time()),'px':spot,'ivn':n['iv'],'iv30':t['iv'],'strad':n['strad'],'vix':vx and vx['last'],'vxn':vn and vn.get('last')}
    with _lk:
        ser=SER.setdefault(s,[]);d=n_.date().isoformat()
        if ser and datetime.fromtimestamp(ser[-1]['t'],ET).date().isoformat()!=d and is_open(n_):ser.clear()
        if not ser or pt['t']-ser[-1]['t']>=50:ser.append(pt)
        else:ser[-1]=pt
        LAST[s]={'near':n,'d30':t,'spot':spot,'open':float(q.get('open') or 0) or None,'prev':float(q['prev_close']),'vix':vx,'vxn':vn,'t':pt['t']}
        snap=list(ser)
    try:json.dump(snap,open(fpath(s,d),'w'))
    except Exception:pass
    if t['iv'] and is_open(n_):
        try:
            fp=os.path.join(D,'ivd_%s.json'%s.replace('.','_'));h=json.load(open(fp)) if os.path.exists(fp) else {}
            h[d]=t['iv'];json.dump(h,open(fp,'w'))
        except Exception:pass
def loop():
    for s in SYMS:
        with _lk:SER[s]=load_disk(s)
    first=True
    while True:
        t0=time.time();act=set(SYMS)|{s for s,ts in list(REQ.items()) if t0-ts<600}
        if is_open() or first or any(s not in LAST for s in act):
            def one(s):
                if s in LAST and not is_open():return
                try:sample(s)
                except Exception as e:LAST.setdefault(s,{})['error']=str(e)[:120]
            list(SP.map(one,act))
        for s in act:SP.submit(V.slow,s)
        first=False
        time.sleep(max(5,60-(time.time()-t0)))
def start():threading.Thread(target=loop,daemon=True).start()
def api(sym):
    s=C.norm(sym)
    if not s:return {'error':'bad symbol'}
    REQ[s]=time.time()
    with _lk:ser=list(SER.get(s,[]));L=dict(LAST.get(s,{}))
    if s not in SYMS and not L:
        try:sample(s)
        except Exception as e:return {'error':str(e)[:120]}
        with _lk:ser=list(SER.get(s,[]));L=dict(LAST.get(s,{}))
    sl=V.slow(s) if hasattr(V,'slow') else {}
    o={'gauge':gauge(s,L,sl),'symbol':s,'open':is_open(),'now':int(time.time()),'last':L,'series':ser,'slow':sl}
    if ser:o['first']=ser[0]
    return o

def pctl(x,arr):return round(sum(1 for a in arr if a<x)/len(arr)*100,1)
def gauge(s,L,sl):
    iv=(L.get('d30') or {}).get('iv')
    try:
        fp=os.path.join(D,'ivd_%s.json'%s.replace('.','_'));h=json.load(open(fp)) if os.path.exists(fp) else {}
    except Exception:h={}
    g={'iv30':iv,'days_recorded':len(h)}
    if iv and len(h)>=120:
        a=list(h.values())[-252:];g.update(score=pctl(iv,a),lo=min(a),hi=max(a),basis='IV30 自身历史百分位（%d个交易日记录）'%len(a),kind='iv')
        return g
    if s=='QQQ.US':
        try:
            vn=V.cboe_now('VXN');hist=[c for _,c in V.cboe_hist('VXN')][-252:];cur=vn['last']
            g.update(score=pctl(cur,hist),lo=min(hist),hi=max(hist),cur=cur,basis='VXN 52周百分位（纳指100隐含波动率指数，Cboe延迟）',kind='vxn')
        except Exception as e:g['error']=str(e)[:80]
        return g
    try:
        a=V.hv_series(s)
        if iv and a:g.update(score=pctl(iv,a),lo=round(min(a),4),hi=round(max(a),4),basis='代理：当前IV30在过去一年滚动HV30分布中的百分位',kind='hv')
    except Exception as e:g['error']=str(e)[:80]
    return g
