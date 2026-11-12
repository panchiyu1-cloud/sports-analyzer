"""示範模式：產生模擬股票資料，用來預覽介面與測試程式（所有數字皆為虛構）。"""
import math
import random
import time
from datetime import timedelta

from .util import now_tw, today_iso

INDUSTRIES = ["半導體業", "電子零組件業", "電腦及週邊設備業", "光電業", "通信網路業", "航運業", "金融保險業",
              "生技醫療業", "鋼鐵工業", "塑膠工業", "食品工業", "建材營造業", "電機機械", "其他電子業"]
KINDS = ["breakout", "wbottom", "wash", "dist", "trend", "down", "random", "random", "box", "trend"]


def _days(n, include_today=False):
    d = now_tw().date()
    out = []
    if include_today:
        out.append(d.isoformat())
        d -= timedelta(days=1)
    elif now_tw().hour < 14:
        d -= timedelta(days=1)
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d.isoformat())
        d -= timedelta(days=1)
    return out[::-1]


def _segments(kind, n, rnd):
    if kind == "breakout":
        return [(n - 46, rnd.uniform(0.0, 0.25), 1.0), (45, rnd.uniform(-0.03, 0.03), 0.7), (1, 0.065, 3.2)]
    if kind == "wbottom":
        return [(n - 70, rnd.uniform(-0.05, 0.1), 1.0), (18, -0.18, 1.2), (14, 0.12, 0.9), (16, -0.11, 0.8),
                (19, 0.13, 1.1), (3, 0.06, 2.2)]
    if kind == "wash":
        return [(n - 60, rnd.uniform(-0.05, 0.08), 1.0), (40, 0.32, 1.5), (17, -0.10, 0.5), (3, 0.015, 0.7)]
    if kind == "dist":
        return [(n - 70, rnd.uniform(-0.05, 0.05), 1.0), (60, 0.65, 1.6), (10, -0.01, 2.6)]
    if kind == "trend":
        return [(n - 60, rnd.uniform(-0.1, 0.1), 1.0), (60, rnd.uniform(0.15, 0.3), 1.3)]
    if kind == "down":
        return [(n - 60, rnd.uniform(-0.05, 0.1), 1.0), (60, rnd.uniform(-0.3, -0.12), 1.2)]
    if kind == "box":
        return [(n - 50, rnd.uniform(-0.1, 0.2), 1.0), (50, 0.0, 0.8)]
    return [(n, rnd.uniform(-0.2, 0.3), 1.0)]


def _series(kind, n, rnd):
    price = rnd.choice([rnd.uniform(15, 60), rnd.uniform(60, 250), rnd.uniform(250, 900)])
    base_vol = rnd.uniform(800, 25000) * 1000
    bars = []
    c = price
    noise = rnd.uniform(0.012, 0.025)
    for seg_len, seg_ret, vmul in _segments(kind, n, rnd):
        drift = math.log(1 + seg_ret) / max(seg_len, 1)
        for k in range(seg_len):
            prev = c
            r = drift + rnd.gauss(0, noise * (0.6 if seg_len == 1 else 1))
            if seg_len == 1:
                r = drift
            c = max(1.0, prev * math.exp(r))
            o = prev * math.exp(rnd.gauss(0, noise * 0.4))
            hi = max(o, c) * (1 + abs(rnd.gauss(0, noise * 0.5)))
            lo = min(o, c) * (1 - abs(rnd.gauss(0, noise * 0.5)))
            if kind == "dist" and vmul > 2 and k >= seg_len - 6:
                hi = max(o, c) * 1.035          # 高檔長上影
            if kind == "wash" and seg_len == 3 and k == seg_len - 1:
                lo = min(o, c) * 0.965          # 長下影止跌
                c = max(c, o * 1.01)
            if seg_len == 1:
                o = prev * 1.005
                hi = c * 1.004
                lo = prev * 0.998
            v = base_vol * vmul * math.exp(rnd.gauss(0, 0.3))
            bars.append([o, hi, lo, c, v])
    return bars


