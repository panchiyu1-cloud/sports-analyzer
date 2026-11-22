#!/usr/bin/env python3
"""模擬戰回測：用和盤中完全相同的程式，重播過去的交易日，看這套做法在過去會有什麼成績。

重播的是這三段程式本身（不是另外寫一份規則）：
    即時排名（Engine._analyze，盤中的預估量）→ 當沖模式的進場建議（DayTrade.advise）→ 模擬戰的買賣（Sim）

資料：Yahoo 的 5 分 K（最多回溯約 60 天），日 K 用掃描程式自己的快取（stock/data/live/hist）。

和盤中實際執行的差別（做不到一樣的地方，都往保守的方向算）：
  1. 盤中每幾秒看一次報價、每分鐘重新排名；這裡每 5 分鐘一次。「等 60 秒再確認」變成等下一根 5 分 K。
  2. 沒有歷史的內外盤資料：「外盤比 ≥ 50%」這個條件不檢查（和盤中資料還在累積時一樣，當作沒有意見）。
  3. 沒有當時的新聞與三大法人資料：排名只用技術面（盤中另外有新聞 ±8 分、法人幾分）。
  4. 買進價用「最後成交價往上一檔」（當作賣一價），市價賣出用最後成交價；停損在 5 分 K 內被碰到時，
     以停損價往下一檔成交（開盤就跳空跌破的用開盤價）；同一根 K 棒同時碰到停損與目標，算停損。
  5. 處置股、注意股、當天不能現股當沖的股票，程式分不出來。

用法：
    python3 stock/backtest_sim.py --days 30 --out /tmp/bt              # GitHub 主機上（要連得到 Yahoo、要有 stock/data 快取）
    python3 stock/backtest_sim.py --demo --days 6 --out /tmp/bt_demo   # 假資料，只測流程
"""
import argparse
import bisect
import gzip
import json
import os
import random
import sys
import tempfile
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from multiprocessing import Pool
from urllib.parse import quote

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from twscan import analysis as an  # noqa: E402
from twscan import sim as SM  # noqa: E402
from twscan import util  # noqa: E402
import twscan.engine as E  # noqa: E402
from twscan.daytrade import ceil_tick, floor_tick, tick_size  # noqa: E402
from twscan.util import log  # noqa: E402

# ---------------------------------------------------------------- 假時鐘
CLOCK = [datetime(2026, 1, 5, 8, 0, tzinfo=util.TW)]


def patch_clock():
    f = lambda: CLOCK[0]  # noqa: E731
    for name, mod in list(sys.modules.items()):
        if name.startswith("twscan") and hasattr(mod, "now_tw"):
            mod.now_tw = f


