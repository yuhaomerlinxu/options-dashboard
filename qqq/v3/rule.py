"""Candidate v3 QQQ 1m rule (not deployed). Three setups, regular session only, closed bars:
 OPEN  : first 5 bars after 9:30; open bar volume >= 3x avg of prior 30 bars; first bar closing beyond VWAP+EMA10+EMA20 in the direction of its own move -> CALL/PUT
 REV   : swing extreme (highest high / lowest low of last 30 bars) made in last 5 bars, RSI14 hit >=RSI_HI (<=RSI_LO) in last 10 bars,
         now close back through EMA10 and beyond previous bar's low (high) -> PUT (CALL)
 REV only until 10:30 ET (REV_END); cooldown COOL min per direction; no new signals after 15:45 ET."""
from feat import *
P=dict(CONF='e20call',REV_END=630,OPENVOL=3.0,RSI_HI=72,RSI_LO=38,SWING=30,RECENT=8,COOL=10,LAST=945)
def conf(p,F,i,sg):
    c=p['CONF'];f=F[i]
    if c=='base':return True
    if c=='e20':return (f['c']-f['e20'])*sg>0
    if c=='e20call':return sg<0 or f['c']>f['e20']
    if c=='hl':  # extreme not set on this or previous bar (higher low / lower high)
        k=lambda x:x['l'] if sg>0 else -x['h']
        return min(k(F[i]),k(F[i-1]))>min(k(x) for x in F[i-6:i-1])
    if c=='hl+e20call':return conf(dict(p,CONF='hl'),F,i,sg) and conf(dict(p,CONF='e20call'),F,i,sg)
    return True
def signals(F,**kw):
    p={**P,**kw};out=[];last={}
    for i,f in enumerate(F):
        if not f['rth'] or f['m']>=p['LAST'] or i<p['SWING']:continue
        d=None;why=None;pr=F[i-1]
        # opening drive
        if f['m']<575:
            o=next(j for j in range(i,-1,-1) if F[j]['m']==570 or not F[j]['rth'] or F[j]['day']!=f['day'])
            if F[o]['m']==570:
                av=sum(x['v'] for x in F[o-30:o])/30
                if F[o]['v']>=p['OPENVOL']*av and not any(s['day']==f['day'] and s['setup']=='OPEN' for s in out):
                    if f['c']>max(f['vwap'],f['e10'],f['e20']) and f['c']>pr['c']:d,why='CALL','OPEN'
                    elif f['c']<min(f['vwap'],f['e10'],f['e20']) and f['c']<pr['c']:d,why='PUT','OPEN'
        if not d and f['m']<p['REV_END']:
            W=F[i-p['SWING']+1:i+1];R=F[i-p['RECENT']+1:i+1];rs=[x['rsi'] for x in F[i-9:i+1]]
            hh=max(x['h'] for x in W);ll=min(x['l'] for x in W)
            if any(x['h']==hh for x in R) and max(rs)>=p['RSI_HI'] and f['c']<f['e10'] and f['c']<pr['l'] and conf(p,F,i,-1):d,why='PUT','REV'
            elif any(x['l']==ll for x in R) and min(rs)<=p['RSI_LO'] and f['c']>f['e10'] and f['c']>pr['h'] and conf(p,F,i,1):d,why='CALL','REV'
        if not d:continue
        if d in last and f['t']-last[d]<p['COOL']*60:continue
        last[d]=f['t']
        sg=1 if d=='CALL' else -1
        def fwd(n):
            j=i+n
            return None if j>=len(F) or F[j]['day']!=f['day'] or not F[j]['rth'] else round((F[j]['c']-f['c'])*sg,2)
        out.append(dict(day=f['day'],bj=datetime.fromtimestamp(f['t']+60,BJ).strftime('%m-%d %H:%M'),et=(f['et']+timedelta(minutes=1)).strftime('%H:%M'),dir=d,setup=why,price=f['c'],m15=fwd(15),m30=fwd(30),m60=fwd(60)))
    return out
def report(S):
    import collections;by=collections.defaultdict(list)
    for s in S:by[s['day']].append(s)
    for d in sorted(by):print(d,len(by[d]),' '.join('%s%s%s'%(s['et'],s['dir'][0],'+' if (s['m30'] or 0)>0 else '-') for s in by[d]))
    for k in('m15','m30','m60'):
        v=[s[k] for s in S if s[k] is not None];print(k,'n',len(v),'hit %.0f%%'%(100*sum(x>0 for x in v)/len(v)),'avg %+.2f'%(sum(v)/len(v)))
    for st in('OPEN','REV'):
        v=[s['m30'] for s in S if s['setup']==st and s['m30'] is not None]
        if v:print(st,'m30 n',len(v),'hit %.0f%%'%(100*sum(x>0 for x in v)/len(v)),'avg %+.2f'%(sum(v)/len(v)))
if __name__=='__main__':
    F=features(load());S=signals(F);report(S)
    print('--- 10/27');[print(s) for s in S if str(s['day'])=='2026-10-27']
