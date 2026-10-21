# Monitoring-only engine: entry signals (15m, US regular session only) + safe-strike credit spreads.
import math,json,os,time
_D=os.path.dirname(os.path.abspath(__file__))
from datetime import datetime,timezone,timedelta,date
from zoneinfo import ZoneInfo
ET=ZoneInfo('America/New_York');BJ=ZoneInfo('Asia/Shanghai')
def ema(v,n):
    k=2/(n+1);o=[];e=None
    for x in v:e=x if e is None else x*k+e*(1-k);o.append(e)
    return o
def rsi(c,n=14):
    o=[50]*len(c);g=l=0
    for i in range(1,len(c)):
        d=c[i]-c[i-1];u,dn=max(d,0),max(-d,0)
        if i<=n:g+=u/n;l+=dn/n
        else:g=(g*(n-1)+u)/n;l=(l*(n-1)+dn)/n
        if i>=n:o[i]=100 if l==0 else 100-100/(1+g/l)
    return o
def stoch(r,n=14,k=3):
    raw=[]
    for i in range(len(r)):
        w=r[max(0,i-n+1):i+1];lo,hi=min(w),max(w);raw.append(50 if hi==lo else (r[i]-lo)/(hi-lo)*100)
    return [sum(raw[max(0,i-k+1):i+1])/len(raw[max(0,i-k+1):i+1]) for i in range(len(r))]
def parse(bars):
    """bars: list of dict time(ISO Z) o h l c v [session]; returns regular-session only."""
    out=[]
    for b in bars:
        if b.get('session','Intraday')!='Intraday':continue
        t=int(datetime.fromisoformat(b['time'].replace('Z','+00:00')).timestamp());e=datetime.fromtimestamp(t,ET)
        if not((9,30)<=(e.hour,e.minute)<(16,0)):continue
        out.append({'t':t,'o':float(b['open']),'h':float(b['high']),'l':float(b['low']),'c':float(b['close']),'v':float(b['volume']),'d':e.date(),'hm':(e.hour,e.minute)})
    return out
def rv(closes,n=20):
    r=[math.log(b/a) for a,b in zip(closes[-n-1:],closes[-n:])]
    if len(r)<5:return 0.25
    m=sum(r)/len(r);return math.sqrt(sum((x-m)**2 for x in r)/(len(r)-1)*252)
# ---------- strikes ----------
MULT=1.5
MULTS={7:1.75,10:1.75}  # keyed by calendar-day bucket (7天/10天); see bt3.py
TD={7:5,10:7}  # nominal trading days per bucket
STEP={'QQQ.US':1,'SPY.US':1,'.SPX.US':5,'NVDA.US':2.5,'TSLA.US':2.5,'AAPL.US':2.5}
WID={'QQQ.US':3,'SPY.US':3,'.SPX.US':25,'NVDA.US':5,'TSLA.US':5,'AAPL.US':5}
def step(s,p):return STEP.get(s,5 if p>300 else 2.5 if p>50 else 1)
def em(S,iv,dte,frac):return S*iv*math.sqrt((frac+dte)/252)
def safe_short(side,S,iv,dte,frac,key,m=None,st=1):
    m=m or 1.75;e=em(S,iv,dte,frac)
    if side=='B':x=min(S-m*e,key) if key else S-m*e;return math.floor(x/st+1e-9)*st,e
    x=max(S+m*e,key) if key else S+m*e;return math.ceil(x/st-1e-9)*st,e