def populate(eng):
    rnd = random.Random(20261108)
    n = 260
    live = eng.demo_live
    days = _days(n, include_today=live)
    chip_days = days[-6:-1] if live else days[-5:]
    frac = eng.volume_fraction() if live else 1.0
    uni, hist, snap, chips_by_day, news = {}, {}, {}, {d: {} for d in chip_days}, {}
    for k in range(160):
        code = f"{9100 + k}"
        kind = KINDS[k % len(KINDS)]
        ind = INDUSTRIES[k % len(INDUSTRIES)]
        name = f"示範{ind[:2]}{k + 1:03d}"
        uni[code] = {"code": code, "name": name, "market": "TWSE" if k % 3 else "TPEx", "type": "stock", "industry": ind}
        raw = _series(kind, n, rnd)
        bars = [[days[i], _tk(b[0]), _tk(b[1]), _tk(b[2]), _tk(b[3]), int(b[4]), _tk(b[3])] for i, b in enumerate(raw)]
        last, prev = bars[-1], bars[-2]
        hist[code] = bars[:-1] if live else bars
        snap[code] = {"price": last[4], "prev": prev[4], "open": last[1], "high": last[2], "low": last[3],
                      "vol": last[5] / 1000 * frac, "value": last[5] * last[4] * frac, "date": last[0],
                      "time": "10:32:00" if live else "收盤", "src": "mis" if live else "daily", "est": False}
        if live:
            if k % 3 == 1:   # 示範「推估價」：這一刻沒有揭示成交價
                snap[code].update(est=True, lt=_tk(last[4] * 0.998), lt_t="10:31:42")
            else:
                snap[code].update(lt=last[4], lt_t="10:31:58")
        bias = {"breakout": 1, "wash": 0.6, "trend": 0.8, "dist": -0.8, "down": -1}.get(kind, 0)
        for d in chip_days:
            f = rnd.gauss(bias, 1) * last[5] * 0.03
            t = rnd.gauss(bias * 0.8, 1) * last[5] * 0.01
            dd = rnd.gauss(0, 1) * last[5] * 0.01
            chips_by_day[d][code] = {"f": round(f), "t": round(t), "d": round(dd), "total": round(f + t + dd)}
        pos = [f"{name} 營收創新高 法人看好後市", f"{name} 接單暢旺 擴產計畫出爐", f"外資調升 {name} 目標價"]
        neg = [f"{name} 獲利下滑 外資調降評等", f"{name} 高檔震盪 主力疑似倒貨", f"{name} 月營收年減 需求疲弱"]
        pool = pos if bias > 0 else neg if bias < 0 else pos[:1] + neg[:1]
        items = []
        for j in range(rnd.randint(0, 4)):
            t = pool[j % len(pool)]
            age = rnd.uniform(0.05, 5)
            items.append({"title": t, "link": "", "source": "示範資料", "ts": time.time() - age * 86400})
        from .news import summarize
        news[code] = {"fetched": time.time(), **summarize(items, code, name)}
    for k in range(6):
        code = f"009{10 + k}"
        uni[code] = {"code": code, "name": f"示範ETF{k + 1}", "market": "TWSE", "type": "etf", "industry": "ETF"}
        raw = _series("random", n, rnd)
        bars = [[days[i], _tk(b[0], True), _tk(b[1], True), _tk(b[2], True), _tk(b[3], True), int(b[4]), _tk(b[3], True)]
                for i, b in enumerate(raw)]
        hist[code] = bars[:-1] if live else bars
        snap[code] = {"price": bars[-1][4], "prev": bars[-2][4], "open": bars[-1][1], "high": bars[-1][2],
                      "low": bars[-1][3], "vol": bars[-1][5] / 1000 * frac, "value": bars[-1][5] * bars[-1][4] * frac,
                      "date": bars[-1][0], "time": "10:32:00" if live else "收盤", "src": "mis" if live else "daily",
                      "est": False, "lt": bars[-1][4] if live else None, "lt_t": "10:31:58" if live else None}
    idx = []
    c = 20000.0
    for d in days:
        o = c
        c = c * math.exp(rnd.gauss(0.0006, 0.009))
        idx.append([d, round(o, 2), round(max(o, c) * 1.003, 2), round(min(o, c) * 0.997, 2), round(c, 2), 0])
    otc, c = [], 250.0
    for k, d in enumerate(days):  # 櫃買指數：前段中小型股較強、後段轉弱（示範風格輪動）
        o = c
        c = c * math.exp(rnd.gauss(0.0012 if k < len(days) * 0.75 else -0.0015, 0.011))
        otc.append([d, round(o, 2), round(max(o, c) * 1.004, 2), round(min(o, c) * 0.996, 2), round(c, 2), 0])
    with eng.lock:
        eng.universe, eng.hist, eng.snap, eng.chips, eng.news = uni, hist, snap, chips_by_day, news
        eng.index_hist = {"TAIEX": idx[:-1] if live else idx, "TPEX": otc[:-1] if live else otc}
        if live:
            eng.index = {"TAIEX": {"name": "加權指數", "price": idx[-1][4], "prev": idx[-2][4], "date": days[-1], "time": "10:32:00"},
                         "TPEX": {"name": "櫃買指數", "price": otc[-1][4], "prev": otc[-2][4], "date": days[-1], "time": "10:32:00"}}
        eng.market_news = {"fetched": time.time(), "score": 0, "n": 3, "items": [
            {"title": "示範新聞：台股量能回溫 電子權值股領漲", "link": "", "source": "示範資料", "ts": time.time() - 3600, "s": 1, "codes": []},
            {"title": "示範新聞：外資連三日買超 半導體族群受惠", "link": "", "source": "示範資料", "ts": time.time() - 7200, "s": 2, "codes": []},
            {"title": "示範新聞：航運股高檔震盪 留意追高風險", "link": "", "source": "示範資料", "ts": time.time() - 9000, "s": -1, "codes": []},
        ]}
        eng.last_sweep = now_tw()
        eng._update_regime()