def set_clock(day, minute, sec=0):
    y, m, d = (int(x) for x in day.split("-"))
    minute, sec = minute + sec // 60, sec % 60
    CLOCK[0] = datetime(y, m, d, minute // 60, minute % 60, sec, tzinfo=util.TW)


# ---------------------------------------------------------------- 資料（整個程序共用；分析時用 fork 分給各個行程）
UNIVERSE, HIST, HDATES = {}, {}, {}
INDEX_HIST = {"TAIEX": [], "TPEX": []}
M5 = {}                    # code -> {day: [[分鐘, 開, 高, 低, 收, 量(股)], ...]}
IDX5 = {"TAIEX": {}, "TPEX": {}}
CFG = {}
STEP = 5
T_FIRST, T_LAST_ENTRY, T_END = 9 * 60 + 5, 12 * 60, 13 * 60 + 15
TOP_KEEP = 40


def parse_5m(j):
    res = ((j.get("chart") or {}).get("result") or [None])[0]
    if not res:
        return {}
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    off = (res.get("meta") or {}).get("gmtoffset", 28800)
    out = {}
    for i, t in enumerate(ts):
        try:
            o, h, lo, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        except (KeyError, IndexError):
            continue
        if None in (o, h, lo, c) or c <= 0:
            continue
        dt = datetime.fromtimestamp(t + off, tz=timezone.utc)
        m = dt.hour * 60 + dt.minute
        if m < 540 or m > 13 * 60 + 35:
            continue
        lab = 540 + min((m - 540) // STEP, 53) * STEP          # 13:30 的收盤集合競價併入 13:25 那一根
        d = out.setdefault(dt.date().isoformat(), {})
        b = d.get(lab)
        if b is None:
            d[lab] = [round(o, 2), round(h, 2), round(lo, 2), round(c, 2), int(v or 0)]
        else:
            b[1], b[2], b[3], b[4] = max(b[1], round(h, 2)), min(b[2], round(lo, 2)), round(c, 2), b[4] + int(v or 0)
    return {day: [[m] + b for m, b in sorted(d.items())] for day, d in out.items()}


def fetch_5m(http, sym, k=0):
    hosts = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    last = None
    for host in (hosts[k % 2], hosts[(k + 1) % 2]):
        url = f"https://{host}/v8/finance/chart/{quote(sym, safe='.')}?range=60d&interval=5m&includePrePost=false"
        try:
            return parse_5m(http.get_json(url, timeout=15, retries=1))
        except Exception as e:      # 記錯誤的種類就好
            last = e
    raise last


def load_real(data_dir, cache_dir, fetch=True):
    """讀掃描程式的快取（股票清單、日 K、指數），再抓（或讀回）每一檔的 5 分 K。"""
    u = util.read_json(os.path.join(data_dir, "universe.json"), {}) or {}
    UNIVERSE.update(u.get("universe") or {})
    hdir = os.path.join(data_dir, "hist")
    for fn in os.listdir(hdir):
        if fn.endswith(".json") and fn[:-5] in UNIVERSE:
            bars = util.read_json(os.path.join(hdir, fn))
            if isinstance(bars, list) and bars:
                HIST[fn[:-5]] = bars
    ih = util.read_json(os.path.join(data_dir, "index_hist.json"), {}) or {}
    for k in INDEX_HIST:
        INDEX_HIST[k] = ih.get(k) or []
    log(f"[回測] 快取：{len(UNIVERSE)} 檔股票、{len(HIST)} 檔有日 K、加權指數日 K {len(INDEX_HIST['TAIEX'])} 根"
        f"（最後一天 {INDEX_HIST['TAIEX'][-1][0] if INDEX_HIST['TAIEX'] else '—'}）")
    os.makedirs(os.path.join(cache_dir, "5m"), exist_ok=True)
    jobs = [(c, f"{c}.TW" if UNIVERSE[c].get("market") == "TWSE" else f"{c}.TWO") for c in sorted(HIST)
            if UNIVERSE[c].get("type") != "etf" and len(HIST[c]) >= an.MIN_BARS]
    jobs += [("^TAIEX", "^TWII"), ("^TPEX", "^TWOII")]
    http = E.Http(insecure_fallback=CFG.get("insecure_ssl_fallback", True))
    stat = {"cached": 0, "ok": 0, "empty": 0, "fail": 0, "errors": {}}

    def job(arg):
        k, (code, sym) = arg
        path = os.path.join(cache_dir, "5m", f"{code.strip('^')}.json.gz")
        if os.path.exists(path):
            try:
                with gzip.open(path, "rt", encoding="utf-8") as f:
                    stat["cached"] += 1
                    return code, json.load(f)
            except (OSError, ValueError):
                pass
        if not fetch:
            return code, {}
        try:
            d = fetch_5m(http, sym, k)
        except Exception as e:
            stat["fail"] += 1
            stat["errors"][type(e).__name__] = stat["errors"].get(type(e).__name__, 0) + 1
            return code, {}
        stat["ok" if d else "empty"] += 1
        with gzip.open(path, "wt", encoding="utf-8") as f:
            json.dump(d, f, separators=(",", ":"))
        return code, d

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=int(CFG.get("yahoo_workers", 4)) * 2) as ex:
        for n, (code, d) in enumerate(ex.map(job, enumerate(jobs))):
            if code in ("^TAIEX", "^TPEX"):
                IDX5[code.strip("^")] = d
            elif d:
                M5[code] = d
            if n and n % 400 == 0:
                log(f"[回測] 5 分 K 進度 {n}/{len(jobs)}（{time.time() - t0:.0f} 秒）")
    log(f"[回測] 5 分 K：{len(M5)} 檔有資料｜讀快取 {stat['cached']}、新抓 {stat['ok']}、沒有資料 {stat['empty']}、失敗 {stat['fail']} {stat['errors'] or ''}"
        f"｜指數 加權 {len(IDX5['TAIEX'])} 天、櫃買 {len(IDX5['TPEX'])} 天（{time.time() - t0:.0f} 秒）")
    return stat


def load_demo(days):
    """假資料：用示範模式的股票與日 K，另外隨機產生 5 分 K。只用來測流程，數字沒有意義。"""
    from twscan.demo import populate
    e = E.Engine(dict(CFG), demo=True)
    populate(e)
    UNIVERSE.update(e.universe)
    HIST.update(e.hist)
    for k in INDEX_HIST:
        INDEX_HIST[k] = e.index_hist.get(k) or []
    dates = [b[0] for b in INDEX_HIST["TAIEX"]][-days:]
    rnd = random.Random(7)
    for code, bars in HIST.items():
        byd = {b[0]: i for i, b in enumerate(bars)}
        out = {}
        for d in dates:
            i = byd.get(d)
            if not i:
                continue
            prev, avgv = bars[i - 1][4], sum(b[5] for b in bars[max(0, i - 20):i]) / 20
            drift = rnd.gauss(0.0002, 0.0012)
            p, rows = prev * (1 + rnd.gauss(0.002, 0.008)), []
            hot = rnd.random() < 0.35
            for k in range(54):
                o = p
                p = max(1.0, p * (1 + drift + rnd.gauss(0, 0.0022)))
                hi, lo = max(o, p) * (1 + abs(rnd.gauss(0, 0.0008))), min(o, p) * (1 - abs(rnd.gauss(0, 0.0008)))
                v = int(avgv / 54 * rnd.uniform(0.4, 1.6) * (3.0 if k == 0 else 1.0) * (2.0 if hot else 1.0))
                rows.append([540 + k * 5, floor_tick(o) or o, ceil_tick(hi), floor_tick(lo), floor_tick(p), v])
            out[d] = rows
        M5[code] = out
    for key in ("TAIEX", "TPEX"):
        byd = {b[0]: i for i, b in enumerate(INDEX_HIST[key])}
        for d in dates:
            i = byd.get(d)
            if i:
                prev = INDEX_HIST[key][i - 1][4]
                IDX5[key][d] = [[540 + k * 5, prev, prev, prev, prev * (1 + 0.0001 * k), 0] for k in range(54)]
    return {"demo": True}


# ---------------------------------------------------------------- 把某一天某個時間點的行情放進引擎
def make_engine():
    E.DATA_DIR = tempfile.mkdtemp(prefix="twscan_bt_")
    eng = E.Engine(dict(CFG))
    eng.universe = UNIVERSE
    eng.status["first_scan_done"] = True
    eng.daytrade._maybe_snapshot = lambda *a, **k: None      # 盤中名單驗證的存檔，回測不需要
    return eng


class Day:
    """某一個交易日的行情：日 K 截到前一天，5 分 K 逐根累計。"""

    def __init__(self, eng, day, spread):
        self.eng, self.day, self.spread = eng, day, spread
        self.cum = {}          # code -> 累計到目前為止 [開, 最高, 最低, 收, 量(張), Σ價×量, Σ量, 已用到第幾根]
        self.prev = {}
        self.skipped = 0
        hist = {}
        for c, bars in HIST.items():
            i = bisect.bisect_left(HDATES[c], day)
            if i >= an.MIN_BARS - 1:
                hist[c] = bars[:i]
        eng.hist = hist
        eng.index_hist = {k: [b for b in v if b[0] < day] for k, v in INDEX_HIST.items()}
        eng.snap, eng.flow, eng.analysis, eng.live_cands = {}, {}, {}, []
        eng.chips, eng.news = {}, {}          # 當時的法人與新聞資料拿不到：排名只用技術面
        eng.index = {}
        self.codes = [c for c in hist if day in (M5.get(c) or {})]
        for c in self.codes:
            p = hist[c][-1][4]
            o = M5[c][day][0][1]
            if not p or not (0.895 <= o / p <= 1.105):     # 前一天收盤和今天開盤對不上（減資、分割等），這一天跳過這一檔
                self.skipped += 1
                continue
            self.prev[c] = p
        self.codes = [c for c in self.codes if c in self.prev]

    def advance(self, code, t):
        """把 code 的行情推進到時間 t（只用開始時間 < t 的 K 棒）。回傳是否已經有成交。"""
        rows = M5[code][self.day]
        st = self.cum.get(code)
        if st is None:
            st = self.cum[code] = [None, None, None, None, 0.0, 0.0, 0.0, 0]
        k = st[7]
        while k < len(rows) and rows[k][0] < t:
            m, o, h, lo, c, v = rows[k]
            if st[0] is None:
                st[0], st[1], st[2] = o, h, lo
            st[1], st[2], st[3] = max(st[1], h), min(st[2], lo), c
            st[4] += v / 1000.0
            st[5] += (h + lo + c) / 3.0 * v
            st[6] += v
            k += 1
        st[7] = k
        return st[0] is not None

    def sides(self, price):
        return [price], [round(price + tick_size(price) * self.spread, 2)]

    def put(self, code, t, price=None, low=None, high=None, sec=0):
        """寫進 eng.snap / eng.flow。price 沒給＝用最後一根的收盤價。"""
        st = self.cum.get(code)
        if not st or st[0] is None:
            return False
        p = st[3] if price is None else price
        prev = self.prev[code]
        t, sec = t + sec // 60, sec % 60
        bids, asks = self.sides(p)
        self.eng.snap[code] = {
            "date": self.day, "time": f"{t // 60:02d}:{t % 60:02d}:{sec:02d}", "price": p, "prev": prev, "open": st[0],
            "high": max(st[1], p) if high is None else high, "low": min(st[2], p) if low is None else low, "vol": st[4],
            "bids": bids, "asks": asks, "limit_up": floor_tick(prev * 1.1), "limit_down": ceil_tick(prev * 0.9)}
        self.eng.flow[code] = {"date": self.day, "pv": st[5], "pv_v": st[6], "outer": 0.0, "inner": 0.0}
        return True

    def index(self, t):
        for key in ("TAIEX", "TPEX"):
            hist = self.eng.index_hist.get(key) or []
            prev = hist[-1][4] if hist else None
            rows = [r for r in (IDX5[key].get(self.day) or []) if r[0] < t]
            self.eng.index[key] = {"date": self.day, "price": rows[-1][4] if rows else None, "prev": prev}

    def bar(self, code, start):
        for r in M5.get(code, {}).get(self.day, []):
            if r[0] == start:
                return r
            if r[0] > start:
                break
        return None


# ---------------------------------------------------------------- 第一段：每 5 分鐘的即時排名（各天互不相干，可以平行）
_ENG = None


def analyze_day(day):
    global _ENG
    try:
        if _ENG is None:
            patch_clock()
            _ENG = make_engine()
        eng = _ENG
        d = Day(eng, day, 1)
        thr, pmin = CFG["top_min_avg_value"], CFG["top_min_price"]
        steps, n_el = {}, []
        for t in range(T_FIRST, T_LAST_ENTRY + 1, STEP):
            set_clock(day, t)
            for c in d.codes:
                if d.advance(c, t):
                    d.put(c, t)
            d.index(t)
            est = eng.volume_fraction() if CFG.get("volume_estimate", True) else None
            cand = []
            for c in d.codes:                       # 先用和排名同一條公式篩掉成交值不夠的，省時間（結果不變）
                b = eng.merged_bars(c, est)
                if b and b[-1][0] == day and b[-1][4] >= pmin and sum(x[4] * x[5] for x in b[-20:]) / 20 >= thr:
                    cand.append(c)
            res, _ = eng._analyze(cand, None, est)
            ranked = sorted(((c, r) for c, r in res.items() if eng._eligible(c, r)),
                            key=lambda x: (x[1]["score"], x[1]["raw"], x[1]["avg_value"]), reverse=True)
            n_el.append(len(ranked))
            steps[t] = {"cands": [c for c, _ in ranked[:TOP_KEEP]],
                        "an": {c: {"score": r["score"], "signals": sorted((s for s in r["signals"] if s.get("pts", 0) > 0), key=lambda s: -s["pts"])[:3]}
                               for c, r in ranked[:TOP_KEEP]}}
        return day, steps, {"stocks": len(d.codes), "skipped": d.skipped, "eligible": round(sum(n_el) / max(1, len(n_el))),
                            "index": bool(IDX5["TAIEX"].get(day)), "tpex": bool(IDX5["TPEX"].get(day))}
    except Exception:
        traceback.print_exc()
        return day, {}, {"error": True}


# ---------------------------------------------------------------- 第二段：照時間順序重播，讓模擬戰自己買賣
def replay(days, steps_by_day, spread=1, overrides=None):
    patch_clock()
    old = dict(SM.DEF)
    SM.DEF.update(overrides or {})
    try:
        eng = make_engine()
        sim = eng.sim
        set_clock(days[0], 8 * 60 + 30)
        sim.load()
        for day in days:
            d = Day(eng, day, spread)
            steps = steps_by_day.get(day) or {}
            anchor = next((c for c in ("2330", "2317", "0050") if c in d.prev), d.codes[0] if d.codes else None)
            for t in range(T_FIRST, T_END + 1, STEP):
                # 1) 手上的部位：先走完 t-5 ～ t 這一根 K 棒（開盤價 → 碰到停損或目標 → 收盤價）
                for it in list(sim._open()):
                    code, b = it["code"], d.bar(it["code"], t - STEP)
                    if not b or code not in d.cum:
                        continue
                    _m, o, h, lo, _c, _v = b
                    st = d.cum[code]
                    set_clock(day, t - STEP, 5)
                    d.put(code, t - STEP, price=o, low=min(st[2], o), high=max(st[1], o), sec=5)
                    with eng.lock:
                        sim._manage(it, CLOCK[0])
                    if it.get("stage") != "open":
                        continue
                    px = None
                    if lo <= it["stop"]:
                        px = max(lo, round(it["stop"] - tick_size(it["stop"]) * spread, 2))
                    elif h >= it["target"]:
                        px = it["target"]
                    if px is not None:
                        set_clock(day, t - STEP, 150)
                        d.put(code, t - STEP, price=px, low=min(st[2], o, px), high=max(st[1], o, px), sec=150)
                        with eng.lock:
                            sim._manage(it, CLOCK[0])
                # 2) 這個時間點：更新行情、排名，讓模擬戰檢查一次
                set_clock(day, t)
                step = steps.get(t)
                need = set(step["cands"] if step else []) | set(sim.watch_codes()) | ({anchor} if anchor else set())
                for c in need:
                    if c in d.prev and d.advance(c, t):
                        d.put(c, t)
                d.index(t)
                if step:
                    eng.analysis = step["an"]
                    eng.daytrade.on_live_rank(step["cands"])
                    eng.live_cands = step["cands"]
                else:
                    eng.live_cands = []
                sim._last = 0
                sim.tick()
            # 收盤：還沒賣掉的（跌停鎖住）用最後一根的收盤價結算
            set_clock(day, 13 * 60 + 40)
            for c in sim.watch_codes():
                if c in d.prev and d.advance(c, 13 * 60 + 40):
                    d.put(c, 13 * 60 + 30)
            sim.on_close()
        api, hist = sim.api(), sim.hist_api()
        hist["trades"] = list(reversed(sim.hist))
        return api, hist, list(sim.hist), list(sim.st["log"])
    finally:
        SM.DEF.clear()
        SM.DEF.update(old)


def breakdown(trades):
    """交易分組統計：進場時段、股價、排名、當時大盤、出場原因以外的幾個角度。"""
    def grp(key):
        out = {}
        for t in trades:
            k = key(t)
            g = out.setdefault(k, {"n": 0, "win": 0, "pnl": 0, "r": 0.0})
            g["n"] += 1
            g["win"] += 1 if t["pnl"] > 0 else 0
            g["pnl"] += t["pnl"]
            g["r"] += t.get("r") or 0
        return [{"k": k, "n": g["n"], "win": g["win"], "pnl": g["pnl"], "avg_r": round(g["r"] / g["n"], 2)} for k, g in sorted(out.items())]

    def hour(t):
        m = int(t["time"][:2]) * 60 + int(t["time"][3:5])
        return "1 09:10–09:30" if m < 570 else "2 09:30–10:00" if m < 600 else "3 10:00–11:00" if m < 660 else "4 11:00–12:00"

    def price(t):
        p = t["entry"]
        return "1 50 元以下" if p < 50 else "2 50–100 元" if p < 100 else "3 100 元以上"

    def mkt(t):
        x = (t.get("ctx") or {}).get("taiex")
        return "0 沒有指數資料" if x is None else "1 加權指數當時是漲的" if x > 0 else "2 加權指數當時是跌的"

    def rank(t):
        r = (t.get("ctx") or {}).get("rank") or t.get("rank") or 99
        return "1 第 1–3 名" if r <= 3 else "2 第 4–10 名" if r <= 10 else "3 第 11 名以後"
    n = len(trades)
    return {"hour": grp(hour), "price": grp(price), "market": grp(mkt), "rank": grp(rank),
            "reached_1r": sum(1 for t in trades if (t.get("mfe_r") or 0) >= 1), "n": n,
            "avg_mfe": round(sum(t.get("mfe_r") or 0 for t in trades) / n, 2) if n else None,
            "avg_mae": round(sum(t.get("mae_r") or 0 for t in trades) / n, 2) if n else None}


VARIANTS = [
    ("main", "和模擬戰相同的規則（買在最後成交價往上一檔）", 1, None),
    ("opt", "樂觀：買在最後成交價（沒有價差）", 0, None),
    ("nocap", "單筆上限放寬到 100%（可以買較貴的股票）", 1, {"pos_pct": 100}),
]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--out", default="/tmp/twscan_bt")
    ap.add_argument("--data-dir", default=os.path.join(ROOT, "data", "live"))
    ap.add_argument("--cache-dir", default=os.path.join(ROOT, "data", "bt"))
    ap.add_argument("--workers", type=int, default=os.cpu_count() or 2)
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--no-fetch", action="store_true")
    a = ap.parse_args()
    CFG.update(E.load_config() if not a.demo else dict(E.DEFAULT_CONFIG))
    t0 = time.time()
    real_now = datetime.now(util.TW)
    if a.demo:
        fetch = load_demo(a.days + 2)
    else:
        fetch = load_real(a.data_dir, a.cache_dir, fetch=not a.no_fetch)
    for c, bars in HIST.items():
        HDATES[c] = [b[0] for b in bars]
    # 交易日：有 5 分 K 的日子（看幾檔每天都有交易的股票），而且日 K 也已經有那一天（才能確定是完整的一天）
    seen = set()
    for c in ("2330", "2317", "0050", "2454", "2412"):
        seen.update((M5.get(c) or {}).keys())
    if not seen:
        for c in list(M5)[:20]:
            seen.update(M5[c].keys())
    full = {b[0] for b in INDEX_HIST["TAIEX"]} | {d for c in ("2330", "2317") for d in HDATES.get(c, [])}
    days = sorted(d for d in seen if d in full)[-a.days:]
    if not days:
        log("[回測] 沒有可用的交易日（5 分 K 抓不到？）")
        os.makedirs(a.out, exist_ok=True)
        util.write_json(os.path.join(a.out, "sim_backtest.json"), {"v": 1, "error": "沒有 5 分 K 資料", "fetch": fetch})
        return 1
    log(f"[回測] {len(days)} 個交易日：{days[0]} ～ {days[-1]}｜第一段：每 5 分鐘的即時排名（{a.workers} 個行程）")
    patch_clock()
    if a.workers > 1:
        with Pool(a.workers) as pool:
            parts = pool.map(analyze_day, days, chunksize=1)
    else:
        parts = [analyze_day(d) for d in days]
    steps_by_day = {d: s for d, s, _ in parts}
    diag = {d: g for d, _, g in parts}
    log(f"[回測] 第一段完成（{time.time() - t0:.0f} 秒）｜每天平均 {round(sum(g.get('stocks', 0) for g in diag.values()) / len(diag))} 檔有 5 分 K、"
        f"{round(sum(g.get('eligible', 0) for g in diag.values()) / len(diag))} 檔符合排名門檻")
    th = INDEX_HIST["TAIEX"]
    by = {b[0]: i for i, b in enumerate(th)}
    bench = None
    if days[0] in by and days[-1] in by and by[days[0]] > 0:
        p0, p1 = th[by[days[0]] - 1][4], th[by[days[-1]]][4]
        bench = {"from": p0, "to": p1, "pct": round((p1 / p0 - 1) * 100, 2),
                 "days": [[d, round((th[by[d]][4] / th[by[d] - 1][4] - 1) * 100, 2)] for d in days if d in by and by[d] > 0]}
    out = {"v": 1, "kind": "sim_backtest", "generated": real_now.strftime("%Y-%m-%d %H:%M"), "demo": a.demo,
           "from": days[0], "to": days[-1], "n_days": len(days), "step_min": STEP, "fetch": fetch, "diag": diag,
           "bench": bench, "variants": []}
    for key, label, spread, ov in VARIANTS:
        t1 = time.time()
        api, hist, trades, _lg = replay(days, steps_by_day, spread, ov)
        s = api["stats"]
        log(f"[回測] {label}：{s.get('n', 0)} 筆、勝率 {s.get('win_rate', '—')}%、總損益 {s.get('pnl', 0):+,}、餘額 {api['account']['balance']:,}"
            f"、最大回落 {api['account']['max_dd']:,}（{time.time() - t1:.0f} 秒）")
        sig = sum(d.get("signals", 0) for d in api["days"])
        v = {"key": key, "label": label, "api": api, "extra": breakdown(trades),
             "signals": sig, "cancels": max(0, sig - len(trades))}     # 出現「可進場」但等到下一根 K 棒條件已經不在，沒有買
        if key == "main":
            v["hist"] = hist
        out["variants"].append(v)
    os.makedirs(a.out, exist_ok=True)
    util.write_json(os.path.join(a.out, "sim_backtest.json"), out)
    log(f"[回測] 完成，共 {time.time() - t0:.0f} 秒 → {os.path.join(a.out, 'sim_backtest.json')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