# ---------- signals ----------
P0=dict(mode='trend',fk=1.0,frsi=65,tol=0.5,mom='rsi',orb=1,vol=0,cool=8,first=(9,45),last=(14,45),slope=1)
def signals(bars,P=P0,live=None,daily=None):
    """bars: parsed regular 15m (closed). live: option levels for latest day. daily: {date:(h,l,c)} prior days."""
    if len(bars)<60:return []
    c=[b['c'] for b in bars];e20,e50=ema(c,20),ema(c,50);R=rsi(c);K=stoch(R)
    tr=[bars[0]['h']-bars[0]['l']]+[max(b['h'],a['c'])-min(b['l'],a['c']) for a,b in zip(bars,bars[1:])];atr=ema(tr,14)
    vavg=ema([b['v'] for b in bars],20)
    days=sorted({b['d'] for b in bars});DH={};DL={};DC={}
    for b in bars:
        DH[b['d']]=max(DH.get(b['d'],b['h']),b['h']);DL[b['d']]=min(DL.get(b['d'],b['l']),b['l']);DC[b['d']]=b['c']
    vw=[];cur=None;orh={};orl={};dlo={};dhi={};lo_sofar=[];hi_sofar=[]
    for b in bars:
        if b['d']!=cur:cur=b['d'];pv=vv=0;lo=b['l'];hi=b['h']
        pv+=(b['h']+b['l']+b['c'])/3*b['v'];vv+=b['v'];vw.append(pv/vv if vv else b['c'])
        lo=min(lo,b['l']);hi=max(hi,b['h']);lo_sofar.append(lo);hi_sofar.append(hi)
        if b['hm']<(10,0):orh[b['d']]=max(orh.get(b['d'],b['h']),b['h']);orl[b['d']]=min(orl.get(b['d'],b['l']),b['l'])
    closes_by_day=[DC[d] for d in days]
    out=[];last={'B':-99,'S':-99}
    for i in range(55,len(bars)):
        b=bars[i];d=b['d'];di=days.index(d)
        if di<1 or b['hm']<P['first'] or b['hm']>P['last']:continue
        pd=days[di-1];a=atr[i-1];prev=bars[i-1];rec=bars[i-3:i+1]
        ivp=rv(closes_by_day[:di]) if di>5 else 0.25
        ref=DC[pd];mv=ref*ivp/math.sqrt(252)
        lv=live if (live and d==days[-1]) else {}
        sup={'VWAP':vw[i-1],'前日低':DL[pd],'-1σ':lv.get('iv_m1',ref-mv),'开盘区间低':orl.get(d)}
        res={'VWAP':vw[i-1],'前日高':DH[pd],'+1σ':lv.get('iv_p1',ref+mv),'开盘区间高':orh.get(d)}
        if lv.get('put_wall'):sup['Put墙']=lv['put_wall']
        if lv.get('call_wall'):res['Call墙']=lv['call_wall']
        gf=lv.get('gamma_flip')
        if gf:sup['伽马场']=gf;res['伽马场']=gf
        sup={k:v for k,v in sup.items() if v};res={k:v for k,v in res.items() if v}
        for side in('B','S'):
            if i-last[side]<P['cool']:continue
            up=side=='B';sg=1 if up else -1
            if P['mode']=='fade':
                lvl=(lv.get('iv_m1',ref-mv) if up else lv.get('iv_p1',ref+mv))
                lvl=ref-(ref-lvl)*P['fk'] if up else ref+(lvl-ref)*P['fk']
                ext=min(x['l'] for x in rec) if up else max(x['h'] for x in rec)
                if not((ext<=lvl) if up else (ext>=lvl)):continue
                if not((min(R[i-4:i])<100-P['frsi']) if up else (max(R[i-4:i])>P['frsi'])):continue
                if not((b['c']>prev['h'] and b['c']>b['o']) if up else (b['c']<prev['l'] and b['c']<b['o'])):continue
                if P['vol'] and not b['v']>vavg[i-1]:continue
                last[side]=i
                key=min(DL[pd],lo_sofar[i]) if up else max(DH[pd],hi_sofar[i])
                out.append({'i':i,'side':side,'t':b['t'],'price':b['c'],'held':['%s%.1fσ反转'%('-' if up else '+',P['fk'])],'atr':a,'rsi':R[i],'k':K[i],'key':key,'rv':ivp,'d':d});continue
            tr_ok=(b['c']-vw[i])*sg>0 and (e20[i]-e50[i])*sg>0 and (not gf or (b['c']-gf)*sg>0)
            if P['slope']:tr_ok=tr_ok and (e20[i]-e20[i-4])*sg>0
            if P['orb']:tr_ok=tr_ok and d in orh and ((b['c']>orh[d]) if up else (b['c']<orl[d]))
            if not tr_ok:continue
            L=sup if up else res
            if up:held=[k for k,v in L.items() if min(x['l'] for x in rec)<=v+P['tol']*a and min(x['c'] for x in rec)>=v-0.1*a and b['c']>v]
            else:held=[k for k,v in L.items() if max(x['h'] for x in rec)>=v-P['tol']*a and max(x['c'] for x in rec)<=v+0.1*a and b['c']<v]
            if not held:continue
            if up and not b['c']>prev['h']:continue
            if not up and not b['c']<prev['l']:continue
            rm=(40<=R[i]<=65 and R[i]>R[i-1] and min(R[i-4:i])<45) if up else (35<=R[i]<=60 and R[i]<R[i-1] and max(R[i-4:i])>55)
            sm=(K[i-1]<20<=K[i]) if up else (K[i-1]>80>=K[i])
            mom={'rsi':rm,'stoch':sm,'either':rm or sm,'both':rm and sm}[P['mom']]
            if not mom:continue
            if P['vol'] and not b['v']>vavg[i-1]:continue
            last[side]=i
            key=min(DL[pd],lo_sofar[i]) if up else max(DH[pd],hi_sofar[i])
            if up and lv.get('put_wall') and lv['put_wall']<b['c']:key=min(key,lv['put_wall'])
            if not up and lv.get('call_wall') and lv['call_wall']>b['c']:key=max(key,lv['call_wall'])
            out.append({'i':i,'side':side,'t':b['t'],'price':b['c'],'held':held,'atr':a,'rsi':R[i],'k':K[i],'key':key,'rv':ivp,'d':d})
    return out
