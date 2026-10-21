# Options Radar — MARKET DATA ONLY (quote/kline/option chain/option quote/finance-calendar). No trade commands ever.
import json,subprocess,threading,time,math,os,re,glob,traceback
from datetime import datetime,timedelta,date
from zoneinfo import ZoneInfo
from urllib.parse import urlparse,parse_qs
ET=ZoneInfo('America/New_York');BJ=ZoneInfo('Asia/Shanghai')
D=os.path.dirname(os.path.abspath(__file__));DATA=os.path.join(D,'radar_data');os.makedirs(DATA,exist_ok=True)
CFG=os.path.join(D,'radar_watchlist.txt')
DEFAULT='QQQ SPY IWM NVDA TSLA AAPL MSFT AMZN GOOGL META AMD AVGO COHR MRVL ORCL PLTR NFLX'
ETF={'QQQ','SPY','IWM'}
ALLOWED={'quote','kline','option','finance-calendar'}
_lk=threading.Lock();_last=[0.0]
def lb(args,timeout=25):
    assert args[0] in ALLOWED and (args[0]!='option' or args[1] in('chain','quote'))
    with _lk:
        w=0.35-(time.time()-_last[0])
        if w>0:time.sleep(w)
        try:r=subprocess.run([os.environ.get('LBSAFE','/app/lbsafe')]+args+['--format','json'],capture_output=True,text=True,timeout=timeout)
        finally:_last[0]=time.time()
    return json.loads(r.stdout or 'null')
_c={}
def cached(k,ttl,f):
    v=_c.get(k)
    if v and time.time()-v[0]<ttl:return v[1]
    x=f();_c[k]=(time.time(),x);return x
def watchlist():
    try:s=open(CFG).read()
    except Exception:s=DEFAULT
    return [x.strip().upper() for x in s.replace(',',' ').split() if x.strip()]
fl=lambda x:float(x) if x not in(None,'','--') else 0.0
# ---- Black-Scholes ----
N=lambda x:0.5*(1+math.erf(x/math.sqrt(2)))
R=0.04
def bs(S,K,T,v,cp):
    if T<=0 or v<=0:return max(0,(S-K) if cp=='C' else (K-S))
    d1=(math.log(S/K)+(R+v*v/2)*T)/(v*math.sqrt(T));d2=d1-v*math.sqrt(T)
    return S*N(d1)-K*math.exp(-R*T)*N(d2) if cp=='C' else K*math.exp(-R*T)*N(-d2)-S*N(-d1)
def delta(S,K,T,v,cp):
    if T<=0 or v<=0:return (1.0 if S>K else 0.0) if cp=='C' else (-1.0 if S<K else 0.0)
    d1=(math.log(S/K)+(R+v*v/2)*T)/(v*math.sqrt(T));return N(d1) if cp=='C' else N(d1)-1
# ---- indicators ----
def ema(a,n):
    k=2/(n+1);e=a[0];o=[]
    for x in a:e=x*k+e*(1-k);o.append(e)
    return o
def rsi(a,n=14):
    g=l=0
    for i in range(1,n+1):d=a[i]-a[i-1];g+=max(d,0);l+=max(-d,0)
    g/=n;l/=n
    for i in range(n+1,len(a)):d=a[i]-a[i-1];g=(g*(n-1)+max(d,0))/n;l=(l*(n-1)+max(-d,0))/n
    return 100 if l==0 else 100-100/(1+g/l)
def earnings():
    def f():
        t=datetime.now(ET).date();out={}
        d=lb(['finance-calendar','report','--start',t.isoformat(),'--end',(t+timedelta(days=75)).isoformat(),'--market','US','--count','3000'],timeout=60) or {}
        for g in d.get('list',[]):
            for i in g.get('infos',[]):
                m=re.search(r'(\d{4})\.(\d{2})\.(\d{2})',i.get('date',''))
                s=i.get('symbol','').split('.')[0]
                if m and s and s not in out:out[s]={'date':'%s-%s-%s'%m.groups(),'when':i.get('date_type','')}
        return out
    try:return cached('earn',6*3600,f)
    except Exception:return {}
