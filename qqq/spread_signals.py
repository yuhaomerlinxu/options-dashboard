# Monitoring-only signal engine (no orders). 15m closed bars, regular session.
import math,time
from datetime import datetime,timedelta
import chartapi as C
ET=C.ET
STEP={'QQQ.US':1,'SPY.US':1,'.SPX.US':5,'NVDA.US':2.5,'TSLA.US':2.5,'AAPL.US':2.5}
WID={'QQQ.US':3,'SPY.US':3,'.SPX.US':25,'NVDA.US':5,'TSLA.US':5,'AAPL.US':5}
P=dict(tol=0.3,cool=8,rsi_lo=40,rsi_hi=65,dip=45,first=1,last_start=(14,45),sig_mult=1.0,trend_need=2)
def ema(v,n):
    k=2/(n+1);o=[];e=None
    for x in v:e=x if e is None else x*k+e*(1-k);o.append(e)
    return o
def rsi(c,n=14):
    o=[None]*len(c);g=l=0
    for i in range(1,len(c)):
        d=c[i]-c[i-1];u,dn=max(d,0),max(-d,0)
        if i<=n:g+=u/n;l+=dn/n
        else:g=(g*(n-1)+u)/n;l=(l*(n-1)+dn)/n
        if i>=n:o[i]=100 if l==0 else 100-100/(1+g/l)
    return o
def stoch(r,n=14,k=3):
    raw=[None]*len(r)
    for i in range(len(r)):
        w=[x for x in r[max(0,i-n+1):i+1] if x is not None]
        if len(w)==n:lo,hi=min(w),max(w);raw[i]=50 if hi==lo else (r[i]-lo)/(hi-lo)*100
    K=[None]*len(r)
    for i in range(len(r)):
        w=[x for x in raw[max(0,i-k+1):i+1] if x is not None]
        if len(w)==k:K[i]=sum(w)/k
    return K
def rnd(x,st,down):return (math.floor if down else math.ceil)(x/st-1e-9)*st
def compute(sym,live_levels=None,closed_only=True,bars=None,P=P):
    s=C.norm(sym)
    if bars is None:bars=[b for b in C.kline(s,'15m') if not b['ext']]
    now=time.time()
    if closed_only:bars=[b for b in bars if b['t']+900<=now]
    if len(bars)<60:return []
    c=[b['c'] for b in bars];e20,e50=ema(c,20),ema(c,50);R=rsi(c);K=stoch(R)
    tr=[bars[0]['h']-bars[0]['l']]+[max(b['h'],a['c'])-min(b['l'],a['c']) for a,b in zip(bars,bars[1:])]
    atr=ema(tr,14)
    day=[datetime.fromtimestamp(b['t'],ET).date() for b in bars]
    days=sorted(set(day));dh={d:max(b['h'] for b,x in zip(bars,day) if x==d) for d in days};dl={d:min(b['l'] for b,x in zip(bars,day) if x==d) for d in days}
    dc={d:[b['c'] for b,x in zip(bars,day) if x==d][-1] for d in days}
    # daily range proxy for 1-sigma (avg of prior days' range*0.8), used historically
    sig=[];last={'B':-99,'S':-99};pv=vv=0;cur=None;vw=[]
    for i,b in enumerate(bars):
        if day[i]!=cur:cur=day[i];pv=vv=0;di=0
        tp=(b['h']+b['l']+b['c'])/3;pv+=tp*b['v'];vv+=b['v'];vw.append(pv/vv if vv else b['c'])
    out=[]
    for i in range(55,len(bars)):
        b=bars[i];d=day[i];t=datetime.fromtimestamp(b['t'],ET)
        if (t.hour,t.minute)<(9,30+15*P['first']) or (t.hour,t.minute)>P['last_start']:continue
        di=days.index(d)
        if di==0:continue
        pd=days[di-1];a=atr[i-1];prev=bars[i-1]
        prior=[x for x in days[:di]][-5:];rng=sum(dh[x]-dl[x] for x in prior)/len(prior)
        ref=dc[pd];mv=rng*0.5  # ~1σ daily move proxy
        lv=live_levels if (live_levels and d==days[-1]) else {}
        sup={'VWAP':vw[i-1],'前日低':dl[pd],'-1σ':lv.get('iv_m1',ref-mv)}
        res={'VWAP':vw[i-1],'前日高':dh[pd],'+1σ':lv.get('iv_p1',ref+mv)}
        if lv.get('put_wall'):sup['Put墙']=lv['put_wall']
        if lv.get('call_wall'):res['Call墙']=lv['call_wall']
        gf=lv.get('gamma_flip')
        if gf:sup['伽马场']=gf;res['伽马场']=gf
        rec=bars[i-3:i+1]
        # Bull Put
        trend_b=[b['c']>vw[i],e20[i]>e50[i]]+([b['c']>gf] if gf else [])
        if all(trend_b) and i-last['B']>=P['cool']:
            lo=min(x['l'] for x in rec)
            held=[k for k,v in sup.items() if lo<=v+P['tol']*a and min(x['c'] for x in rec)>=v-0.1*a and b['c']>v]
            mom=(R[i] and P['rsi_lo']<=R[i]<=P['rsi_hi'] and R[i]>R[i-1] and min(R[i-4:i])<P['dip']) or (K[i] and K[i-1] is not None and K[i-1]<20<=K[i])
            if held and b['c']>prev['h'] and mom:
                lvl=max(sup[k] for k in held);rb=sum(1 for x in bars[i+1:] if day[bars.index(x)]==d) if False else None
                out.append(mk('B',s,b,i,held,lvl,a,R[i],K[i],t,bars,day,d,dc,days,sup))
                last['B']=i
        trend_s=[b['c']<vw[i],e20[i]<e50[i]]+([b['c']<gf] if gf else [])
        if all(trend_s) and i-last['S']>=P['cool']:
            hi=max(x['h'] for x in rec)
            held=[k for k,v in res.items() if hi>=v-P['tol']*a and max(x['c'] for x in rec)<=v+0.1*a and b['c']<v]
            mom=(R[i] and 35<=R[i]<=60 and R[i]<R[i-1] and max(R[i-4:i])>55) or (K[i] and K[i-1] is not None and K[i-1]>80>=K[i])
            if held and b['c']<prev['l'] and mom:
                lvl=min(res[k] for k in held)
                out.append(mk('S',s,b,i,held,lvl,a,R[i],K[i],t,bars,day,d,dc,days,res))
                last['S']=i
    return out
