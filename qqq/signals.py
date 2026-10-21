"""Pure signal logic: resampling + per-timeframe V-reversal checks + multi-TF confirm."""
from datetime import datetime, timedelta, timezone

def parse_bars(raw):
    out = []
    for b in raw:
        t = datetime.fromisoformat(b["time"].replace("Z", "+00:00"))
        out.append(dict(t=t, o=float(b["open"]), h=float(b["high"]), l=float(b["low"]),
                        c=float(b["close"]), v=float(b["volume"])))
    out.sort(key=lambda x: x["t"])
    return out

def resample(bars1, n, now=None):
    """Aggregate closed 1m bars into n-minute bars aligned to 09:30 ET (13:30/14:30Z
    depending on DST: alignment uses minute-of-day of the first bar of each day)."""
    if n == 1:
        return list(bars1)
    groups, out = {}, []
    day_open = {}
    for b in bars1:
        d = b["t"].date()
        day_open.setdefault(d, b["t"])
        k = (d, int((b["t"] - day_open[d]).total_seconds() // 60) // n)
        groups.setdefault(k, []).append(b)
    for k in sorted(groups):
        g = groups[k]
        if len(g) < n:  # incomplete bucket (still forming)
            continue
        out.append(dict(t=g[0]["t"], o=g[0]["o"], h=max(x["h"] for x in g),
                        l=min(x["l"] for x in g), c=g[-1]["c"], v=sum(x["v"] for x in g)))
    return out

def ema(vals, n):
    k, e, out = 2 / (n + 1), None, []
    for v in vals:
        e = v if e is None else v * k + e * (1 - k)
        out.append(e)
    return out

def eval_bar(bars, i, cfg):
    """Return (direction|None, reason dict) for bar i."""
    if i < max(cfg["vol_lookback"], cfg["trend_lookback"], 2):
        return None, None
    b, p = bars[i], bars[i - 1]
    closes = [x["c"] for x in bars[: i + 1]]
    e5, e9 = ema(closes, cfg["ema_fast"]), ema(closes, cfg["ema_slow"])
    vavg = sum(x["v"] for x in bars[i - cfg["vol_lookback"]: i]) / cfg["vol_lookback"]
    vol_ok = b["v"] >= cfg["vol_mult"] * vavg
    win = bars[i - cfg["trend_lookback"]: i + 1]
    thr = cfg["trend_min_move_pct"] / 100
    # prior decline: highest high then later lowest low within window
    hi_idx = max(range(len(win)), key=lambda j: win[j]["h"])
    lo_after = min(win[hi_idx:], key=lambda x: x["l"])
    drop = (win[hi_idx]["h"] - lo_after["l"]) / win[hi_idx]["h"]
    lo_idx = min(range(len(win)), key=lambda j: win[j]["l"])
    hi_after = max(win[lo_idx:], key=lambda x: x["h"])
    rise = (hi_after["h"] - win[lo_idx]["l"]) / win[lo_idx]["l"]
    base = dict(bar_time=b["t"].isoformat(), o=b["o"], h=b["h"], l=b["l"], c=b["c"], vol=int(b["v"]),
                vol_avg10=round(vavg), ema5=round(e5[i], 3), ema9=round(e9[i], 3))
    bull_px = b["c"] > p["h"]
    bull_ema = b["c"] > e5[i] and b["c"] > e9[i] and e5[i] > e9[i] and e5[i - 1] <= e9[i - 1]
    if (bull_px or bull_ema) and vol_ok and drop >= thr and b["c"] > lo_after["l"]:
        r = dict(base, trigger="close>prev_high" if bull_px else "ema5 cross up ema9",
                 prior_decline_pct=round(drop * 100, 3), swing_low=lo_after["l"])
        return "bullish", r
    bear_px = b["c"] < p["l"]
    bear_ema = b["c"] < e5[i] and b["c"] < e9[i] and e5[i] < e9[i] and e5[i - 1] >= e9[i - 1]
    if (bear_px or bear_ema) and vol_ok and rise >= thr and b["c"] < hi_after["h"]:
        r = dict(base, trigger="close<prev_low" if bear_px else "ema5 cross down ema9",
                 prior_rally_pct=round(rise * 100, 3), swing_high=hi_after["h"])
        return "bearish", r
    return None, None

class Confirmer:
    """Tracks latest per-TF signal; fires when all TFs agree within confirm window."""
    def __init__(self, cfg):
        self.cfg, self.last = cfg, {}
        self.last_fire = {}  # dir -> time
        self.last_dir = None

    def update(self, tf, bars):
        """Evaluate the last closed bar of tf; bar close time = t + tf min."""
        if not bars:
            return
        d, r = eval_bar(bars, len(bars) - 1, self.cfg)
        if d:
            close_t = bars[-1]["t"] + timedelta(minutes=tf)
            self.last[tf] = (d, close_t, r)

    def check(self, now):
        w = timedelta(minutes=self.cfg["confirm_window_min"])
        tfs = self.cfg["timeframes"]
        if not all(tf in self.last for tf in tfs):
            return None
        dirs = {self.last[tf][0] for tf in tfs}
        if len(dirs) != 1:
            return None
        if any(now - self.last[tf][1] > w for tf in tfs):
            return None
        d = dirs.pop()
        lf = self.last_fire.get(d)
        flipped = self.last_dir is not None and self.last_dir != d
        if lf and not flipped and now - lf < timedelta(minutes=self.cfg["cooldown_min"]):
            return None
        # avoid re-firing on the identical set of bars
        key = tuple(self.last[tf][1] for tf in tfs)
        if getattr(self, "_lastkey", None) == key:
            return None
        self._lastkey = key
        self.last_fire[d], self.last_dir = now, d
        return d, {f"{tf}m": self.last[tf][2] for tf in tfs}