def _tk(p, etf=False):
    """四捨五入到合法的升降單位，示範資料看起來才像真的報價。"""
    from .daytrade import tick_size
    t = tick_size(p, etf)
    return round(round(p / t) * t, 2)


def tick(eng):
    """模擬盤中跳動，讓前端的即時更新與內外盤可以被測試。約三分之一的跳動模擬「這一刻沒有揭示成交價」的推估價。"""
    from .daytrade import tick_size
    rnd = random.Random()
    with eng.lock:
        codes = list(eng.rt_priority[:30]) or list(eng.snap)[:30]
        now = eng.clock().strftime("%H:%M:%S")
        for code in codes:
            s = eng.snap.get(code)
            if not s:
                continue
            etf = (eng.universe.get(code) or {}).get("type") == "etf"
            p = _tk(s["price"] * (1 + rnd.gauss(0, 0.002)), etf)
            dv = rnd.randint(5, 120)
            est = rnd.random() < 0.35
            s.update(price=p, high=max(s["high"], p), low=min(s["low"], p), vol=s["vol"] + dv, time=now, est=est)
            if not est:
                s.update(lt=p, lt_t=now, lt_v=s["vol"])
            step = tick_size(p, etf)
            s["bids"] = [round(p - step * (i + (0 if est else 1)), 2) for i in range(5)]
            s["asks"] = [round(p + step * (i + (1 if est else 0)), 2) for i in range(5)]
            s["bid_vols"] = [rnd.randint(10, 300) for _ in range(5)]
            s["ask_vols"] = [rnd.randint(10, 300) for _ in range(5)]
            f = eng.flow.setdefault(code, {"date": s["date"], "outer": 0.0, "inner": 0.0, "since": "09:00:00"})
            f["date"] = s["date"]
            if f.get("pv_v") is None:
                f["pv"], f["pv_v"] = (s["vol"] - dv) * (s["open"] + s["high"] + s["low"] + p) / 4, s["vol"] - dv
            f["pv"] += dv * p
            f["pv_v"] = s["vol"]
            share = 0.62 if (eng.analysis.get(code) or {}).get("score", 0) >= 50 else 0.45
            out = sum(1 for _ in range(dv) if rnd.random() < share)
            f["outer"] += out * 5
            f["inner"] += (dv - out) * 5
            f["depth"] = sum(s["bid_vols"]) / sum(s["ask_vols"])