def bj(t):return datetime.fromtimestamp(t,BJ).strftime('%m-%d %H:%M')
# ---------- live ----------
import threading
import chartapi as C
_lk=threading.Lock();_cache={};_pend=set()
def ncdf(x):return 0.5*(1+math.erf(x/math.sqrt(2)))
def bs(S,K,iv,T,cp):
    if iv<=0 or T<=0:return max(0,(S-K) if cp=='C' else (K-S)),0.0
    d1=(math.log(S/K)+0.5*iv*iv*T)/(iv*math.sqrt(T));d2=d1-iv*math.sqrt(T)
    if cp=='C':return S*ncdf(d1)-K*ncdf(d2),ncdf(d1)
    return K*ncdf(-d2)-S*ncdf(-d1),ncdf(d1)-1
def tdays(a,b):
    n=0;d=a
    while d<b:
        d+=timedelta(days=1)
        if d.weekday()<5:n+=1
    return n
def occ(u,e,cp,k):return '%s%s%s%d.US'%(u[:-3],e.replace('-','')[2:],cp,round(k*1000))
def lb(a,to=30):
    import subprocess
    r=subprocess.run([C.LB]+a+['--format','json'],capture_output=True,text=True,timeout=to);return json.loads(r.stdout or '[]')
STATS=None
def stats():
    global STATS
    if STATS is None:
        try:STATS=json.load(open(os.path.join(_D,'hist','bt_stats.json')))
        except Exception:STATS={}
    return STATS
def session_info(bars):
    """bars parsed regular 15m incl. today; returns prior-day low/high, today low/high, swing."""
    days=sorted({b['d'] for b in bars});today=datetime.now(ET).date()
    cur=[b for b in bars if b['d']==today];prev=[b for b in bars if b['d']==(days[-2] if cur and len(days)>1 else days[-1])]
    return {'pdl':min(b['l'] for b in prev),'pdh':max(b['h'] for b in prev),'tl':min(b['l'] for b in cur) if cur else None,'th':max(b['h'] for b in cur) if cur else None}
def bias(bars,lv):
    if len(bars)<60:return {'bias':'neutral','why':'数据不足'}
    c=[b['c'] for b in bars];e20,e50=ema(c,20),ema(c,50);d=bars[-1]['d'];db=[b for b in bars if b['d']==d]
    pv=sum((b['h']+b['l']+b['c'])/3*b['v'] for b in db);vv=sum(b['v'] for b in db);vw=pv/vv if vv else c[-1]
    sc=(1 if c[-1]>vw else -1)+(1 if e20[-1]>e50[-1] else -1);why=['价%sVWAP'%('>' if c[-1]>vw else '<'),'EMA20%sEMA50'%('>' if e20[-1]>e50[-1] else '<')]
    gf=(lv or {}).get('gamma_flip')
    if gf:sc+=1 if c[-1]>gf else -1;why.append('价%s伽马场'%('>' if c[-1]>gf else '<'))
    b='bull' if sc>=2 else 'bear' if sc<=-2 else 'neutral'
    return {'bias':b,'score':sc,'why':'、'.join(why)+'（%s 15m收盘）'%bj(bars[-1]['t']+900)}
