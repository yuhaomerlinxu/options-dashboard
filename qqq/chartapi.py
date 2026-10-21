# Market-data only: longbridge quote / kline / option chain|quote. No trade commands.
import os,json,subprocess,time,threading,re,math
from datetime import datetime,timezone,date
from zoneinfo import ZoneInfo
LB=os.environ.get('LBSAFE','/app/lbsafe');ET=ZoneInfo('America/New_York')
SYM=re.compile(r'^[A-Z0-9.]{1,12}$')
_lk=threading.Lock();_c={}
def norm(s):
    s=(s or 'QQQ').strip().upper()
    if not SYM.match(s):return None
    if s in('SPX','.SPX','SPX.US'):s='.SPX.US'
    if not s.endswith('.US'):s=s+'.US'
    return s if re.match(r'^[A-Z0-9.]{1,9}\.US$',s) and '..' not in s else None
def lb(args,to=25):
    r=subprocess.run([LB]+args+['--format','json'],capture_output=True,text=True,timeout=to)
    return json.loads(r.stdout)
def cached(key,ttl,fn):
    with _lk:
        e=_c.get(key)
        if e and time.time()-e[0]<ttl:return e[1]
    v=fn()
    with _lk:_c[key]=(time.time(),v)
    return v
def ts(t):return int(datetime.fromisoformat(t.replace('Z','+00:00')).timestamp())
def quote(s):
    def f():
        d=lb(['quote',s],10)[0];return d
    return cached(('q',s),1,f)
def kline(s,period):
    cnt={'5m':1000,'15m':1000}[period]
    def f():
        bars=lb(['kline',s,'--period',period,'--count',str(cnt),'--session','all'])
        out=[];days=[]
        for b in bars:
            t=ts(b['time']);d=datetime.fromtimestamp(t,ET).date()
            if d not in days:days.append(d)
            out.append({'t':t,'o':float(b['open']),'h':float(b['high']),'l':float(b['low']),'c':float(b['close']),'v':float(b['volume']),'ext':b.get('session','Intraday')!='Intraday'})
        rd=sorted({datetime.fromtimestamp(b['t'],ET).date() for b in out if not b['ext']})
        nd=3 if period=='5m' else 10
        cut=rd[-nd] if len(rd)>=nd else (rd[0] if rd else days[0])
        keep={d for d in days if d>=cut}
        out=[b for b in out if datetime.fromtimestamp(b['t'],ET).date() in keep]
        # drop flat zero-volume overnight filler bars
        out=[b for b in out if not(b['ext'] and b['v']==0 and b['h']==b['l'])]
        return out
    return cached(('k',s,period),1,f)
def ncdf(x):return 0.5*(1+math.erf(x/math.sqrt(2)))
def gamma(S,K,iv,T):
    if iv<=0 or T<=0:return 0.0
    d1=(math.log(S/K)+0.5*iv*iv*T)/(iv*math.sqrt(T))
    return math.exp(-d1*d1/2)/math.sqrt(2*math.pi)/(S*iv*math.sqrt(T))
def occ(s,exp,cp,k):
    tk=s[:-3];y=exp.replace('-','')[2:]
    return '%s%s%s%d.US'%(tk,y,cp,round(k*1000))
def options(s,spot):
    def f():
        try:exps=[e['expiry_date'] for e in lb(['option','chain',s],15)]
        except Exception:return None
        today=datetime.now(ET).date().isoformat()
        exps=[e for e in exps if e>=today][:2]
        if not exps:return None
        cons=[]
        for e in exps:
            ch=lb(['option','chain',s,'--date',e],20)
            ks=[float(r['strike']) for r in ch if r.get('standard','true')=='true']
            ks=[k for k in ks if abs(k/spot-1)<=0.15]
            for k in ks:cons+=[occ(s,e,'C',k),occ(s,e,'P',k)]
        q=[]
        for i in range(0,len(cons),50):
            try:q+=lb(['option','quote']+cons[i:i+50],30)
            except Exception:pass
        if not q:return None
        return {'exps':exps,'q':[{'e':x['expiry_date'],'k':float(x['strike_price']),'cp':x['direction'][0],'iv':float(x.get('implied_volatility') or 0),'last':float(x.get('last') or 0),'oi':int(x.get('open_interest') or 0)} for x in q]}
    return cached(('o',s),300,f)
