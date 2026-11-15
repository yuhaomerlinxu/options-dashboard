# -*- coding: utf-8 -*-
"""Hedge-fund news radar. Daily headlines about top US funds and managers,
tagged by Mag7 / QQQ / SPX and 看多/看空/中性. No 13F or other lagging sources."""
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
try:
    from zoneinfo import ZoneInfo
    ETZ = ZoneInfo('America/New_York')
except Exception:
    ETZ = timezone(timedelta(hours=-5))

WEB_UA = 'OptionsDashboard/1.0 (https://options-dashboard-pp77.onrender.com; yuhaomerlinxu@users.noreply.github.com)'
WEB_SLEEP = 0.15
NEWS_TTL = 20 * 60  # refresh about every 20 minutes

FUNDS = [
    {'id':'bridgewater','name':'Bridgewater','name_zh':'桥水','people':'Ray Dalio','news':['Bridgewater Associates','Ray Dalio']},
    {'id':'citadel','name':'Citadel Advisors','name_zh':'城堡','people':'Ken Griffin','news':['Citadel Ken Griffin','Citadel Advisors']},
    {'id':'millennium','name':'Millennium','name_zh':'千禧年','people':'Israel Englander','news':['Millennium Management hedge fund','Israel Englander']},
    {'id':'point72','name':'Point72','name_zh':'Point72','people':'Steve Cohen','news':['Point72 Steve Cohen']},
    {'id':'deshaw','name':'D.E. Shaw','name_zh':'D.E. Shaw','people':'David Shaw','news':['D.E. Shaw','DE Shaw']},
    {'id':'renaissance','name':'Renaissance','name_zh':'文艺复兴','people':'Renaissance Technologies','news':['Renaissance Technologies']},
    {'id':'twosigma','name':'Two Sigma','name_zh':'Two Sigma','people':'Two Sigma','news':['Two Sigma Investments']},
    {'id':'elliott','name':'Elliott','name_zh':'艾略特','people':'Paul Singer','news':['Elliott Management','Paul Singer Elliott']},
    {'id':'pershing','name':'Pershing Square','name_zh':'潘兴广场','people':'Bill Ackman','news':['Pershing Square','Bill Ackman']},
    {'id':'tiger','name':'Tiger Global','name_zh':'老虎全球','people':'Chase Coleman','news':['Tiger Global','Chase Coleman']},
    {'id':'coatue','name':'Coatue','name_zh':'Coatue','people':'Philippe Laffont','news':['Coatue Management']},
    {'id':'viking','name':'Viking','name_zh':'维京','people':'Andreas Halvorsen','news':['Viking Global Investors']},
    {'id':'lonepine','name':'Lone Pine','name_zh':'Lone Pine','people':'Steve Mandel','news':['Lone Pine Capital']},
    {'id':'thirdpoint','name':'Third Point','name_zh':'第三点','people':'Daniel Loeb','news':['Third Point Daniel Loeb']},
    {'id':'appaloosa','name':'Appaloosa','name_zh':'Appaloosa','people':'David Tepper','news':['Appaloosa David Tepper']},
    {'id':'soros','name':'Soros Fund','name_zh':'索罗斯','people':'George Soros','news':['Soros Fund Management']},
    {'id':'duquesne','name':'Duquesne','name_zh':'Duquesne','people':'Stanley Druckenmiller','news':['Duquesne Druckenmiller','Stanley Druckenmiller']},
    {'id':'scion','name':'Scion (Burry)','name_zh':'Scion / 伯里','people':'Michael Burry','news':['Michael Burry','Scion Asset Management']},
    {'id':'baupost','name':'Baupost','name_zh':'Baupost','people':'Seth Klarman','news':['Baupost Seth Klarman']},
]
FUND_BY_ID = {f['id']: f for f in FUNDS}