def compute_live(s):
    proxy=s=='.SPX.US';u='SPY.US' if proxy else s
    q=C.quote(u);spot=float(q['last']);now=datetime.now(ET);today=now.date()
    ratio=float(C.quote(s)['last'])/spot if proxy else 1.0
    lv=C._c.get(('L',u));lv=lv[1] if lv and lv[1].get('has_options') else {}
    bars=parse(lb(['kline',u,'--period','15m','--count','600']))
    si=session_info(bars)
    exps=[e['expiry_date'] for e in lb(['option','chain',u],20)]
    cds={e:(date.fromisoformat(e)-today).days for e in exps if e>today.isoformat()};sel=[]
    for tgt,lo,hi in((7,6,8),(10,9,12)):
        pool=[e for e in cds if e not in [x for x,_ in sel]]
        if not pool:break
        inr=[e for e in pool if lo<=cds[e]<=hi];e=min(inr or pool,key=lambda e:(abs(cds[e]-tgt),cds[e]));sel.append((e,tgt))
    exps=sel
    open_now=now.weekday()<5 and (9,30)<=(now.hour,now.minute)<(16,0)
    if open_now:frac=max(0.02,(16*60-(now.hour*60+now.minute))/390)
    elif now.weekday()<5 and (now.hour,now.minute)<(9,30):frac=1.0
    else:frac=0.0
    out=[];st=stats();W=WID.get(u,5);STp=step(u,spot)
    for e,bk in exps:
        dte=tdays(today,date.fromisoformat(e))
        if dte==0 and frac==0:continue
        Tt=(frac+dte)/252
        ch=lb(['option','chain',u,'--date',e],20);ks=sorted(float(r['strike']) for r in ch if r.get('standard','true')=='true')
        ks=[k for k in ks if abs(k/spot-1)<=0.16]
        atm=sorted(ks,key=lambda k:abs(k-spot))[:2]
        syms=[occ(u,e,'P',k) for k in ks if k<=spot or k in atm]+[occ(u,e,'C',k) for k in ks if k>=spot or k in atm]
        Q=[]
        for i in range(0,len(syms),50):
            try:Q+=lb(['option','quote']+syms[i:i+50])
            except Exception:pass
        Q=[{'k':float(x['strike_price']),'cp':x['direction'][0],'iv':float(x.get('implied_volatility') or 0),'last':float(x.get('last') or 0),'oi':int(x.get('open_interest') or 0)} for x in Q]
        ivs=[x['iv'] for x in Q if x['k'] in atm and x['iv']>0]
        if not ivs:continue
        iv=sum(ivs)/len(ivs);EM=spot*iv*math.sqrt((frac+dte)/252)
        row={'expiry':e,'bucket':bk,'cal_days':cds[e],'label':'%d天'%bk,'dte':dte,'atm_iv':round(iv,4),'em':round(EM,2),'mult':MULTS[bk]}
        for side in('B','S'):
            cp='P' if side=='B' else 'C';L=sorted([x for x in Q if x['cp']==cp and x['iv']>0],key=lambda x:x['k'])
            for x in L:x['th'],x['delta']=bs(spot,x['k'],x['iv'],max(Tt,1e-4),cp)
            key=min(si['pdl'],si['tl'] or 1e9) if side=='B' else max(si['pdh'],si['th'] or 0)
            pw=lv.get('put_wall' if side=='B' else 'call_wall')
            short,_=safe_short(side,spot,iv,dte,frac,key,m=MULTS[bk],st=1e-9)
            if pw and ((side=='B' and pw<spot and short-1.0*EM<=pw<short) or (side=='S' and pw>spot and short<pw<=short+1.0*EM)):short=pw
            cand=[x for x in L if (x['k']<=short if side=='B' else x['k']>=short)]
            cand=cand[::-1] if side=='B' else cand
            sh=next((x for x in cand if abs(x['delta'])<=0.10),None)
            if not sh:row[side]={'error':'无满足条件的行权价'};continue
            lk=sh['k']-W if side=='B' else sh['k']+W
            lg=min([x for x in L if (x['k']<sh['k'] if side=='B' else x['k']>sh['k'])] or [sh],key=lambda x:abs(x['k']-lk))
            if lg is sh:row[side]={'error':'无长腿'};continue
            cl=round(sh['last']-lg['last'],2);ct=round(sh['th']-lg['th'],2);w=abs(sh['k']-lg['k']);cr=max(ct,0.01)
            hs=st.get('_uncond',{}).get('%s|%d'%(s,bk));ss=st.get(s,{}).get('br',{}).get(str(bk))
            r={'short':sh['k'],'long':lg['k'],'delta':round(sh['delta'],3),'iv':sh['iv'],'credit_last':cl,'credit_theo':ct,'width':w,
               'max_loss':round((w-cr)*100),'breakeven':round(sh['k']-cr if side=='B' else sh['k']+cr,2),'touch_prob':round(min(1,2*abs(sh['delta'])),3),
               'key':round(key,2),'em_line':round(spot-row['mult']*EM if side=='B' else spot+row['mult']*EM,2),
               'stop':'标的触及 %.2f（短腿%s0.3×EM）或价差≥2×收入'%((sh['k']+0.3*EM) if side=='B' else (sh['k']-0.3*EM),'+' if side=='B' else '−'),
               'low_credit':ct<0.03*w,'hist_touch':hs[1] if hs else None,'hist_close':hs[2] if hs else None,'sig_hist':ss}
            if proxy:r['spx_short']=round(sh['k']*ratio/5)*5;r['spx_long']=round(lg['k']*ratio/5)*5
            row[side]=r
        out.append(row)
    return {'symbol':s,'underlying':u,'proxy':proxy,'ratio':round(ratio,4),'spot':spot,'frac':round(frac,3),'session':'盘中' if open_now else '休市（按下一交易日全天估算）' if frac==1 else '收盘后',
            'bias':bias(bars,lv),'spreads':out,'levels_key':{'pdl':si['pdl'],'pdh':si['pdh']},'note':'收入按最新成交价/理论价(BS, IV)；CLI无买卖盘，下单以券商实时 mid 为准。触碰概率≈2×|Δ|。仅监控，不下单。','time_bj':datetime.now(BJ).strftime('%m-%d %H:%M')}