def populate_daytrade(eng):
    """示範用的當沖資料：過去幾天的盤中名單驗證、交易紀錄；--demo-live 另外加上今天的排名、分鐘價、持倉與提醒。"""
    from .daytrade import CHECKPOINTS, floor_tick, net_ret, tick_size
    dt = eng.daytrade
    rnd = random.Random(7)
    disc = eng.cfg.get("fee_discount", 0.28)
    codes = [c for c, u in eng.universe.items() if u["type"] == "stock"]
    days = [b[0] for b in eng.index_hist["TAIEX"]][-15:]
    for d in days[:-1] if not eng.demo_live else days:
        rec = {"date": d, "final": True, "snaps": {}, "alerts": []}
        for ci, cp in enumerate(CHECKPOINTS):
            picks = []
            for k, code in enumerate(rnd.sample(codes, 10)):
                price = round(rnd.uniform(20, 300), 1)
                sp = rnd.uniform(0.008, 0.025)
                stop, target = round(price * (1 - sp), 2), round(price * (1 + 2 * sp), 2)
                adv = rnd.choices(["go", "wait", "avoid"], [0.25 + 0.08 * ci, 0.5, 0.25])[0]
                edge = (0.35 if adv == "go" else 0.0) + (0.2 if k == 0 else 0.0) + 0.1 * ci - 0.15
                u = rnd.random()
                if u < 0.36 - edge * 0.2:
                    hit, close = ["stop", "10:40"], price * (1 + rnd.gauss(-0.014, 0.012))
                elif u < 0.58:
                    hit, close = ["target", "11:20"], price * (1 + rnd.gauss(0.024, 0.012))
                else:
                    hit, close = None, min(target * 0.999, max(stop * 1.001, price * (1 + rnd.gauss(edge / 100 + 0.002, 0.008))))
                ex = stop if hit and hit[0] == "stop" else target if hit else close
                picks.append({"rank": k + 1, "code": code, "name": eng.universe[code]["name"], "score": rnd.randint(48, 78),
                              "etf": False, "price": price, "entry": price, "stop": stop, "target": target, "adv": adv,
                              "fit": True, "hi": max(price, close), "lo": min(price, close), "hit": hit, "close": round(close, 2),
                              "ret": round(net_ret(price, close, disc), 3), "rule": round(net_ret(price, ex, disc), 3)})
            rec["snaps"][cp] = {"time": cp, "picks": picks}
        dt.history[d] = rec
    past = days[-4:-1]
    for k, (d, buy, sell, lots) in enumerate([(past[0], 48.6, 49.35, 3), (past[1], 126.5, 125.0, 1), (past[2], 22.35, 22.75, 1)]):
        code = codes[k * 7]
        dt._record(code, eng.universe[code]["name"], False, lots, buy, sell, d, "09:40", "11:15", True, {"manual": True})
        dt.trades[-1]["id"] = f"demo{k}"
    if not eng.demo_live:
        return
    today = today_iso()
    day = dt._day()
    # 讓前幾名呈現不同狀況：可進場／離均價太遠／剛進榜／開高走低
    shapes = [("go", 0.9, 62, "09:02"), ("far", 2.8, 58, "09:02"), ("go", 0.5, 66, "09:21"), ("new", 0.7, 57, "10:29"),
              ("below", -0.6, 44, "09:02"), ("go", 1.1, 60, "09:40")]
    fit = []
    for code in eng.live_cands[:40]:
        s = eng.snap.get(code) or {}
        if s.get("price") and s["price"] * 1000 <= dt.settings["budget"] * 0.6:
            fit.append(code)
    for k, code in enumerate(eng.live_cands[:25]):
        day["ranks"][code] = {"since": "09:02", "first": "09:02", "last": "10:32", "rank": k + 1, "miss": 0}
    for k, code in enumerate(fit[:12]):
        s = eng.snap[code]
        kind, ext, outer, since = shapes[k % len(shapes)]
        p = s["price"]
        etf = False
        vw = p / (1 + ext / 100)
        if kind == "below":
            s["open"] = round(p * 1.012, 2)
            s["high"] = max(s["high"], round(p * 1.02, 2))
        else:
            s["open"] = floor_tick(min(s["open"], vw * 0.996), etf)
            s["low"] = min(s["low"], floor_tick(s["open"] * 0.995, etf))
        tk = tick_size(p, etf)
        s["bids"] = [round(p - tk * (i + 1), 2) for i in range(5)]
        s["asks"] = [round(p + tk * i, 2) for i in range(5)]
        s["bid_vols"] = [rnd.randint(20, 300) for _ in range(5)]
        s["ask_vols"] = [rnd.randint(20, 300) for _ in range(5)]
        vol = s["vol"]
        eng.flow[code] = {"date": today, "outer": vol * 0.6 * outer / 100, "inner": vol * 0.6 * (1 - outer / 100),
                          "since": "09:00:00", "pv": vol * vw, "pv_v": vol, "last_v": vol, "depth": 1.1}
        day["ranks"][code]["since"] = since
        # 分鐘走勢：從開盤價走到現價
        n = 93
        px, x = [], s["open"]
        for i in range(n):
            tgt = s["open"] + (p - s["open"]) * ((i + 1) / n) ** 0.7
            x = x + (tgt - x) * 0.35 + rnd.gauss(0, p * 0.0012)
            px.append([f"{9 + i // 60:02d}:{i % 60:02d}", round(x, 2)])
        px[-1][1] = p
        day["px"][code] = px
    if len(fit) >= 3:
        code = fit[2]
        s = eng.snap[code]
        entry = floor_tick(s["price"] * 0.991)
        stop, target = dt.levels(entry, s["open"], dt.vwap(code, s), s["low"], False)
        dt.positions.append({"id": "pdemo", "code": code, "name": eng.universe[code]["name"], "etf": False, "date": today,
                             "time": "09:48", "entry": entry, "lots": 4, "stop": stop, "target": target, "score": 66, "rank": 3})
        day["akeys"].append(f"entry:{code}")
        day["alerts"].append({"id": f"{today}-1", "ts": time.time() - 2700, "time": "09:46", "kind": "entry", "code": code,
                              "title": f"可進場：{eng.universe[code]['name']} {code}",
                              "body": f"現價 {entry}｜停損 {stop}｜目標 {target}｜最多 6 張"})
        day["seq"] = 1
    for cp in CHECKPOINTS[:3]:
        picks = []
        for k, code in enumerate(eng.live_cands[:10]):
            a = dt.advise(code, rank=k + 1)
            if a["status"] != "nodata":
                picks.append({"rank": k + 1, "code": code, "name": a["name"], "score": a.get("score"), "etf": False,
                              "price": a["price"], "entry": a["entry"], "stop": a["stop"], "target": a["target"],
                              "adv": a["status"], "fit": a["fit"], "hi": a["price"], "lo": a["price"], "hit": None})
        day["snaps"][cp] = {"time": cp, "picks": picks}


