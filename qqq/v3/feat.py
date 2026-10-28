import json
from datetime import datetime,timedelta,timezone
from zoneinfo import ZoneInfo
ET=ZoneInfo('America/New_York');BJ=timezone(timedelta(hours=8))
def ema_series(v,n):
    k=2/(n+1);o=[];e=None
    for x in v:e=x if e is None else x*k+e*(1-k);o.append(e)
    return o
def rsi_series(c,n=14):
    o=[50]*len(c);ag=al=None
    for i in range(1,len(c)):
        d=c[i]-c[i-1];g,l=max(d,0),max(-d,0)
        if ag is None:ag,al=g,l
        else:ag=(ag*(n-1)+g)/n;al=(al*(n-1)+l)/n
        o[i]=100 if al==0 else 100-100/(1+ag/al)
    return o
def features(bars):
    c=[b['c'] for b in bars]
    E={n:ema_series(c,n) for n in (10,20,60,120)};R=rsi_series(c)
    st=[];
    for i in range(len(R)):
        w=R[max(0,i-13):i+1];lo,hi=min(w),max(w);st.append(0.5 if hi==lo else (R[i]-lo)/(hi-lo))
    K=[sum(st[max(0,i-2):i+1])/len(st[max(0,i-2):i+1]) for i in range(len(st))]
    D=[sum(K[max(0,i-2):i+1])/len(K[max(0,i-2):i+1]) for i in range(len(K))]
    out=[];pv=vv=0;day=None
    for i,b in enumerate(bars):
        d=datetime.fromtimestamp(b['t'],ET);m=d.hour*60+d.minute
        rth=570<=m<960
        if rth and day!=d.date():day=d.date();pv=vv=0
        if rth:pv+=(b['h']+b['l']+b['c'])/3*b['v'];vv+=b['v']
        out.append(dict(b,et=d,m=m,rth=rth,day=d.date(),e10=E[10][i],e20=E[20][i],e60=E[60][i],e120=E[120][i],rsi=R[i],k=K[i]*100,dd=D[i]*100,vwap=pv/vv if vv else b['c']))
    return out
def load():
    A=json.load(open('/tmp/all.json'));return A['bars']
