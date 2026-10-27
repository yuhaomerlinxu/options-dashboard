# Volatility panel data: ATM IV per expiry, HV20, VIX/VXN, expected move. Market data only.
import math,time,threading,urllib.request,csv,io,json
from datetime import datetime,date
import chartapi as C
ET=C.ET
def _http(u,to=10):
    return urllib.request.urlopen(urllib.request.Request(u,headers={'User-Agent':'Mozilla/5.0'}),timeout=to).read().decode()
def cboe_hist(ix):
    def f():
        rows=list(csv.DictReader(io.StringIO(_http('https://cdn.cboe.com/api/global/us_indices/daily_prices/%s_History.csv'%ix,15))))
        return [(r['DATE'],float(r['CLOSE'])) for r in rows if r.get('CLOSE')]
    return C.cached(('cboeh',ix),3600,f)
def cboe_now(ix):
    def f():
        d=json.loads(_http('https://cdn.cboe.com/api/global/delayed_quotes/quotes/_%s.json'%ix))['data']
        return {'last':d.get('current_price'),'prev':d.get('prev_day_close'),'src':'Cboe(延迟)'}
    return C.cached(('cboen',ix),60,f)
def index(ix):
    o={'name':ix}
    try:
        if ix=='VIX':
            q=C.lb(['quote','.VIX.US'],10)[0];o.update(last=float(q['last']),prev=float(q['prev_close']),src='Longbridge')
        else:raise Exception()
    except Exception:
        try:o.update(cboe_now(ix))
        except Exception as e:o['error']=str(e)[:80]
    try:
        h=[c for _,c in cboe_hist(ix)][-252:];cur=o.get('last') or h[-1]
        lo,hi=min(h+[cur]),max(h+[cur])
        o.update(rank=round((cur-lo)/(hi-lo)*100,1) if hi>lo else None,pctl=round(sum(1 for c in h if c<cur)/len(h)*100,1),lo52=lo,hi52=hi)
    except Exception as e:o.setdefault('hist_error',str(e)[:80])
    return o
def hv(s,n=20):
    def f():
        b=C.lb(['kline',s,'--period','day','--count',str(n+5)],15)
        c=[float(x['close']) for x in b]
        r=[math.log(c[i]/c[i-1]) for i in range(1,len(c))][-n:]
        m=sum(r)/len(r);return math.sqrt(sum((x-m)**2 for x in r)/(len(r)-1)*252)
    return C.cached(('hv',s),600,f)
def dte(e):return (date.fromisoformat(e)-datetime.now(ET).date()).days
def atm(s,e,spot):
    ch=C.lb(['option','chain',s,'--date',e],20)
    ks=sorted({float(r['strike']) for r in ch if r.get('standard','true')=='true'},key=lambda k:abs(k-spot))[:2]
    q=C.lb(['option','quote']+[C.occ(s,e,cp,k) for k in ks for cp in 'CP'],20)
    k0=ks[0];ivs=[float(x['implied_volatility']) for x in q if float(x.get('implied_volatility') or 0)>0 and float(x['strike_price'])==k0]
    if not ivs:ivs=[float(x['implied_volatility']) for x in q if float(x.get('implied_volatility') or 0)>0]
    st=sum(float(x.get('last') or 0) for x in q if float(x['strike_price'])==k0)
    return {'exp':e,'dte':dte(e),'strike':k0,'iv':round(sum(ivs)/len(ivs),4) if ivs else None,'straddle':round(st,2) if st else None}
