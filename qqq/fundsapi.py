# -*- coding: utf-8 -*-
"""Hedge-fund 13F + news radar. Positions from SEC 13F (official EDGAR first,
13f.info public parse as fallback when SEC blocks the host). News from Google News / CNBC RSS."""
from __future__ import annotations
import os, re, json, time, threading, urllib.request, urllib.parse, urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from html import unescape
from email.utils import parsedate_to_datetime

D = os.path.dirname(os.path.abspath(__file__))
DAT = os.path.join(D, 'funds_data')
os.makedirs(DAT, exist_ok=True)
BJ = timezone(timedelta(hours=8))
ETZ = timezone(timedelta(hours=-5))  # display helper; DST via zoneinfo if available
try:
    from zoneinfo import ZoneInfo
    ETZ = ZoneInfo('America/New_York')
except Exception:
    pass

SEC_UA = 'OptionsDashboard/1.0 (https://options-dashboard-pp77.onrender.com; yuhaomerlinxu@users.noreply.github.com)'
WEB_UA = 'OptionsDashboard/1.0 (https://options-dashboard-pp77.onrender.com; yuhaomerlinxu@users.noreply.github.com)'
SEC_SLEEP = 0.2
WEB_SLEEP = 0.35
NEWS_TTL = 45 * 60
FILING_TTL = 20 * 3600

FUNDS = [
    {'id':'bridgewater','name':'Bridgewater','name_zh':'桥水','cik':'0001350694','people':'Ray Dalio','news':['Bridgewater Associates','Ray Dalio']},
    {'id':'citadel','name':'Citadel Advisors','name_zh':'城堡','cik':'0001423053','people':'Ken Griffin','news':['Citadel Ken Griffin','Citadel Advisors']},
    {'id':'millennium','name':'Millennium','name_zh':'千禧年','cik':'0001273087','people':'Israel Englander','news':['Millennium Management hedge fund','Israel Englander']},
    {'id':'point72','name':'Point72','name_zh':'Point72','cik':'0001603466','people':'Steve Cohen','news':['Point72 Steve Cohen']},
    {'id':'deshaw','name':'D.E. Shaw','name_zh':'D.E. Shaw','cik':'0001009207','people':'David Shaw','news':['D.E. Shaw','DE Shaw']},
    {'id':'renaissance','name':'Renaissance','name_zh':'文艺复兴','cik':'0001037389','people':'Renaissance Technologies','news':['Renaissance Technologies']},
    {'id':'twosigma','name':'Two Sigma','name_zh':'Two Sigma','cik':'0001179392','people':'Two Sigma','news':['Two Sigma Investments']},
    {'id':'elliott','name':'Elliott','name_zh':'艾略特','cik':'0001791786','people':'Paul Singer','news':['Elliott Management','Paul Singer Elliott']},
    {'id':'pershing','name':'Pershing Square','name_zh':'潘兴广场','cik':'0001336528','people':'Bill Ackman','news':['Pershing Square','Bill Ackman']},
    {'id':'tiger','name':'Tiger Global','name_zh':'老虎全球','cik':'0001167483','people':'Chase Coleman','news':['Tiger Global','Chase Coleman']},
    {'id':'coatue','name':'Coatue','name_zh':'Coatue','cik':'0001135730','people':'Philippe Laffont','news':['Coatue Management']},
    {'id':'viking','name':'Viking','name_zh':'维京','cik':'0001103804','people':'Andreas Halvorsen','news':['Viking Global Investors']},
    {'id':'lonepine','name':'Lone Pine','name_zh':'Lone Pine','cik':'0001061165','people':'Steve Mandel','news':['Lone Pine Capital']},
    {'id':'thirdpoint','name':'Third Point','name_zh':'第三点','cik':'0001040273','people':'Daniel Loeb','news':['Third Point Daniel Loeb']},
    {'id':'appaloosa','name':'Appaloosa','name_zh':'Appaloosa','cik':'0001656456','people':'David Tepper','news':['Appaloosa David Tepper']},
    {'id':'soros','name':'Soros Fund','name_zh':'索罗斯','cik':'0001029160','people':'George Soros','news':['Soros Fund Management']},
    {'id':'duquesne','name':'Duquesne','name_zh':'Duquesne','cik':'0001536411','people':'Stanley Druckenmiller','news':['Duquesne Druckenmiller','Stanley Druckenmiller']},
    {'id':'scion','name':'Scion (Burry)','name_zh':'Scion / 伯里','cik':'0001649339','people':'Michael Burry','news':['Michael Burry','Scion Asset Management']},
    {'id':'baupost','name':'Baupost','name_zh':'Baupost','cik':'0001061768','people':'Seth Klarman','news':['Baupost Seth Klarman']},
]