def osym(s,exp,cp,k):
    return '%s%s%s%d.US'%(s,exp[2:4]+exp[5:7]+exp[8:10],cp,round(float(k)*1000))
def analyze(s,earn,today):
    q=lb(['quote',s+'.US'])[0];S=fl(q['last'])
    ext=None
    for kk in('overnight','post_market','pre_market'):
        if q.get(kk) and fl(q[kk].get('last')):ext=ext or {'session':kk,'last':fl(q[kk]['last'])}
    exps=[e['expiry_date'] for e in cached('exp'+s,3600,lambda:lb(['option','chain',s+'.US']))]
    dte=lambda e:(date.fromisoformat(e)-today).days
    best=None
    for b in exps:
        fr=(date.fromisoformat(b)-timedelta(days=7)).isoformat()
        if fr in exps and 24<=dte(b)<=42:
            sc=abs(dte(b)-31)
            if best is None or sc<best[0]:best=(sc,fr,b)
    if not best:return {'symbol':s,'skip':'无合适的 ~31DTE / 前腿相差7天 到期组合'}
    _,fe,be=best
    ch={e:cached('ch%s%s'%(s,e),600,lambda e=e:lb(['option','chain',s+'.US','--date',e])) for e in(fe,be)}
    def rows(e):
        o=[]
        for r in ch[e]:
            k=fl(r['strike']);o.append({'k':k,'C':{'last':fl(r['call_last']),'iv':fl(r['call_iv']),'vol':fl(r['call_vol'])},'P':{'last':fl(r['put_last']),'iv':fl(r['put_iv']),'vol':fl(r['put_vol'])},'std':r.get('standard')=='true'})
        return [x for x in o if x['std']]
    F,B=rows(fe),rows(be)
    def atmiv(rs):
        rs2=sorted(rs,key=lambda x:abs(x['k']-S))[:2];v=[x[c]['iv'] for x in rs2 for c in 'CP' if 0.03<x[c]['iv']<3]
        return sum(v)/len(v) if v else 0
    ivf,ivb=atmiv(F),atmiv(B)
    if not ivf or not ivb:return {'symbol':s,'skip':'ATM IV 缺失'}
    Tf,Tb=dte(fe)/365,dte(be)/365
    # OI around ATM on front expiry (walls, max pain within window)
    near=sorted(F,key=lambda x:abs(x['k']-S))[:16]
    syms=[osym(s,fe,c,x['k']) for x in near for c in 'CP']
    oi={}
    try:
        for r in lb(['option','quote']+syms) or []:oi[r['symbol']]=(r.get('open_interest') or 0,r.get('volume') or 0)
    except Exception:pass
    coi={x['k']:oi.get(osym(s,fe,'C',x['k']),(0,0))[0] for x in near};poi={x['k']:oi.get(osym(s,fe,'P',x['k']),(0,0))[0] for x in near}
    up=[k for k in coi if k>=S];dn=[k for k in poi if k<=S]
    cwall=max(up,key=lambda k:coi[k]) if up and max(coi[k] for k in up)>0 else None
    pwall=max(dn,key=lambda k:poi[k]) if dn and max(poi[k] for k in dn)>0 else None
    ks=sorted(coi);mp=None
    if ks and sum(coi.values())+sum(poi.values())>0:
        mp=min(ks,key=lambda P:sum(coi[k]*max(0,P-k)+poi[k]*max(0,k-P) for k in ks))
    # trend
    db=[fl(x['close']) for x in cached('kd'+s,1800,lambda:lb(['kline',s+'.US','--period','day','--count','120']))]
    hb=cached('kh'+s,600,lambda:lb(['kline',s+'.US','--period','1h','--count','80']))
    hc=[fl(x['close']) for x in hb]
    e20,e50=ema(db,20)[-1],ema(db,50)[-1];r14=rsi(db);h20=ema(hc,20)[-1]
    lastday=hb[-7:];vw=sum((fl(x['high'])+fl(x['low'])+fl(x['close']))/3*fl(x['volume']) for x in lastday)/max(1,sum(fl(x['volume']) for x in lastday))
    t=(1 if S>e20 else -1)+(1 if e20>e50 else -1)+(1 if hc[-1]>h20 else -1)+(1 if S>vw else -1)+(1 if r14>55 else -1 if r14<45 else 0)
    trend='bull' if t>=3 else 'bear' if t<=-3 else 'neutral'
    # earnings
    ed=earn.get(s);eflag='na' if s in ETF else 'unknown'
    if ed:
        d0=date.fromisoformat(ed['date'])
        eflag='before_front' if today<=d0<=date.fromisoformat(fe) else 'between' if d0<=date.fromisoformat(be) else 'after'
    # pick strategy & legs
    def pick(rs,cp,T,target):
        c=[x for x in rs if x[cp]['last']>0.05 and 0.03<x[cp]['iv']<3]
        return min(c,key=lambda x:abs(abs(delta(S,x['k'],T,x[cp]['iv'],cp))-target)) if c else None
    if trend=='bull':st,cp,bl,fl_='Call Diagonal','C',pick(B,'C',Tb,0.65),pick(F,'C',Tf,0.30)
    elif trend=='bear':st,cp,bl,fl_='Put Diagonal','P',pick(B,'P',Tb,0.65),pick(F,'P',Tf,0.30)
    else:
        if t>=0:
            cp,st='C','Call Calendar';K=cwall if cwall and S*1.01<=cwall<=S*1.04 else min([x['k'] for x in F],key=lambda k:abs(k-S*1.025),default=None)
        else:
            cp,st='P','Put Calendar';K=pwall if pwall and S*0.96<=pwall<=S*0.99 else min([x['k'] for x in F],key=lambda k:abs(k-S*0.975),default=None)
        bl=next((x for x in B if x['k']==K),None);fl_=next((x for x in F if x['k']==K),None)
    if not bl or not fl_ or bl[cp]['last']<=0 or fl_[cp]['last']<=0:return {'symbol':s,'skip':'腿价格缺失'}
    debit=bl[cp]['last']-fl_[cp]['last']
    if debit<=0.02:return {'symbol':s,'skip':'净借记≤0(last价不同步)'}
    legs=[]
    for side,x,e,T in(('BUY',bl,be,Tb),('SELL',fl_,fe,Tf)):
        sym=osym(s,e,cp,x['k']);o=oi.get(sym)
        legs.append({'side':side,'expiry':e,'dte':dte(e),'strike':x['k'],'type':'Call' if cp=='C' else 'Put','symbol':sym,'last':x[cp]['last'],'iv':round(x[cp]['iv'],4),'delta':round(delta(S,x['k'],T,x[cp]['iv'],cp),3),'vol':x[cp]['vol'],'oi':o[0] if o else None})
    try:
        for r in lb(['option','quote']+[l['symbol'] for l in legs if l['oi'] is None]) or []:
            for l in legs:
                if l['symbol']==r['symbol']:l['oi']=r.get('open_interest');l['vol']=r.get('volume',l['vol'])
    except Exception:pass
    # P/L at front expiry (BS, back leg IV held)
    Tr=(dte(be)-dte(fe))/365;grid=[S*(0.85+0.0015*i) for i in range(201)]
    pl=[bs(P,bl['k'],Tr,bl[cp]['iv'],cp)-max(0,(P-fl_['k']) if cp=='C' else (fl_['k']-P))-debit for P in grid]
    prof=[P for P,v in zip(grid,pl) if v>0]
    be_lo,be_hi=(min(prof),max(prof)) if prof else (None,None)
    bes=[round(grid[i]+(grid[i+1]-grid[i])*(-pl[i])/(pl[i+1]-pl[i]),2) for i in range(len(pl)-1) if (pl[i]<=0)!=(pl[i+1]<=0)]
    curve=[[round(P,2),round(v*100,1)] for P,v in list(zip(grid,pl))[::4]]
    maxp=max(pl)
    # score
    edge=(ivf-ivb)/ivb
    if edge<-0.15:return {'symbol':s,'skip':'后月IV明显高于前月(%.0f%% vs %.0f%%)'%(ivb*100,ivf*100)}
    s_term=max(0,min(40,20+edge*200))
    s_trend=min(25,abs(t)*5) if trend!='neutral' else (15 if ((cwall or mp) and abs(((cwall if cp=='C' else pwall) or mp or S)-S)/S<0.02) else 8)
    minoi=min((l['oi'] or 0) for l in legs);minv=min((l['vol'] or 0) for l in legs)
    s_liq=min(20,4*math.log10(1+minoi)+2*math.log10(1+minv))
    s_ev={'na':15,'after':15,'between':8,'unknown':6,'before_front':0}[eflag]
    score=round(s_term+s_trend+s_liq+s_ev-(15 if eflag=='before_front' else 0),1)
    tz={'bull':'多头','bear':'空头','neutral':'震荡'}[trend]
    evtxt={'na':'ETF无财报','after':'财报(%s)在两腿之后'%(ed or {}).get('date'),'between':'财报(%s)在前后腿之间:后腿IV含事件溢价,注意财报后IV回落'%(ed or {}).get('date'),'unknown':'财报日期未知,请自行核对','before_front':'⚠前腿跨财报(%s):属于刻意卖IV crush,风险高'%(ed or {}).get('date')}[eflag]
    reason='%s:趋势%s(得分%+d;价%s EMA20 %.2f/EMA50 %.2f,RSI %.0f,60m %s EMA20,%s近7h VWAP %.2f)。期限结构:前月(%s) ATM IV %.1f%% vs 后月(%s) %.1f%%,%s。%s。'%(
        s,tz,t,'>' if S>e20 else '<',e20,e50,r14,'站上' if hc[-1]>h20 else '跌破','高于' if S>vw else '低于',vw,fe,ivf*100,be,ivb*100,
        '前高后低(倒挂)有利卖前月' if edge>0.03 else '基本平坦' if edge>-0.05 else '后月略高,优势较小',evtxt)
    if eflag=='unknown' and edge<-0.08:reason+='后月IV明显偏高,可能含未确认的财报/事件,谨慎。'
    if trend=='neutral':reason+='震荡格局,行权价取%s附近(Call墙 %s / Put墙 %s / 最大痛点 %s),期望价格在前腿到期时靠近该价。'%(legs[0]['strike'],cwall,pwall,mp)
    return {'symbol':s,'spot':S,'ext':ext,'strategy':st,'score':score,'parts':{'term':round(s_term,1),'trend':round(s_trend,1),'liq':round(s_liq,1),'event':s_ev},
        'front':{'expiry':fe,'dte':dte(fe),'atm_iv':round(ivf,4)},'back':{'expiry':be,'dte':dte(be),'atm_iv':round(ivb,4)},'term_edge':round(edge,4),
        'trend':trend,'trend_score':t,'ind':{'ema20':round(e20,2),'ema50':round(e50,2),'rsi':round(r14,1),'vwap7h':round(vw,2),'h_ema20':round(h20,2)},
        'levels':{'call_wall':cwall,'put_wall':pwall,'max_pain':mp,'note':'基于前腿到期ATM±8档OI'},'earnings':ed,'earn_flag':eflag,
        'legs':legs,'debit':round(debit,2),'max_loss':round(debit*100,0),'max_profit_est':round(maxp*100,0),'rr':round(maxp/debit,2),'breakevens':bes,'curve':curve,'short_dist':round((fl_['k']-S)/S,4),'score10':round(score/10,1),'profit_zone':[round(be_lo,2) if be_lo else None,round(be_hi,2) if be_hi else None],
        'limit_note':'无买卖盘数据:以 last 计算净借记 %.2f;实盘请在买卖中间价附近挂限价单,勿市价。'%debit,
        'exit':'止盈:组合价值涨 25–50%% (≈%.2f–%.2f);止损:亏损 50%% (≈%.2f);前腿 %s 到期前(建议前1–2个交易日)平仓或展期。'%(debit*1.25,debit*1.5,debit*0.5,fe),
        'reason':reason}
