"""Pure 3m+5m resonance rule (backtest). At each closed 1m bar (RTH 9:30-15:45 ET), look at the last CLOSED 3m and 5m bars.
TF state bull/bear by MODE:
  trend : close>EMA10>EMA20 (bear: close<EMA10<EMA20)
  break : close > previous TF bar high and close>EMA20 (bear mirror)
  both  : trend AND break
Signal fires when both TFs first agree (state change into agreement), optional 1m VWAP side filter, cooldown COOL min per direction."""
from feat import *
def tf_states(bars,tf,mode):
    k10,k20=2/11,2/21;e10=e20=None;cur=None;prev=None;st={};out={}
    for b in bars:
        bs=b['t']-b['t']%(tf*60)
        if cur is None or cur['t']!=bs:cur={'t':bs,'h':b['h'],'l':b['l'],'c':b['c']}
        else:cur['h']=max(cur['h'],b['h']);cur['l']=min(cur['l'],b['l']);cur['c']=b['c']
        if b['t']+60==bs+tf*60:
            c=cur['c'];e10=c if e10 is None else c*k10+e10*(1-k10);e20=c if e20 is None else c*k20+e20*(1-k20)
            tr=1 if c>e10>e20 else -1 if c<e10<e20 else 0
            br=0
            if prev:br=1 if c>prev['h'] and c>e20 else -1 if c<prev['l'] and c<e20 else 0
            s={'trend':tr,'break':br,'both':tr if tr==br else 0}[mode]
            prev=dict(cur);st=s
        out[b['t']]=st if st!={} else 0
    return out
def signals(F,mode='trend',vwap=False,cool=15,start=570,end=945):
    s3=tf_states(F,3,mode);s5=tf_states(F,5,mode);out=[];last={};pa=0
    for i,f in enumerate(F):
        a=s3[f['t']] if s3[f['t']]==s5[f['t']] else 0
        new=a!=0 and a!=pa;pa=a
        if not(new and f['rth'] and start<=f['m']<end):continue
        if vwap and (f['c']-f['vwap'])*a<=0:continue
        d='CALL' if a>0 else 'PUT'
        if d in last and f['t']-last[d]<cool*60:continue
        last[d]=f['t']
        def fwd(n):
            j=i+n;return None if j>=len(F) or F[j]['day']!=f['day'] or not F[j]['rth'] else round((F[j]['c']-f['c'])*a,2)
        out.append(dict(day=f['day'],bj=datetime.fromtimestamp(f['t']+60,BJ).strftime('%m-%d %H:%M'),dir=d,setup='R35',price=f['c'],m15=fwd(15),m30=fwd(30),m60=fwd(60)))
    return out
def stats(S,days):
    r={'n':len(S),'pd':len(S)/max(1,days)}
    for k in('m15','m30','m60'):
        v=[s[k] for s in S if s[k] is not None];r[k]=(100*sum(x>0 for x in v)/len(v) if v else 0,sum(v)/len(v) if v else 0)
    return r
def fmt(r):return 'n=%d %.1f/day | 15m %.0f%% %+.2f | 30m %.0f%% %+.2f | 60m %.0f%% %+.2f'%(r['n'],r['pd'],*r['m15'],*r['m30'],*r['m60'])