def levels(s,qt):
    now=datetime.now(ET);last=float(qt['last'])
    reg_open=now.weekday()<5 and (now.hour,now.minute)>=(9,30) and now.hour<16 and qt.get('open') and datetime.fromisoformat(qt.get('timestamp','2000-01-01T00:00:00Z').replace('Z','+00:00')).astimezone(ET).date()==now.date()
    ref=float(qt['open']) if reg_open else last; refname='今日开盘' if reg_open else '前一常规收盘'
    spot=last
    for k in('post_market','overnight','pre_market'):pass
    o=options(s,spot)
    if not o:return {'has_options':False,'ref':ref,'ref_name':refname}
    Q=o['q'];e0=o['exps'][0];L={'has_options':True,'ref':ref,'ref_name':refname,'expiries':o['exps'],'n_contracts':len(Q)}
    # ATM IV nearest expiry
    n0=[x for x in Q if x['e']==e0 and x['iv']>0]
    if n0:
        ka=min({x['k'] for x in n0},key=lambda k:abs(k-spot))
        ivs=[x['iv'] for x in n0 if x['k']==ka]
        iv=sum(ivs)/len(ivs);mv=ref*iv*math.sqrt(1/252)
        L.update(atm_strike=ka,atm_iv=round(iv,4),move=round(mv,2),iv_p1=round(ref+mv,2),iv_m1=round(ref-mv,2),iv_p2=round(ref+2*mv,2),iv_m2=round(ref-2*mv,2))
    agg={}
    for x in Q:
        a=agg.setdefault(x['k'],{'C':0,'P':0});a[x['cp']]+=x['oi']
    up=[(v['C'],k) for k,v in agg.items() if k>spot];dn=[(v['P'],k) for k,v in agg.items() if k<spot]
    if up:L['call_wall'],L['call_wall_oi']=max(up)[1],max(up)[0]
    if dn:L['put_wall'],L['put_wall_oi']=max(dn)[1],max(dn)[0]
    # max pain nearest expiry
    e0q=[x for x in Q if x['e']==e0];ks=sorted({x['k'] for x in e0q})
    if ks and sum(x['oi'] for x in e0q)>0:
        def pain(P):return sum(x['oi']*(max(0,P-x['k']) if x['cp']=='C' else max(0,x['k']-P)) for x in e0q)
        L['max_pain']=min(ks,key=pain);L['pivot_method']='最大痛点(%s)'%e0
    # gamma flip
    def T(e):
        close=datetime.fromisoformat(e+'T16:00:00').replace(tzinfo=ET)
        return max((close-now).total_seconds()/(365*86400),1/(365*24*2))
    tt={e:T(e) for e in o['exps']}
    def gex(S):return sum((1 if x['cp']=='C' else -1)*x['oi']*gamma(S,x['k'],x['iv'],tt[x['e']])*S*S*0.01*100 for x in Q if x['iv']>0 and x['oi']>0)
    grid=[spot*(0.85+0.0025*i) for i in range(121)];g=[gex(S) for S in grid]
    L['gex_spot']=round(gex(spot)/1e6,1)
    fl=[]
    for i in range(1,len(grid)):
        if g[i-1]==0 or g[i-1]*g[i]<0:
            a,b=grid[i-1],grid[i];fl.append(a+(b-a)*(-g[i-1])/((g[i]-g[i-1]) or 1))
    if fl:L['gamma_flip']=round(min(fl,key=lambda x:abs(x-spot)),2)
    return L