TICKERS = {
    'AAPL':  {'aliases':['Apple','AAPL'], 'kind':'m7'},
    'MSFT':  {'aliases':['Microsoft','MSFT'], 'kind':'m7'},
    'NVDA':  {'aliases':['Nvidia','NVIDIA','NVDA'], 'kind':'m7'},
    'AMZN':  {'aliases':['Amazon','AMZN'], 'kind':'m7'},
    'GOOGL': {'aliases':['Alphabet','Google','GOOGL','GOOG'], 'kind':'m7'},
    'META':  {'aliases':['Meta','Facebook','META'], 'kind':'m7'},
    'TSLA':  {'aliases':['Tesla','TSLA'], 'kind':'m7'},
    'QQQ':   {'aliases':['QQQ','Nasdaq 100','Nasdaq-100','Nasdaq100','NDX'], 'kind':'idx'},
    'SPX':   {'aliases':['SPX','SPY','S&P 500','S&P500','S&P 500 Index','IVV','VOO'], 'kind':'idx'},
}
ORDER = ['AAPL','MSFT','NVDA','AMZN','GOOGL','META','TSLA','QQQ','SPX']

LOCK = threading.Lock()
FETCH_LOCK = threading.Lock()
STATE = {
    'news': {'items':[], 'fetched':0, 'fetched_bj':None, 'err':None},
    'payload': None,
    'payload_t': 0,
    'status': 'idle',
}

FORMULA_ZH = (
    '近 7 天、标题里提到该标的的新闻。用关键词规则把标题标成「看多」「看空」或「中性」：'
    '增持、加仓、看涨、做多等记看多；减持、做空、看跌等记看空；对不上的记中性。'
    '得分 = 100 × (看多条数 − 看空条数) ÷ (看多 + 看空)，限制在 −100 到 100。'
    '≥25 为偏多，≤−25 为偏空，否则中性。中性条数不进入分母。'
    '看多与看空合计不足 3 条时标注「样本偏少」。'
    '标题是英文原文，中文标签只按规则生成，不是模型摘要。判断只来自新闻情绪，不是实时仓位。'
)

def _now_bj():
    return datetime.now(BJ).strftime('%Y-%m-%d %H:%M:%S')