STATE={'running':False,'progress':'','last_run':0}
STF=os.path.join(DATA,'status.json');FLAG=os.path.join(DATA,'run.flag')
def wstat():
    try:json.dump(dict(STATE,pid=os.getpid(),ts=time.time()),open(STF+'.tmp','w'));os.replace(STF+'.tmp',STF)
    except Exception:pass
def rstat():
    try:
        d=json.load(open(STF))
        if time.time()-d.get('ts',0)>120:d['running']=False;d['stale']=True
        return d
    except Exception:return {'running':False,'progress':'','last_run':0,'stale':True}
def run(tag='scheduled'):
    if STATE['running']:return
    STATE['running']=True
    try:
        now=datetime.now(ET);today=now.date();earn=earnings();out=[];skips=[]
        for i,s in enumerate(watchlist()):
            STATE['progress']='%d/%d %s'%(i+1,len(watchlist()),s);wstat()
            try:
                r=analyze(s,earn,today);(skips if 'skip' in r else out).append(r)
            except Exception as e:skips.append({'symbol':s,'skip':'错误: %s'%e});traceback.print_exc()
        out.sort(key=lambda x:-x['score'])
        doc={'us_date':today.isoformat(),'generated_bj':datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S'),'session':'regular' if in_session() else 'off-hours (基于上一收盘/last价)','tag':tag,
             'top':out[:5],'others':out[5:],'skipped':skips,'earnings_source':'longbridge finance-calendar' if earn else 'unavailable'}
        fn=os.path.join(DATA,'radar_%s.json'%today.strftime('%Y%m%d'))
        hist=[]
        if os.path.exists(fn):
            try:hist=json.load(open(fn)).get('runs',[])
            except Exception:pass
        hist=(hist+[{'generated_bj':doc['generated_bj'],'top':[(x['symbol'],x['strategy'],x['score']) for x in doc['top']]}])[-40:]
        doc['runs']=hist;json.dump(doc,open(fn+'.tmp','w'),ensure_ascii=False);os.replace(fn+'.tmp',fn)
        STATE['last_run']=time.time()
    finally:STATE['running']=False;STATE['progress']='';wstat()