def intraday(eng, code, size):
    """示範用的分時 K 線：把每天的日 K 拆成幾根，走勢在當天的高低點之間、從開盤價走到收盤價。"""
    import zlib
    nb = 240 // size + 1
    days = 22 if size == 30 else 62
    with eng.lock:
        daily = list(eng.merged_bars(code) or [])[-days:]
    rnd = random.Random(zlib.crc32(f"{code}{size}".encode()))
    w = [2.2, 1.3, 1.0, 0.8, 0.7, 0.7, 0.8, 1.1, 1.8] if size == 30 else [3.5, 1.8, 1.4, 1.5, 2.6]
    today = today_iso()
    now = eng.clock()
    out = []
    for b in daily:
        d, o, h, l, c, v = b[:6]
        n = nb
        if d == today and eng.demo_live:
            n = min(nb, max(1, (now.hour * 60 + now.minute - 540) // size + 1))
        steps = n * 4
        first, second = sorted(rnd.sample(range(1, steps), 2)) if steps > 2 else (1, 1)
        lo_at, hi_at = (first, second) if c >= o else (second, first)   # 上漲日先低後高，下跌日先高後低
        path, x = [o], o
        for k in range(1, steps + 1):
            tgt = o + (c - o) * k / steps
            x = min(h, max(l, x + (tgt - x) * 0.4 + rnd.gauss(0, (h - l) * 0.08)))
            if n == nb:
                if k == hi_at:
                    x = h
                elif k == lo_at:
                    x = l
            path.append(x)
        path[-1] = c
        ws = w[:n]
        for i in range(n):
            seg = path[i * 4: i * 4 + 5]
            m = 540 + i * size
            out.append([f"{d} {m // 60:02d}:{m % 60:02d}", round(seg[0], 2), round(max(seg), 2), round(min(seg), 2),
                        round(seg[-1], 2), int(v * ws[i] / sum(w))])
    return out