def _http(url, ua=WEB_UA, timeout=25, accept='*/*'):
    req = urllib.request.Request(url, headers={
        'User-Agent': ua,
        'Accept': accept,
        'Accept-Encoding': 'identity',
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.getcode(), r.geturl(), r.read()

# 看空 first, so “increases short” is not tagged 看多.
# Do not use a bare “short” token — it matches “short-term”.
# Position verbs (cut/trim/sell) require a holding-like noun to avoid “Fed cuts rates”.
TAG_RULES = [
    ('看空', r'\b(bearish|shorted|shorting|shorts|short(?:s|ing)?\s+(?:position|positions|interest|bet|bets|the|against|seller)|'
            r'bets? against|puts? on|puts|put options?|downside|bubble|crash|sell-?off|overvalued|increases? short|goes? short)\b|看空|做空|'
            r'\b(cuts?|trim(s|med)?|reduces?|exits?|dumps?|slashes?|unloads?|lightens?|sells? off|closes?|cut|reduced)\b'
            r'.{0,48}\b(stake|position|holding|holdings|shares?|exposure|longs?)\b'),
    ('看多', r'\b(bullish|goes? long|upside|undervalued|rally|buy the dip|overweight|'
            r'initiates? a? ?position|new position|sees value|staying bullish|remains bullish|'
            r'really,? really strong)\b|看多|做多|'
            r'\b(adds? to|increases?(d)?(?!\s+short)|boosts?|accumulat\w+|raises? stake|'
            r'buys? more|tops? up|shares added)\b|'
            r'\b(added)\b.{0,24}\b(shares?|stake|position)\b'),
]

def tag_text(text):
    t = text or ''
    for lab, pat in TAG_RULES:
        if re.search(pat, t, re.I):
            return lab
    return '中性'

def _ticker_spans(title):
    hits = []
    for sym, m in TICKERS.items():
        for a in m['aliases']:
            # ticker symbols are case-sensitive so “spy issue” does not become SPY
            flags = 0 if (len(a) <= 5 and a.isupper()) else re.I
            for mo in re.finditer(r'(?<![A-Za-z0-9])'+re.escape(a)+r'(?![A-Za-z0-9])', title or '', flags):
                hits.append((sym, mo.start(), mo.end()))
    return hits

def _related_tickers(title):
    seen = set()
    out = []
    for sym, _, _ in sorted(_ticker_spans(title), key=lambda x: x[1]):
        if sym not in seen:
            seen.add(sym)
            out.append(sym)
    return [s for s in ORDER if s in seen]

def tag_headline(title):
    """Overall tag plus per-ticker tags for mixed headlines (e.g. boosts Intel, cuts Apple)."""
    title = title or ''
    spans = _ticker_spans(title)
    tickers = []
    seen = set()
    for sym, _, _ in sorted(spans, key=lambda x: x[1]):
        if sym not in seen:
            seen.add(sym)
            tickers.append(sym)
    overall = tag_text(title)
    per = {}
    if len(tickers) == 1:
        per[tickers[0]] = overall
        return overall, [s for s in ORDER if s in per], per
    if not tickers:
        return overall, [], per
    n = len(title)
    for sym, s, e in spans:
        # clause window: stop at comma / semicolon so “boosts Intel, cuts Apple” splits correctly
        left = s
        while left > 0 and title[left-1] not in ',;.—':
            left -= 1
            if s - left > 90:
                break
        right = e
        while right < n and title[right] not in ',;.—':
            right += 1
            if right - e > 90:
                break
        lab = tag_text(title[left:right])
        prev = per.get(sym)
        if prev is None or (prev == '中性' and lab != '中性'):
            per[sym] = lab
        elif lab != '中性':
            per[sym] = lab
    dirs = {v for v in per.values() if v in ('看多', '看空')}
    if len(dirs) == 1:
        overall = dirs.pop()
    elif len(dirs) > 1:
        overall = '中性'
    return overall, [s for s in ORDER if s in per], per

def _rss_items(url):
    code, final, body = _http(url, timeout=20, accept='application/rss+xml,application/xml,text/xml,*/*')
    root = ET.fromstring(body)
    items = []
    for it in root.findall('.//item'):
        def tx(tag):
            e = it.find(tag)
            return (e.text or '').strip() if e is not None and e.text else ''
        title = unescape(re.sub(r'<[^>]+>', '', tx('title')))
        link = tx('link')
        src = ''
        src_e = it.find('source')
        if src_e is not None:
            src = (src_e.text or src_e.get('url') or '').strip()
        pub = tx('pubDate')
        ts = None
        if pub:
            try:
                ts = parsedate_to_datetime(pub)
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
            except Exception:
                ts = None
        items.append({'title': title, 'link': link, 'source': src, 'pub': pub, 'ts': ts})
    return items

def _gnews(q):
    qs = urllib.parse.urlencode({'q': q, 'hl':'en-US','gl':'US','ceid':'US:en'})
    return _rss_items('https://news.google.com/rss/search?'+qs)

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

def _queries():
    qs = []
    for f in FUNDS:
        for q in f['news'][:2]:
            qs.append(q)
    # public remarks / letters from the more vocal managers
    for q in [
        'Bill Ackman interview OR "investor letter" OR tweet',
        'Michael Burry interview OR tweet OR "on X"',
        'Stanley Druckenmiller interview',
        'Ray Dalio interview OR letter',
        'David Tepper interview',
        'Ken Griffin interview',
    ]:
        qs.append(q)
    for sym, m in TICKERS.items():
        name = m['aliases'][0]
        qs.append(f'{name} hedge fund')
    qs += [
        'hedge fund Magnificent Seven',
        'hedge fund Nasdaq-100',
        'S&P 500 hedge fund OR Ackman OR Burry OR Dalio OR Druckenmiller',
        'Tesla stock (Burry OR Ackman OR Tepper OR "hedge fund")',
    ]
    # unique, keep order
    out, seen = [], set()
    for q in qs:
        k = q.lower()
        if k not in seen:
            seen.add(k)
            out.append(q)
    return out

def _normalize_item(it, seen, cutoff):
    title = it['title'] or ''
    key = re.sub(r'\s+', ' ', title.lower())[:90]
    if not title or key in seen:
        return None
    ts = it['ts']
    if ts and ts < cutoff:
        return None
    src = it['source']
    disp = title
    if ' - ' in title and not src:
        disp, src = title.rsplit(' - ', 1)
    blob = title + ' ' + disp
    funds = _related_funds(blob)
    tag, ticks, ticker_tags = tag_headline(blob)
    low = blob.lower()
    # only keep stories about these funds/managers, or headlines that say “hedge fund”
    if not funds and 'hedge fund' not in low and 'hedge-fund' not in low:
        return None
    seen.add(key)
    bj = ts.astimezone(BJ).strftime('%Y-%m-%d %H:%M') if ts else ''
    return {
        'title': disp,
        'title_raw': title,
        'link': it['link'],
        'source': src or 'Google News',
        'ts': ts.timestamp() if ts else 0,
        'time_bj': bj,
        'tag': tag,
        'funds': funds,
        'tickers': ticks,
        'ticker_tags': ticker_tags,
    }

def refresh_news(force=False):
    if FETCH_LOCK.locked() and not force:
        with FETCH_LOCK:
            return
    with FETCH_LOCK:
        with LOCK:
            if not force and STATE['news']['fetched'] and time.time()-STATE['news']['fetched'] < NEWS_TTL:
                return
            STATE['status'] = 'fetching_news'
        items = []
        seen = set()
        err = None
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        for q in _queries():
            try:
                rows = _gnews(q)
                time.sleep(WEB_SLEEP)
            except Exception as e:
                err = str(e)
                continue
            for it in rows:
                rec = _normalize_item(it, seen, cutoff)
                if rec:
                    items.append(rec)
        # CNBC top news, keep if related to these funds or tickers
        try:
            for it in _rss_items('https://www.cnbc.com/id/100003114/device/rss/rss.html'):
                rec = _normalize_item(it, seen, cutoff)
                if rec:
                    rec['source'] = rec['source'] or 'CNBC'
                    if rec['source'] == 'Google News':
                        rec['source'] = 'CNBC'
                    items.append(rec)
        except Exception as e:
            err = ((err or '') + f' cnbc:{e}').strip()
        items.sort(key=lambda x: x['ts'], reverse=True)
        items = items[:300]
        path = os.path.join(DAT, 'news.json')
        tmp = path + '.tmp.' + str(os.getpid())
        with open(tmp, 'w') as f:
            json.dump({'fetched': _now_bj(), 'items': items}, f, ensure_ascii=False)
        os.replace(tmp, path)
        with LOCK:
            STATE['news'] = {
                'items': items,
                'fetched': time.time(),
                'fetched_bj': _now_bj(),
                'err': err,
            }
            STATE['status'] = 'ready'
            STATE['payload'] = None

def _load_news_disk():
    p = os.path.join(DAT, 'news.json')
    if not os.path.isfile(p):
        return
    try:
        d = json.load(open(p))
        with LOCK:
            if not STATE['news']['items']:
                STATE['news'] = {
                    'items': d.get('items') or [],
                    'fetched': os.path.getmtime(p),
                    'fetched_bj': d.get('fetched'),
                    'err': None,
                }
    except Exception:
        pass

def _score(bull, bear):
    tot = bull + bear
    if tot <= 0:
        return 0
    return int(max(-100, min(100, round(100.0 * (bull - bear) / tot))))

def _tone_label(score):
    if score >= 25:
        return 'up', '偏多'
    if score <= -25:
        return 'dn', '偏空'
    return 'mid', '中性'

def judgments(items):
    out = []
    for sym in ORDER:
        def item_tag(n, s=sym):
            per = n.get('ticker_tags') or {}
            if s in per:
                return per[s]
            if s in (n.get('tickers') or []) and len(n.get('tickers') or []) == 1:
                return n.get('tag') or '中性'
            return '中性'
        rel = [n for n in items if sym in (n.get('tickers') or [])]
        bull = sum(1 for n in rel if item_tag(n) == '看多')
        bear = sum(1 for n in rel if item_tag(n) == '看空')
        neu = sum(1 for n in rel if item_tag(n) == '中性')
        score = _score(bull, bear)
        tone, label = _tone_label(score)
        drivers = []
        for n in rel:
            tg = item_tag(n)
            if tg in ('看多', '看空') and len(drivers) < 4:
                drivers.append({
                    'tag': tg,
                    'title': n['title'],
                    'time_bj': n.get('time_bj') or '',
                    'source': n.get('source') or '',
                    'link': n.get('link') or '',
                    'funds': n.get('funds') or [],
                })
        latest = rel[0]['time_bj'] if rel else ''
        out.append({
            'symbol': sym,
            'kind': TICKERS[sym]['kind'],
            'score': score,
            'tone': tone,
            'label': label,
            'news_bull': bull,
            'news_bear': bear,
            'news_neu': neu,
            'n': len(rel),
            'thin': (bull + bear) < 3,
            'latest_bj': latest,
            'drivers': drivers,
        })
    return out

def fund_briefs(items):
    rows = []
    for f in FUNDS:
        rel = [n for n in items if f['id'] in (n.get('funds') or [])]
        bull = sum(1 for n in rel if n.get('tag') == '看多')
        bear = sum(1 for n in rel if n.get('tag') == '看空')
        latest = rel[0] if rel else None
        rows.append({
            'id': f['id'],
            'name': f['name'],
            'name_zh': f['name_zh'],
            'people': f['people'],
            'n': len(rel),
            'news_bull': bull,
            'news_bear': bear,
            'latest_title': latest['title'] if latest else '',
            'latest_bj': latest['time_bj'] if latest else '',
            'latest_tag': latest['tag'] if latest else '',
            'latest_link': latest['link'] if latest else '',
        })
    rows.sort(key=lambda r: (-r['n'], r['name']))
    return rows

def build_payload():
    items = list(STATE['news'].get('items') or [])
    # drop internal title_raw to keep payload smaller
    news = [{k: v for k, v in n.items() if k != 'title_raw'} for n in items]
    return {
        'news_fetched': STATE['news'].get('fetched_bj') or '',
        'generated': _now_bj(),
        'n_news': len(news),
        'n_funds': len(FUNDS),
        'ttl_min': NEWS_TTL // 60,
        'formula': FORMULA_ZH,
        'note': (
            '判断只依据近 7 天公开新闻的规则情绪，不是基金实时仓位。'
            '来源是 Google News 与 CNBC 的公开 RSS；基金经理在 X 上的发言、采访或致投资者信，只有被媒体转述时才会出现。'
            '时间均为北京时间。约每 20 分钟自动刷新。'
        ),
        'error': STATE['news'].get('err'),
        'judgments': judgments(items),
        'funds': fund_briefs(items),
        'news': news,
    }

def payload(force=False):
    _load_news_disk()
    with LOCK:
        stale = (not STATE['news']['fetched']) or (time.time() - STATE['news']['fetched'] > NEWS_TTL)
        cached = STATE['payload']
        if cached and not stale and not force:
            return cached
    if stale or force or not STATE['news']['items']:
        refresh_news(force=force)
    p = build_payload()
    with LOCK:
        STATE['payload'] = p
        STATE['payload_t'] = time.time()
    return p

def prewarm():
    def loop():
        try:
            payload(force=False)
        except Exception as e:
            print('funds news prewarm failed:', repr(e), flush=True)
        while True:
            time.sleep(NEWS_TTL)
            try:
                refresh_news(force=False)
                with LOCK:
                    STATE['payload'] = None
            except Exception as e:
                print('funds news refresh failed:', repr(e), flush=True)
    threading.Thread(target=loop, name='funds-news', daemon=True).start()

if __name__ == '__main__':
    p = payload(force=True)
    print(p['generated'], 'n_news', p['n_news'], 'err', p['error'])
    for j in p['judgments']:
        print(j['symbol'], j['label'], j['score'], '多', j['news_bull'], '空', j['news_bear'], '中', j['news_neu'], 'n', j['n'])