def _run(s):
    try:v=compute_live(s)
    except Exception as e:v={'error':str(e),'retry':True}
    with _lk:_cache[s]=(time.time()-(280 if v.get('retry') else 0),v);_pend.discard(s)
def spreads_async(sym):
    s=C.norm(sym)
    if not s:return {'error':'无效代码'}
    with _lk:
        e=_cache.get(s)
        if not(e and time.time()-e[0]<300) and s not in _pend:_pend.add(s);threading.Thread(target=_run,args=(s,),daemon=True).start()
    return e[1] if e else {'pending':True}
_sg={}
def signals_api(sym):
    s=C.norm(sym)
    if not s:return {'error':'无效代码'}
    e=_sg.get(s)
    if e and time.time()-e[0]<60:return e[1]
    raw=C.kline(s,'15m');now=time.time()
    bars=[b for b in parse([{'time':datetime.fromtimestamp(x['t'],timezone.utc).isoformat().replace('+00:00','Z'),'open':x['o'],'high':x['h'],'low':x['l'],'close':x['c'],'volume':x['v'],'session':'Pre' if x['ext'] else 'Intraday'} for x in raw]) if b['t']+900<=now]
    # extend history from disk if available for indicator warmup / older signals
    try:
        h=parse(json.load(open(os.path.join(_D,'hist','%s.json'%s)))['m15']);t0=bars[0]['t'] if bars else 1e18
        bars=[b for b in h if b['t']<t0]+bars
    except Exception:pass
    lv=C._c.get(('L',s));lv=lv[1] if lv and lv[1].get('has_options') else None
    sg=signals(bars,P0,lv)[-12:];D={}
    for b in bars:D[b['d']]=b['c']
    out=[]
    for x in sg:
        hm=datetime.fromtimestamp(x['t'],ET);frac=max(0.05,(16*60-(hm.hour*60+hm.minute+15))/390);st=step(s,x['price'])
        strikes={b:safe_short(x['side'],x['price'],x['rv'],TD[b],frac,x['key'],m=MULTS[b],st=st)[0] for b in(7,10)}
        out.append({'side':x['side'],'t':x['t'],'time_bj':bj(x['t']+900),'price':round(x['price'],2),'held':x['held'],'rsi':round(x['rsi'],1),'strikes_rv':strikes,
                    'label':('Bull Put 卖%g' if x['side']=='B' else 'Bear Call 卖%g')%strikes[7],'day_close':round(D.get(x['d'],0),2),'today':x['d']==datetime.now(ET).date()})
    st=stats().get(s,{})
    res={'symbol':s,'signals':out,'stats':{k:st.get(k) for k in('n','hit30','hit60','hitc','mae_med')},'params':P0}
    _sg[s]=(time.time(),res);return res
def prewarm():
    for s in('SPX','QQQ','SPY','NVDA','TSLA','AAPL','COHR'):
        try:spreads_async(s);time.sleep(20)
        except Exception:pass