def mk(side,s,b,i,held,lvl,a,r,k,t,bars,day,d,dc,days,lv):
    st=STEP.get(s,2.5 if b['c']>100 else 1);w=WID.get(s,5 if b['c']>100 else 2)
    left=max(1,sum(1 for j in range(i+1,len(bars)) if day[j]==d)) if d!=days[-1] else max(1,int((16*60-(t.hour*60+t.minute+15))/15))
    sr=a*math.sqrt(left)  # remaining-session 1σ proxy (~delta 0.16)
    if side=='B':
        key=lv.get('Put墙',lv.get('-1σ'));tgt=min(b['c']-sr,max(key,b['c']-2*sr)) if key else b['c']-sr
        short=rnd(tgt,st,True);long_=short-w
    else:
        key=lv.get('Call墙',lv.get('+1σ'));tgt=max(b['c']+sr,min(key,b['c']+2*sr)) if key else b['c']+sr
        short=rnd(tgt,st,False);long_=short+w
    aft=[bars[j] for j in range(i+1,len(bars)) if day[j]==d]
    cush=None
    if aft:cush=round(((min(x['l'] for x in aft)-short) if side=='B' else (short-max(x['h'] for x in aft)))/a,1)
    closed=d!=days[-1] or datetime.now(ET).hour>=16
    win=None
    if closed and d in dc:win=(dc[d]>short) if side=='B' else (dc[d]<short)
    return {'side':side,'t':b['t'],'time_et':t.strftime('%m-%d %H:%M'),'price':round(b['c'],2),'levels_held':held,'key':round(lvl,2),'rsi':round(r or 0,1),'stoch':round(k or 0,1),'atr':round(a,3),
            'short':short,'long':long_,'day_close':round(dc[d],2) if closed else None,'win':win,'cushion_atr':cush,'label':('Bull Put %g/%g' if side=='B' else 'Bear Call %g/%g')%(short,long_)}