def chart(sym,period):
    s=norm(sym)
    if not s:return {'error':'无效代码'}
    if period not in('5m','15m'):period='5m'
    try:qt=quote(s)
    except Exception as e:return {'error':'行情获取失败: %s'%e,'symbol':s}
    bars=kline(s,period)
    d={'symbol':s,'period':period,'bars':bars,'quote':qt}
    if bars:
        lastday=datetime.fromtimestamp(bars[-1]['t'],ET).date()
        rb=[b for b in bars if not b['ext'] and datetime.fromtimestamp(b['t'],ET).date()==max(datetime.fromtimestamp(x['t'],ET).date() for x in bars if not x['ext'])] if any(not b['ext'] for b in bars) else []
        if rb:d['sess_high']=max(b['h'] for b in rb);d['sess_low']=min(b['l'] for b in rb)
    try:d['levels']=levels(s,qt)
    except Exception as e:d['levels']={'has_options':False,'error':str(e)}
    return d
def last(sym):
    s=norm(sym)
    if not s:return {'error':'无效代码'}
    q=quote(s);ext=None
    for k in('pre_market','post_market','overnight'):
        v=q.get(k)
        if v and v.get('timestamp') and (ext is None or v['timestamp']>ext[1]['timestamp']):ext=(k,v)
    return {'symbol':s,'last':q['last'],'chg':q.get('change_percentage'),'ext':{'k':ext[0],'last':ext[1]['last'],'ts':ext[1]['timestamp']} if ext else None,'ts':q.get('timestamp')}

_pend=set()
def candles(sym,period):
    s=norm(sym)
    if not s:return {'error':'无效代码'}
    if period not in('5m','15m'):period='5m'
    bars=kline(s,period);d={'symbol':s,'period':period,'bars':bars}
    rb=[b for b in bars if not b['ext']]
    if rb:
        day=max(datetime.fromtimestamp(b['t'],ET).date() for b in rb)
        rb=[b for b in rb if datetime.fromtimestamp(b['t'],ET).date()==day]
        d['sess_high']=max(b['h'] for b in rb);d['sess_low']=min(b['l'] for b in rb)
    return d
PX={'k','ref','atm_strike','move','iv_p1','iv_m1','iv_p2','iv_m2','call_wall','put_wall','max_pain','gamma_flip'}
def spx_levels():
    q=quote('.SPX.US');y=quote('SPY.US');r=float(q['last'])/float(y['last'])
    v=dict(levels('SPY.US',y))
    for k in PX:
        if isinstance(v.get(k),(int,float)):v[k]=round(v[k]*r,2)
    v['ref_name']='SPY'+v.get('ref_name','')+'换算';v['proxy']='由SPY换算 (SPX/SPY=%.4f)'%r
    if v.get('pivot_method'):v['pivot_method']+=' 由SPY换算'
    return v
def _calc(s):
    try:
        v=spx_levels() if s=='.SPX.US' else levels(s,quote(s))
    except Exception as e:v={'has_options':False,'error':str(e),'retry':True}
    if s=='.SPX.US' and not v.get('has_options'):v['retry']=True
    with _lk:
        # failed/empty results expire after ~20s instead of 5 min
        _c[('L',s)]=(time.time()-(280 if v.get('retry') else 0),v);_pend.discard(s)
def levels_async(sym):
    s=norm(sym)
    if not s:return {'error':'无效代码'}
    with _lk:
        e=_c.get(('L',s))
        fresh=e and time.time()-e[0]<300
        if not fresh and s not in _pend:
            _pend.add(s);threading.Thread(target=_calc,args=(s,),daemon=True).start()
    if e:return e[1]
    return {'pending':True}
def prewarm():
    for s in('SPX','QQQ','SPY','NVDA','TSLA','AAPL','COHR'):
        try:
            for p in('5m','15m'):kline(norm(s),p)
            levels_async(s)
        except Exception:pass