# ticker universe for judgments. GOOG rolls into GOOGL.
TICKERS = {
    'AAPL':  {'cusips':['037833100'], 'names':['APPLE INC'], 'aliases':['Apple','AAPL'], 'kind':'m7'},
    'MSFT':  {'cusips':['594918104'], 'names':['MICROSOFT CORP','MICROSOFT'], 'aliases':['Microsoft','MSFT'], 'kind':'m7'},
    'NVDA':  {'cusips':['67066G104'], 'names':['NVIDIA CORP','NVIDIA'], 'aliases':['Nvidia','NVIDIA','NVDA'], 'kind':'m7'},
    'AMZN':  {'cusips':['023135106'], 'names':['AMAZON COM INC','AMAZON.COM','AMAZON COM'], 'aliases':['Amazon','AMZN'], 'kind':'m7'},
    'GOOGL': {'cusips':['02079K305','02079K107'], 'names':['ALPHABET INC'], 'aliases':['Alphabet','Google','GOOGL','GOOG'], 'kind':'m7'},
    'META':  {'cusips':['30303M102'], 'names':['META PLATFORMS','FACEBOOK'], 'aliases':['Meta','Facebook','META'], 'kind':'m7'},
    'TSLA':  {'cusips':['88160R101'], 'names':['TESLA INC','TESLA MOTORS'], 'aliases':['Tesla','TSLA'], 'kind':'m7'},
    'QQQ':   {'cusips':['46090E103'], 'names':['INVESCO QQQ','POWERSHARES QQQ','INVESCO QQQ TRUST'], 'aliases':['QQQ','Nasdaq 100','Nasdaq-100'], 'kind':'idx'},
    'SPY':   {'cusips':['78462F103','464287200','922908363'], 'names':['SPDR S&P 500','ISHARES CORE S&P 500','VANGUARD S&P 500','VANGUARD INDEX FDS S&P'], 'aliases':['SPY','S&P 500','S&P500','SPX','IVV','VOO'], 'kind':'idx'},
}
CUSIP_TO = {}
for _t, _m in TICKERS.items():
    for c in _m['cusips']:
        CUSIP_TO[c] = _t
# IVV / VOO map to SPY bucket
CUSIP_TO['464287200'] = 'SPY'
CUSIP_TO['922908363'] = 'SPY'

LOCK = threading.Lock()
STATE = {
    'filings': {},      # cik -> parsed pack
    'news': {'items':[], 'fetched':0, 'err':None},
    'payload': None,
    'payload_t': 0,
    'status': 'idle',
    'sec_ok': None,
    'source_note': '',
}

def _now_bj():
    return datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')