def compute(sym):
    s=C.norm(sym)
    if not s:return {'error':'bad symbol'}
    out={'symbol':s,'t':datetime.now(ET).strftime('%Y-%m-%d %H:%M ET')}
    try:q=C.quote(s);spot=float(q['last']);out['spot']=spot
    except Exception as e:return {'error':'quote: %s'%e}
    try:
        exps=[x['expiry_date'] for x in C.lb(['option','chain',s],15)]
        n=datetime.now(ET);today=n.date().isoformat();exps=[e for e in exps if e>today or (e==today and n.hour<16)]
        pick={'near':exps[0]}
        if len(exps)>1:
            wk=[e for e in exps if 5<=dte(e)<=9];pick['week']=wk[0] if wk else exps[1]
        m=min(exps,key=lambda e:abs(dte(e)-30));pick['d30']=m
        out['iv']={k:atm(s,e,spot) for k,e in pick.items()}
    except Exception as e:out['iv_error']=str(e)[:120]
    try:out['hv20']=round(hv(s),4)
    except Exception as e:out['hv_error']=str(e)[:80]
    out['vix']=index('VIX')
    if s in('QQQ.US','TQQQ.US','SQQQ.US','QQQM.US'):out['vxn']=index('VXN')
    iv=out.get('iv',{});ref=(iv.get('d30') or iv.get('near') or {}).get('iv')
    near=(iv.get('near') or {});wk=(iv.get('week') or {})
    em={}
    if near.get('iv') or ref:
        v=ref or near['iv'];em['d1']=round(spot*v*math.sqrt(1/252),2)
    if wk.get('iv') or ref:
        v=wk.get('iv') or ref;em['w1']=round(spot*v*math.sqrt(5/252),2)
    if near.get('straddle'):em['near_straddle']=near['straddle'];em['near_exp']=near['exp']
    if wk.get('straddle'):em['week_straddle']=wk['straddle'];em['week_exp']=wk['exp']
    out['em']=em
    if ref and out.get('hv20'):out['spread']=round(ref-out['hv20'],4)
    px=out.get('vxn');r=px.get('pctl') if px else None;lvl=None
    if r is not None:lvl='高' if r>=60 else '低' if r<=30 else '中';out['level_basis']='VXN 52周百分位(代理IV Percentile)'
    elif ref and out.get('hv20'):
        x=ref/out['hv20'];lvl='高' if x>=1.2 else '低' if x<=0.9 else '中';out['level_basis']='IV30/HV20=%.2f'%x
    if lvl:
        out['hint']={'高':'IV偏高：更适合垂直价差（牛市看涨/熊市看跌价差）或卖权利金，单腿买方成本高',
          '低':'IV偏低：期权便宜，更适合买入单腿看涨/看跌','中':'IV中性：单腿与价差均可，价差可降低成本'}[lvl]
        if out.get('spread') is not None and out['spread']>0.03 and lvl!='高':out['hint']+='；但IV明显高于实际波动，价差更划算'
        out['level']=lvl
    return out
def api(sym):return C.cached(('vol',C.norm(sym)),300,lambda:compute(sym))
def earnings(s):
    def f():
        from datetime import timedelta
        t=datetime.now(ET).date()
        for _ in range(3):
            try:d=C.lb(['finance-calendar','report','--symbol',s,'--start',t.isoformat(),'--end',(t+timedelta(days=120)).isoformat(),'--market','US'],30) or {}
            except Exception:d={}
            for g in d.get('list',[]):
                for i in g.get('infos',[]):
                    if i.get('symbol')==s:return {'date':i.get('date'),'content':i.get('content'),'when':{'Post':'盘后','Pre':'盘前'}.get(i.get('date_type'),i.get('date_type') or '')}
            if d.get('list') is not None:return None
            time.sleep(2)
        raise Exception('finance-calendar 无响应')
    return C.cached(('earn',s),3600,f)
_compute=compute
def compute(sym):
    o=_compute(sym)
    s=C.norm(sym)
    if s and not s.startswith('.') and s not in('QQQ.US','SPY.US'):
        try:o['earnings']=earnings(s)
        except Exception as e:o['earnings_error']=str(e)[:80]
    return o
def slow(sym):
    """HV20, VIX/VXN 52w rank, earnings — refreshed every 30 min."""
    s=C.norm(sym)
    def f():
        o={}
        try:o['hv20']=round(hv(s),4)
        except Exception as e:o['hv_error']=str(e)[:80]
        o['vix']=index('VIX')
        if s in('QQQ.US','TQQQ.US','SQQQ.US','QQQM.US'):o['vxn']=index('VXN')
        if not s.startswith('.') and s not in('QQQ.US','SPY.US'):
            try:o['earnings']=earnings(s)
            except Exception as e:o['earnings_error']=str(e)[:80]
        return o
    return C.cached(('slow',s),1800,f)
def hv_series(s,n=30):
    def f():
        b=C.lb(['kline',s,'--period','day','--count','300'],20)
        c=[float(x['close']) for x in b];r=[math.log(c[i]/c[i-1]) for i in range(1,len(c))]
        out=[]
        for i in range(n,len(r)+1):
            w=r[i-n:i];m=sum(w)/n;out.append(math.sqrt(sum((x-m)**2 for x in w)/(n-1)*252))
        return out[-252:]
    return C.cached(('hvs',s),3600,f)