def in_session(n=None):
    n=n or datetime.now(ET)
    return n.weekday()<5 and (9*60+30)<=n.hour*60+n.minute<16*60
def scheduler():
    fs=sorted(glob.glob(os.path.join(DATA,'radar_*.json')))
    if fs:STATE['last_run']=os.path.getmtime(fs[-1])
    if not glob.glob(os.path.join(DATA,'radar_*.json')):run('startup')
    while True:
        try:
            wstat();n=datetime.now(ET)
            if os.path.exists(FLAG):
                os.remove(FLAG)
                if time.time()-STATE['last_run']>300:run('manual')
            if in_session(n):
                m=n.hour*60+n.minute-(9*60+35)  # first run 09:35 ET, then every 30 min
                if m>=0 and time.time()-STATE['last_run']>25*60 and m%30<5:run('open' if m<5 else 'refresh')
            elif n.weekday()<5 and n.hour*60+n.minute>=8*60 and n.hour<9 and time.time()-STATE['last_run']>6*3600:
                run('premarket')
        except Exception:traceback.print_exc()
        time.sleep(30)
def start():threading.Thread(target=scheduler,daemon=True).start()
if __name__=='__main__':scheduler()
PAGE=None
def handle(h,p,token):
    base='/qqq/radar';sub=p[len(base):]
    qs=parse_qs(urlparse(h.path).query)
    if sub=='':
        h.send(200,open(os.path.join(D,'radar.html'),'rb').read().replace(b'__TOKEN__',token.encode()),'text/html; charset=utf-8');return
    if sub=='/api':
        files=sorted(glob.glob(os.path.join(DATA,'radar_*.json')))
        dates=[os.path.basename(f)[6:14] for f in files];d=qs.get('d',[None])[0]
        st=rstat()
        if qs.get('run') and not st['running'] and time.time()-st.get('last_run',0)>300:open(FLAG,'w').write('1')
        f=os.path.join(DATA,'radar_%s.json'%d) if d in dates else (files[-1] if files else None)
        doc=json.load(open(f)) if f else None
        r={'status':'pending' if st['running'] else 'ok','progress':st.get('progress',''),'runner_alive':not st.get('stale'),'dates':dates,'data':doc,'in_session':in_session(),'server_time_bj':datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S'),'watchlist':watchlist()}
        h.send(200,json.dumps(r,ensure_ascii=False).encode(),'application/json; charset=utf-8');return
    h.send(404,b'not found','text/plain')