def ncdf(x):return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(S,K,iv,T,cp):
    if iv<=0 or T<=0:return (max(0,S-K) if cp=='C' else max(0,K-S)),(1.0 if (cp=='C' and S>K) else 0.0)
    d1=(math.log(S/K)+0.5*iv*iv*T)/(iv*math.sqrt(T));d2=d1-iv*math.sqrt(T)
    if cp=='C':return S*ncdf(d1)-K*ncdf(d2),ncdf(d1)
    return K*ncdf(-d2)-S*ncdf(-d1),ncdf(d1)-1
def live_plan(sym,side):
    """Strategy using live option quotes (nearest expiry); SPX -> SPY scaled."""
    s=C.norm(sym);proxy=s=='.SPX.US';u='SPY.US' if proxy else s
    q=C.quote(u);spot=float(q['last']);o=C.options(u,spot)
    if not o:return {'error':'无期权数据'}
    lv=C.levels(u,q);e=o['exps'][0];now=datetime.now(ET)
    T=max((datetime.fromisoformat(e+'T16:00:00').replace(tzinfo=ET)-now).total_seconds()/(365*86400),1/(365*24*2))
    cp='P' if side=='B' else 'C';Q=sorted([x for x in o['q'] if x['e']==e and x['cp']==cp and x['iv']>0],key=lambda x:x['k'])
    for x in Q:x['th'],x['delta']=bs(spot,x['k'],x['iv'],T,cp)
    key=lv.get('put_wall' if side=='B' else 'call_wall');s1=lv.get('iv_m1' if side=='B' else 'iv_p1')
    near=max([v for v in(key,s1) if v]) if side=='B' else min([v for v in(key,s1) if v])
    cand=[x for x in Q if (x['k']<=near if side=='B' else x['k']>=near) and 0.08<=abs(x['delta'])<=0.22]
    if not cand:cand=[x for x in Q if 0.08<=abs(x['delta'])<=0.22]
    if not cand:return {'error':'无合适 delta 0.10-0.20 行权价'}
    sh=min(cand,key=lambda x:abs(abs(x['delta'])-0.15))
    w=WID.get(u,5);lk=sh['k']-w if side=='B' else sh['k']+w
    lg=min(Q,key=lambda x:abs(x['k']-lk))
    mid=lambda x:x['last'] if x['last']>0 else x['th']
    cr=round(mid(sh)-mid(lg),2);W=abs(sh['k']-lg['k'])
    r=1/10.0287 if False else None
    out={'underlying':u,'proxy':proxy,'expiry':e,'short':sh['k'],'long':lg['k'],'short_delta':round(sh['delta'],3),'short_iv':sh['iv'],'credit':cr,'theo_credit':round(sh['th']-lg['th'],2),
         'max_loss':round((W-cr)*100,0),'max_profit':round(cr*100,0),'breakeven':round(sh['k']-cr if side=='B' else sh['k']+cr,2),'limit':cr,'stop_spread':round(2*cr,2),
         'stop_underlying':round(near,2),'spot':spot,'note':'报价为最新成交价(CLI无买卖盘)，限价请以券商实时 mid 为准'}
    if proxy:
        ra=float(C.quote('.SPX.US')['last'])/spot
        out['spx_equiv']='≈SPX %g/%g (SPY×%.3f)，SPX期权CLI不可用，建议用SPY或XSP交易'%(round(sh['k']*ra/5)*5,round(lg['k']*ra/5)*5,ra)
    return out
_sc={}
def api(sym):
    s=C.norm(sym)
    if not s:return {'error':'无效代码'}
    e=_sc.get(s)
    if e and time.time()-e[0]<60:return e[1]
    lv=C._c.get(('L',s));lv=lv[1] if lv and lv[1].get('has_options') else None
    sig=compute(s,lv)
    today=datetime.now(ET).date()
    res={'symbol':s,'signals':sig,'params':P}
    if sig and datetime.fromtimestamp(sig[-1]['t'],ET).date()==today:
        try:res['plan']=live_plan(s,sig[-1]['side']);res['plan_for']=sig[-1]['time_et']
        except Exception as ex:res['plan']={'error':str(ex)}
    _sc[s]=(time.time(),res);return res