def _http(url, ua=WEB_UA, timeout=30, accept='*/*'):
    req = urllib.request.Request(url, headers={
        'User-Agent': ua,
        'Accept': accept,
        'Accept-Encoding': 'identity',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.geturl(), r.read()

def _http_json(url, **kw):
    code, final, body = _http(url, **kw)
    return json.loads(body.decode('utf-8', 'replace'))

def _sleep(sec):
    time.sleep(sec)

def _parse_mdy(s):
    s = (s or '').strip()
    for fmt in ('%m/%d/%Y','%Y-%m-%d','%m/%d/%y'):
        try:
            return datetime.strptime(s, fmt).date().isoformat()
        except Exception:
            pass
    return s or None

def _quarter_of(iso):
    if not iso or len(iso)<7: return None
    y,m = int(iso[:4]), int(iso[5:7])
    return f'{y}-Q{(m-1)//3+1}'

def map_ticker(cusip, name, sym):
    c = (cusip or '').strip().upper()
    if c in CUSIP_TO:
        return CUSIP_TO[c]
    sy = (sym or '').strip().upper()
    if sy == 'GOOG':
        return 'GOOGL'
    if sy in TICKERS:
        return sy
    nm = (name or '').upper()
    for t, m in TICKERS.items():
        for n in m['names']:
            if n and n in nm:
                return t
    return None

def parse_holdings_rows(rows):
    out = []
    for row in rows or []:
        if not row or len(row)<4:
            continue
        # [sym, issuer, class, cusip, value_000, pct, shares, principal, option]
        sym = row[0] if len(row)>0 else None
        issuer = row[1] if len(row)>1 else ''
        cls = row[2] if len(row)>2 else ''
        cusip = str(row[3] or '')
        val = row[4] if len(row)>4 else 0
        pct = row[5] if len(row)>5 else None
        sh = row[6] if len(row)>6 else 0
        prn = row[7] if len(row)>7 else None
        opt = row[8] if len(row)>8 else None
        try: val = int(val or 0)
        except Exception:
            try: val = int(float(val or 0))
            except Exception: val = 0
        try: sh = int(sh or 0)
        except Exception:
            try: sh = int(float(sh or 0))
            except Exception: sh = 0
        try: pct = float(pct) if pct is not None else None
        except Exception: pct = None
        opt_s = None
        if opt:
            o = str(opt).strip().lower()
            if o.startswith('put'): opt_s = 'Put'
            elif o.startswith('call'): opt_s = 'Call'
        rec = {
            'sym': (str(sym).strip().upper() if sym else '') or None,
            'name': issuer or '',
            'cls': cls or '',
            'cusip': cusip,
            'value_k': val,          # $000 as reported
            'value': val * 1000,     # USD
            'pct': pct,
            'shares': sh,
            'principal': prn,
            'opt': opt_s,
            'ticker': map_ticker(cusip, issuer, sym),
        }
        out.append(rec)
    return out

def _parse_filing_meta(html):
    meta = {}
    for dt, dd in re.findall(r'<dt[^>]*>\s*(.*?)\s*</dt>\s*<dd[^>]*>\s*(.*?)\s*</dd>', html or '', re.S|re.I):
        k = re.sub(r'<[^>]+>','', dt)
        v = re.sub(r'<[^>]+>','', dd)
        k = re.sub(r'\s+',' ', unescape(k)).strip()
        v = re.sub(r'\s+',' ', unescape(v)).strip()
        meta[k] = v
    # SEC link
    m = re.search(r'href="(https://www\.sec\.gov/Archives/edgar/data/[^"]+)"', html or '')
    if m: meta['sec_url'] = m.group(1)
    asof = _parse_mdy(meta.get('Holdings as of') or meta.get('Report date') or '')
    filed = _parse_mdy(meta.get('Date filed') or '')
    form = meta.get('Form type') or '13F-HR'
    # value string like $73.4B kept as display; also parse roughly
    return {
        'asof': asof,
        'filed': filed,
        'form': form,
        'value_disp': meta.get('Value'),
        'n_disp': meta.get('Num holdings'),
        'sec_url': meta.get('sec_url'),
        'raw': {k: meta[k] for k in meta if k not in ()},
    }

def _parse_manager_filings(html, cik, slug=None):
    """Return list of {acc, year, q, url, restatement} unique by (year,q), latest period first.
    Only keep links whose path belongs to this manager (cik or slug), so related entities are not mixed in.
    """
    items = []
    for href in re.findall(r'href="(/13f/(\d{15,20})-([^"]*?)-q([1-4])-(\d{4})(?:-restatement)?)"', html or '', re.I):
        path, acc, path_slug, q, year = href
        if slug and path_slug.lower() != slug.lower() and cik not in path:
            continue
        items.append({
            'acc': acc,
            'q': int(q),
            'year': int(year),
            'period': f'{year}-Q{q}',
            'path': path,
            'restatement': 'restatement' in path.lower(),
        })
    items.sort(key=lambda it: (it['year'], it['q'], it['restatement']), reverse=True)
    seen = set(); out = []
    for it in items:
        if it['period'] in seen: continue
        seen.add(it['period'])
        out.append(it)
    return out

def _parse_13dg(html):
    rows = []
    # links to 13d/g pages or names + dates if present
    for m in re.finditer(r'href="(/13d/[^"]+|/13g/[^"]+|/13dg/[^"]+)"[^>]*>\s*([^<]+)', html or '', re.I):
        rows.append({'url':'https://13f.info'+m.group(1), 'title': unescape(m.group(2)).strip()})
    # also list section items with dates
    for m in re.finditer(r'(13[DG](?:/A)?)\s+(?:on|filed)?\s*([^<\n]{0,80})', html or '', re.I):
        pass
    return rows[:40]

def try_sec_submissions(cik):
    url = f'https://data.sec.gov/submissions/CIK{cik}.json'
    try:
        code, final, body = _http(url, ua=SEC_UA, timeout=20, accept='application/json')
        if code != 200: return None
        return json.loads(body.decode('utf-8','replace'))
    except Exception as e:
        return {'_err': str(e)}

def fetch_fund_13finfo(fund):
    cik = fund['cik']
    page_url = f'https://13f.info/manager/{cik}'
    code, final, body = _http(page_url, timeout=25)
    html = body.decode('utf-8','replace')
    slug=None
    if cik+'-' in (final or ''):
        slug = final.rsplit(cik+'-',1)[-1].strip('/')
    filings = _parse_manager_filings(html, cik, slug=slug)
    dg = _parse_13dg(html)
    pack = {
        'id': fund['id'], 'cik': cik, 'name': fund['name'], 'name_zh': fund['name_zh'],
        'source': '13f.info (SEC 13F 公开解析)',
        'manager_url': final,
        'filings': [],
        'dg': dg,
        'fetched': _now_bj(),
    }
    # take latest 2 periods
    use = filings[:2]
    for it in use:
        _sleep(WEB_SLEEP)
        furl = 'https://13f.info' + it['path']
        try:
            _, _, fb = _http(furl, timeout=25)
            fhtml = fb.decode('utf-8','replace')
            meta = _parse_filing_meta(fhtml)
        except Exception as e:
            meta = {'err': str(e)}
        _sleep(WEB_SLEEP)
        data_url = f"https://13f.info/data/13f/{it['acc']}"
        try:
            payload = _http_json(data_url, timeout=60, accept='application/json')
            rows = payload.get('data') if isinstance(payload, dict) else payload
            holdings = parse_holdings_rows(rows)
        except Exception as e:
            holdings = []
            meta['data_err'] = str(e)
        total_k = sum(h['value_k'] for h in holdings)
        rec = {
            'acc': it['acc'],
            'period': it['period'],
            'year': it['year'], 'q': it['q'],
            'asof': meta.get('asof'),
            'filed': meta.get('filed'),
            'form': meta.get('form') or '13F-HR',
            'sec_url': meta.get('sec_url') or f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{it['acc']}/{it['acc'][:10]}-{it['acc'][10:12]}-{it['acc'][12:]}-index.html" if False else meta.get('sec_url'),
            'info_url': furl,
            'n': len(holdings),
            'total_value': total_k * 1000,
            'total_k': total_k,
            'value_disp': meta.get('value_disp'),
            'holdings': holdings,
        }
        # build a nicer sec url from acc
        if not rec['sec_url']:
            acc_dash = it['acc']
            if len(acc_dash)>=18 and '-' not in acc_dash:
                acc_dash = f"{it['acc'][:-8]}-{it['acc'][-8:-6]}-{it['acc'][-6:]}"
                # actually acc from 13f.info is nodash 18 digits: 000103738926000062
                # dash form: 0001037389-26-000062
            if re.fullmatch(r'\d{18}', it['acc']):
                acc_dash = f"{it['acc'][:10]}-{it['acc'][10:12]}-{it['acc'][12:]}"
                rec['sec_url'] = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{it['acc']}/{acc_dash}-index.htm"
        pack['filings'].append(rec)
    return pack

def load_cache(cik):
    p = os.path.join(DAT, f'{cik}.json')
    if os.path.isfile(p):
        try:
            return json.load(open(p))
        except Exception:
            return None
    return None

def save_cache(cik, pack):
    p = os.path.join(DAT, f'{cik}.json')
    tmp = p + '.tmp'
    with open(tmp,'w') as f:
        json.dump(pack, f, ensure_ascii=False, separators=(',',':'))
    os.replace(tmp, p)

def cache_age(cik):
    p = os.path.join(DAT, f'{cik}.json')
    if not os.path.isfile(p): return 1e18
    return time.time() - os.path.getmtime(p)

def refresh_filings(force=False):
    with LOCK:
        STATE['status'] = 'fetching_13f'
    # probe SEC once
    sec_probe = try_sec_submissions('0001037389')
    sec_ok = isinstance(sec_probe, dict) and 'filings' in sec_probe
    with LOCK:
        STATE['sec_ok'] = bool(sec_ok)
        if sec_ok:
            STATE['source_note'] = '持仓来自 SEC EDGAR 13F-HR 官方接口。'
        else:
            STATE['source_note'] = 'SEC EDGAR 官方接口当前从本服务器被拒绝（Akamai 拦截机房 IP）。持仓改用 13f.info 对同一份 13F 申报的公开解析，并附上 sec.gov 原件链接。'
    errors = []
    for fund in FUNDS:
        cik = fund['cik']
        if not force and cache_age(cik) < FILING_TTL:
            pack = load_cache(cik)
            if pack:
                with LOCK:
                    STATE['filings'][cik] = pack
                continue
        try:
            pack = fetch_fund_13finfo(fund)
            save_cache(cik, pack)
            with LOCK:
                STATE['filings'][cik] = pack
        except Exception as e:
            errors.append(f"{fund['name']}: {e}")
            pack = load_cache(cik)
            if pack:
                pack['stale'] = True
                pack['fetch_err'] = str(e)
                with LOCK:
                    STATE['filings'][cik] = pack
        _sleep(WEB_SLEEP)
    with LOCK:
        STATE['status'] = 'filings_ready'
        STATE['filing_errors'] = errors
        STATE['filings_fetched'] = _now_bj()
    return errors

# ---------- news ----------
TAG_RULES = [
    ('看空', r'\b(bearish|short(s|ing)?|bets? against|puts? on|downside|bubble|crash|sell-off|overvalued|increases? short)\b|看空|做空'),
    ('减持', r'\b(cuts?|trim(s|med)?|reduces?|sells? off|exits?|dumps?|slashes?|unloads?|lightens?|closes? (a |the )?position)\b|减持|减仓|清仓'),
    ('增持', r'\b(adds? to|increases?(d)?(?!\s+short)|boosts?|accumulat\w+|raises? stake|buys? more|new position|initiates? a? ?position|tops? up)\b|增持|加仓'),
    ('看多', r'\b(bullish|goes? long|upside|undervalued|rally|buy the dip|overweight)\b|看多|做多'),
    ('维权', r'\b(activist|13[dD]|proxy fight|campaign|board seat)\b|维权|激进股东'),
    ('宏观', r'\b(Fed|FOMC|inflation|interest rate|recession|GDP|treasury|macro)\b|宏观|美联储|通胀'),
    ('监管', r'\b(SEC|lawsuit|probe|investigation|fraud|charge[sd]?)\b|监管|诉讼|调查'),
]

def tag_headline(title):
    t = title or ''
    for lab, pat in TAG_RULES:
        if re.search(pat, t, re.I):
            return lab
    return '其他'

def _rss_items(url):
    code, final, body = _http(url, timeout=20, accept='application/rss+xml,application/xml,text/xml,*/*')
    root = ET.fromstring(body)
    items = []
    # RSS 2.0
    for it in root.findall('.//item'):
        def tx(tag):
            e = it.find(tag)
            return (e.text or '').strip() if e is not None and e.text else ''
        title = unescape(tx('title'))
        link = tx('link')
        src = ''
        src_e = it.find('source')
        if src_e is not None:
            src = (src_e.text or src_e.get('url') or '').strip()
        # google news often: title = "Headline - Source"
        pub = tx('pubDate')
        ts = None
        if pub:
            try:
                ts = parsedate_to_datetime(pub)
                if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                ts = None
        items.append({'title': title, 'link': link, 'source': src, 'pub': pub, 'ts': ts})
    return items

def _gnews(q):
    qs = urllib.parse.urlencode({'q': q, 'hl':'en-US','gl':'US','ceid':'US:en'})
    return _rss_items('https://news.google.com/rss/search?'+qs)

def _related_tickers(title):
    hit = set()
    t = title or ''
    low = t.lower()
    for sym, m in TICKERS.items():
        if re.search(r'\b'+re.escape(sym)+r'\b', t):
            hit.add(sym)
        for a in m['aliases']:
            if len(a)<=3:
                if re.search(r'\b'+re.escape(a)+r'\b', t):
                    hit.add(sym)
            elif a.lower() in low:
                hit.add(sym)
    return sorted(hit)

def _related_funds(title):
    hit = []
    t = (title or '').lower()
    for f in FUNDS:
        keys = [f['name'], f['people']] + f.get('news', [])
        for k in keys:
            if k and k.lower() in t:
                hit.append(f['id'])
                break
    return hit

def refresh_news(force=False):
    with LOCK:
        if not force and STATE['news']['fetched'] and time.time()-STATE['news']['fetched'] < NEWS_TTL:
            return
        STATE['status'] = 'fetching_news'
    items = []
    queries = []
    for f in FUNDS:
        queries.append((f['news'][0], f['id'], None))
    for sym, m in TICKERS.items():
        # one query per ticker
        q = m['aliases'][0] + ' stock' if m['kind']=='m7' else (sym if sym!='SPY' else 'S&P 500 hedge fund')
        queries.append((q, None, sym))
    # a few general
    queries += [('hedge fund Mag 7', None, None), ('hedge fund 13F', None, None)]
    seen = set()
    err = None
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    for q, fid, sym in queries:
        try:
            rows = _gnews(q)
            _sleep(0.15)
        except Exception as e:
            err = str(e)
            continue
        for it in rows:
            title = it['title'] or ''
            key = re.sub(r'\s+',' ', title.lower())[:90]
            if not title or key in seen:
                continue
            ts = it['ts']
            if ts and ts < cutoff:
                continue
            seen.add(key)
            # split google "headline - Source"
            src = it['source']
            disp = title
            if ' - ' in title and not src:
                disp, src = title.rsplit(' - ', 1)
            funds = _related_funds(title) or ([fid] if fid else [])
            ticks = _related_tickers(title)
            if sym and sym not in ticks:
                # keep if query was ticker-specific
                ticks = ticks + [sym]
            bj = ''
            if ts:
                bj = ts.astimezone(BJ).strftime('%Y-%m-%d %H:%M')
            items.append({
                'title': disp,
                'title_raw': title,
                'link': it['link'],
                'source': src or 'Google News',
                'ts': ts.timestamp() if ts else 0,
                'time_bj': bj,
                'tag': tag_headline(title),
                'funds': funds,
                'tickers': ticks,
            })
    # CNBC top news, keep if related
    try:
        for it in _rss_items('https://www.cnbc.com/id/100003114/device/rss/rss.html'):
            title = it['title'] or ''
            key = re.sub(r'\s+',' ', title.lower())[:90]
            if not title or key in seen: continue
            ts = it['ts']
            if ts and ts < cutoff: continue
            funds = _related_funds(title)
            ticks = _related_tickers(title)
            if not funds and not ticks:
                continue
            seen.add(key)
            bj = ts.astimezone(BJ).strftime('%Y-%m-%d %H:%M') if ts else ''
            items.append({
                'title': title, 'title_raw': title, 'link': it['link'],
                'source': 'CNBC', 'ts': ts.timestamp() if ts else 0,
                'time_bj': bj, 'tag': tag_headline(title),
                'funds': funds, 'tickers': ticks,
            })
    except Exception as e:
        err = (err or '') + f' cnbc:{e}'
    items.sort(key=lambda x: x['ts'], reverse=True)
    # cap
    items = items[:250]
    path = os.path.join(DAT, 'news.json')
    with open(path,'w') as f:
        json.dump({'fetched': _now_bj(), 'items': items}, f, ensure_ascii=False)
    with LOCK:
        STATE['news'] = {'items': items, 'fetched': time.time(), 'fetched_bj': _now_bj(), 'err': err}
        STATE['status'] = 'ready'

def _load_news_disk():
    p = os.path.join(DAT,'news.json')
    if os.path.isfile(p):
        try:
            d = json.load(open(p))
            with LOCK:
                if not STATE['news']['items']:
                    STATE['news'] = {'items': d.get('items') or [], 'fetched': os.path.getmtime(p), 'fetched_bj': d.get('fetched'), 'err': None}
        except Exception:
            pass

# ---------- aggregation / score ----------
FORMULA_ZH = (
    '评分范围为 -100 到 100，由仓位与新闻两部分相加后截断。'
    '仓位全部来自 13F，是季度持仓、约 45 天滞后，不是实时交易。'
    '① 广度（最多 ±40）：对该标的普通股（指数含相关 ETF）增持的基金数减去减持的基金数，再除以有变动的基金数。'
    '② 金额（最多 ±30）：各基金该标的市值变动之和，除以变动绝对值之和。'
    '③ 期权（指数最多 ±20，个股最多 ±10）：当期 Call 市值减 Put 市值，再除以两者之和；13F 的 Put/Call 行是公开申报的期权仓，不是逐笔成交。'
    '④ 新闻（最多 ±20）：近 7 天标题里，规则标签为「增持/看多」记多、「减持/看空」记空，按条数轧差。标题为英文原文，中文标签只按关键词规则生成，不是模型摘要。'
    '≥25 偏多，≤−25 偏空，其余中性。样本少于 3 家基金会在卡片上标明「样本偏少」。'
)

def _equity_only(h):
    return not h.get('opt')

def _index_h(h, sym):
    return h.get('ticker')==sym

def fund_summary(pack):
    fils = sorted(pack.get('filings') or [], key=lambda f: (f.get('year') or 0, f.get('q') or 0), reverse=True)
    cur = fils[0] if fils else None
    prev = fils[1] if len(fils)>1 else None
    def bag(fil, equity=True):
        d = {}
        if not fil: return d
        for h in fil.get('holdings') or []:
            key = (h.get('ticker') or h.get('cusip') or h.get('name'), h.get('opt') or 'EQ')
            if key not in d:
                d[key] = {'shares':0,'value':0,'name':h.get('name'),'sym':h.get('sym'),'ticker':h.get('ticker'),'opt':h.get('opt'),'pct':h.get('pct') or 0}
            d[key]['shares'] += h.get('shares') or 0
            d[key]['value'] += h.get('value') or 0
            d[key]['pct'] += h.get('pct') or 0
        return d
    b0, b1 = bag(cur), bag(prev)
    # top holdings equity
    tops = []
    if cur:
        eq = [h for h in cur['holdings'] if not h.get('opt')]
        eq.sort(key=lambda x: x['value'], reverse=True)
        # merge same ticker
        merged = {}
        for h in eq:
            k = h.get('ticker') or h.get('cusip') or h.get('name')
            if k not in merged:
                merged[k] = {'sym': h.get('ticker') or h.get('sym') or '', 'name': h['name'], 'value':0, 'shares':0, 'pct':0}
            merged[k]['value'] += h['value']
            merged[k]['shares'] += h['shares']
            merged[k]['pct'] += h.get('pct') or 0
        tops = sorted(merged.values(), key=lambda x: x['value'], reverse=True)[:8]
    # mag7 / index exposure
    expo = {}
    for t in TICKERS:
        def side(bag, opt=None):
            s=v=0
            for (k,o), rec in bag.items():
                if rec.get('ticker')!=t: continue
                if opt is None and rec.get('opt'): continue
                if opt and rec.get('opt')!=opt: continue
                s += rec['shares']; v += rec['value']
            return s,v
        s0,v0 = side(b0); s1,v1 = side(b1)
        c0,cv0 = side(b0,'Call'); c1,cv1 = side(b1,'Call')
        p0,pv0 = side(b0,'Put'); p1,pv1 = side(b1,'Put')
        if not (v0 or v1 or cv0 or pv0 or cv1 or pv1):
            continue
        chg_s = s0-s1 if (prev and (s0 or s1)) else None
        # action
        act = '无'
        if prev:
            if v0 and not v1 and s0: act = '新建'
            elif v1 and not v0: act = '清仓'
            elif chg_s is not None and chg_s>0: act = '增持'
            elif chg_s is not None and chg_s<0: act = '减持'
            else: act = '持平'
        else:
            act = '持有' if v0 else '无'
        expo[t] = {
            'shares': s0, 'value': v0, 'prev_shares': s1, 'prev_value': v1,
            'd_shares': (s0-s1) if prev else None, 'd_value': (v0-v1) if prev else None,
            'pct': (v0 / cur['total_value'] * 100) if cur and cur.get('total_value') else None,
            'call_v': cv0, 'put_v': pv0, 'd_call': (cv0-cv1) if prev else None, 'd_put': (pv0-pv1) if prev else None,
            'act': act,
        }
    changes = []
    if prev:
        keys = set(b0)|set(b1)
        for k in keys:
            r0, r1 = b0.get(k), b1.get(k)
            v0 = (r0 or {}).get('value') or 0
            v1 = (r1 or {}).get('value') or 0
            s0 = (r0 or {}).get('shares') or 0
            s1 = (r1 or {}).get('shares') or 0
            dv, ds = v0-v1, s0-s1
            if dv==0 and ds==0: continue
            rec = r0 or r1
            if rec.get('opt'):  # keep option changes for QQQ/SPY/m7 only or large
                if not rec.get('ticker') and abs(dv)<50_000_000:
                    continue
            act = '新建' if (r0 and not r1) else ('清仓' if (r1 and not r0) else ('增持' if ds>0 or dv>0 else '减持'))
            changes.append({
                'sym': rec.get('ticker') or rec.get('sym') or '',
                'name': rec.get('name'),
                'opt': rec.get('opt'),
                'act': act,
                'shares': s0, 'prev_shares': s1, 'd_shares': ds,
                'value': v0, 'prev_value': v1, 'd_value': dv,
            })
        changes.sort(key=lambda x: abs(x['d_value']), reverse=True)
    return {
        'cik': pack['cik'], 'id': pack['id'], 'name': pack['name'], 'name_zh': pack.get('name_zh'),
        'source': pack.get('source'),
        'manager_url': pack.get('manager_url'),
        'asof': (cur or {}).get('asof'),
        'filed': (cur or {}).get('filed'),
        'period': (cur or {}).get('period'),
        'prev_period': (prev or {}).get('period'),
        'prev_asof': (prev or {}).get('asof'),
        'form': (cur or {}).get('form'),
        'sec_url': (cur or {}).get('sec_url'),
        'info_url': (cur or {}).get('info_url'),
        'n': (cur or {}).get('n') or 0,
        'total_value': (cur or {}).get('total_value') or 0,
        'prev_total': (prev or {}).get('total_value') or 0,
        'top': tops,
        'expo': expo,
        'changes': changes[:40],
        'n_changes': len(changes),
        'stale': pack.get('stale'),
        'fetch_err': pack.get('fetch_err'),
        'dg': (pack.get('dg') or [])[:8],
    }

def _news_for_ticker(items, sym):
    bull=bear=other=0
    for it in items:
        if sym not in (it.get('tickers') or []):
            # also allow index aliases already mapped
            continue
        tag = it.get('tag')
        if tag in ('增持','看多'): bull += 1
        elif tag in ('减持','看空'): bear += 1
        else: other += 1
    return bull, bear, other

def judgments(fund_rows, news_items):
    out = []
    m7 = [t for t,m in TICKERS.items() if m['kind']=='m7']
    idxs = [t for t,m in TICKERS.items() if m['kind']=='idx']
    for t in m7+idxs:
        add=cut=new=exited=hold=0
        dval=0; absdv=0; cur_v=0; prev_v=0
        call_v=put_v=0
        drivers=[]
        n_touch=0
        for fr in fund_rows:
            ex = (fr.get('expo') or {}).get(t)
            if not ex: continue
            n_touch += 1
            cur_v += ex.get('value') or 0
            prev_v += ex.get('prev_value') or 0
            call_v += ex.get('call_v') or 0
            put_v += ex.get('put_v') or 0
            act = ex.get('act')
            dv = ex.get('d_value')
            if dv is None: dv = 0
            dval += dv; absdv += abs(dv)
            if act=='新建': new += 1; add += 1
            elif act=='清仓': exited += 1; cut += 1
            elif act=='增持': add += 1
            elif act=='减持': cut += 1
            else: hold += 1
            if act in ('新建','清仓','增持','减持') or abs(dv)>=5_000_000 or (ex.get('call_v') or 0)+(ex.get('put_v') or 0)>0:
                drivers.append({
                    'fund': fr['name'], 'fund_zh': fr.get('name_zh'), 'id': fr['id'],
                    'act': act, 'd_shares': ex.get('d_shares'), 'd_value': dv,
                    'value': ex.get('value'), 'shares': ex.get('shares'),
                    'call_v': ex.get('call_v') or 0, 'put_v': ex.get('put_v') or 0,
                })
        moved = add+cut
        breadth = 40.0 * (add-cut) / moved if moved else 0.0
        flow = 30.0 * dval / absdv if absdv else 0.0
        opt_den = call_v+put_v
        opt_w = 20.0 if TICKERS[t]['kind']=='idx' else 10.0
        opt = opt_w * (call_v-put_v) / opt_den if opt_den else 0.0
        bull,bear,other = _news_for_ticker(news_items, t)
        news_n = bull+bear
        news = 20.0 * (bull-bear) / news_n if news_n else 0.0
        score = int(max(-100, min(100, round(breadth+flow+opt+news))))
        if score>=25: label, tone = '偏多', 'up'
        elif score<=-25: label, tone = '偏空', 'dn'
        else: label, tone = '中性', 'mid'
        drivers.sort(key=lambda x: abs(x.get('d_value') or 0), reverse=True)
        out.append({
            'symbol': t, 'kind': TICKERS[t]['kind'],
            'score': score, 'label': label, 'tone': tone,
            'n_funds': n_touch, 'n_add': add, 'n_cut': cut, 'n_new': new, 'n_exit': exited, 'n_hold': hold,
            'd_value': dval, 'value': cur_v, 'prev_value': prev_v,
            'call_v': call_v, 'put_v': put_v,
            'news_bull': bull, 'news_bear': bear, 'news_other': other,
            'parts': {'breadth': round(breadth,1), 'flow': round(flow,1), 'options': round(opt,1), 'news': round(news,1)},
            'thin': n_touch < 3,
            'drivers': drivers[:8],
        })
    return out

def _latest_period(fund_rows):
    periods = [fr['period'] for fr in fund_rows if fr.get('period')]
    # prefer 2026-Q3 if any
    if any(p=='2026-Q3' for p in periods):
        return '2026-Q3', '2026-09-30'
    if periods:
        return periods[0], next((fr['asof'] for fr in fund_rows if fr.get('period')==periods[0] and fr.get('asof')), None)
    return None, None

def build_payload():
    with LOCK:
        packs = list(STATE['filings'].values())
        news = list(STATE['news'].get('items') or [])
        source_note = STATE.get('source_note') or ''
        sec_ok = STATE.get('sec_ok')
        ferr = list(STATE.get('filing_errors') or [])
        filings_fetched = STATE.get('filings_fetched')
        news_bj = STATE['news'].get('fetched_bj')
    fund_rows = [fund_summary(p) for p in packs]
    fund_rows.sort(key=lambda x: x.get('total_value') or 0, reverse=True)
    jud = judgments(fund_rows, news)
    period, asof = _latest_period(fund_rows)
    n_q3 = sum(1 for fr in fund_rows if fr.get('period')=='2026-Q3')
    period_counts = {}
    for fr in fund_rows:
        period_counts[fr.get('period') or '未知'] = period_counts.get(fr.get('period') or '未知', 0) + 1
    mix_note = '各基金最新申报期可能不同：判断按该基金「自己最近两个季度」的变动汇总，不是同一报告期的截面。'
    # heatmap: funds x m7, cell = d_value and act
    m7 = ['AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA']
    heat = []
    for fr in fund_rows:
        row = {'id': fr['id'], 'name': fr['name'], 'name_zh': fr.get('name_zh'), 'period': fr.get('period'), 'cells': {}}
        for t in m7:
            ex = (fr.get('expo') or {}).get(t)
            if not ex:
                row['cells'][t] = None
            else:
                row['cells'][t] = {
                    'act': ex['act'], 'd_value': ex.get('d_value'), 'd_shares': ex.get('d_shares'),
                    'value': ex.get('value'), 'shares': ex.get('shares'),
                }
        heat.append(row)
    # recent filings list
    recent = []
    for fr in fund_rows:
        if fr.get('filed'):
            recent.append({
                'kind':'13F', 'form': fr.get('form') or '13F-HR', 'fund': fr['name'], 'fund_zh': fr.get('name_zh'),
                'id': fr['id'], 'filed': fr['filed'], 'asof': fr.get('asof'), 'period': fr.get('period'),
                'url': fr.get('sec_url') or fr.get('info_url'), 'n': fr.get('n'), 'total_value': fr.get('total_value'),
            })
        for dg in fr.get('dg') or []:
            recent.append({
                'kind':'13D/G', 'form':'13D/G', 'fund': fr['name'], 'fund_zh': fr.get('name_zh'),
                'id': fr['id'], 'filed': None, 'title': dg.get('title'), 'url': dg.get('url'),
            })
    recent.sort(key=lambda x: x.get('filed') or '', reverse=True)
    # slim funds for payload (drop huge unused)
    funds_out = []
    for fr in fund_rows:
        funds_out.append({k: fr[k] for k in fr if k!='dg'})
    return {
        'asof_bj': _now_bj(),
        'period': period,
        'report_asof': asof or ('2026-09-30' if period=='2026-Q3' else asof),
        'n_funds': len(fund_rows),
        'n_q3': n_q3,
        'period_counts': period_counts,
        'mix_note': mix_note,
        'lag_note': '13F 是季度持仓，截止日期通常为季末，法定申报期限约 45 天，不是盘中实时仓位。部分基金已披露 2026-Q3（截至 2026-09-30），尚未披露的仍显示上一季。',
        'source_note': source_note,
        'sec_direct': bool(sec_ok),
        'formula': FORMULA_ZH,
        'judgments': jud,
        'funds': funds_out,
        'heatmap': heat,
        'news': news[:120],
        'recent': recent[:40],
        'errors': ferr,
        'filings_fetched': filings_fetched,
        'news_fetched': news_bj,
    }

def payload(force=False):
    _load_news_disk()
    with LOCK:
        # load caches if memory empty
        if not STATE['filings']:
            for fund in FUNDS:
                pack = load_cache(fund['cik'])
                if pack:
                    STATE['filings'][fund['cik']] = pack
        have = len(STATE['filings'])
        news_stale = (not STATE['news']['fetched']) or (time.time()-STATE['news']['fetched']>NEWS_TTL)
        filings_stale = have < len(FUNDS) or any(cache_age(f['cik'])>FILING_TTL for f in FUNDS)
        cached = STATE['payload']
        cached_t = STATE['payload_t']
    if force or filings_stale or have==0:
        refresh_filings(force=force)
        news_stale = True
    if force or news_stale:
        try:
            refresh_news(force=True)
        except Exception as e:
            with LOCK:
                STATE['news']['err'] = str(e)
    p = build_payload()
    with LOCK:
        STATE['payload'] = p
        STATE['payload_t'] = time.time()
        STATE['status'] = 'ready'
    return p

def prewarm():
    def run():
        try:
            payload(force=False)
        except Exception as e:
            print('funds prewarm failed:', repr(e), flush=True)
        while True:
            time.sleep(30*60)
            try:
                refresh_news(force=False)
                # daily-ish 13F
                if any(cache_age(f['cik'])>FILING_TTL for f in FUNDS):
                    refresh_filings(force=False)
                with LOCK:
                    STATE['payload'] = None
            except Exception as e:
                print('funds refresh loop:', repr(e), flush=True)
    threading.Thread(target=run, daemon=True).start()

if __name__ == '__main__':
    print('refreshing filings + news...', flush=True)
    p = payload(force=True)
    print('funds', p['n_funds'], 'q3', p['n_q3'], 'period', p['period'], 'news', len(p['news']))
    for j in p['judgments']:
        print(j['symbol'], j['score'], j['label'], 'add', j['n_add'], 'cut', j['n_cut'], 'dV', j['d_value'])
