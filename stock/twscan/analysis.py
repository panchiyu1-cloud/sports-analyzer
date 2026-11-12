"""評分模型：技術指標、趨勢型態、量價、內外盤、洗盤／出貨、籌碼、新聞 → 0~100 分。

每一個訊號都有固定加減分，前端會完整列出「評分拆解」，讓你看得到分數怎麼來的。
第 i 天的評估只使用第 i 天（含）以前的資料，因此可以拿來做歷史回測。
"""
from . import indicators as ind
from .util import clamp, r2

MIN_BARS = 81  # 至少要約 4 個月資料才評分


def compute_features(bars):
    d = [b[0] for b in bars]
    o = [float(b[1]) for b in bars]
    h = [float(b[2]) for b in bars]
    l = [float(b[3]) for b in bars]
    c = [float(b[4]) for b in bars]
    v = [float(b[5] or 0) for b in bars]
    F = {"d": d, "o": o, "h": h, "l": l, "c": c, "v": v, "n": len(c)}
    for n in (5, 10, 20, 60, 120):
        F[f"ma{n}"] = ind.sma(c, n)
    F["vma5"], F["vma20"] = ind.sma(v, 5), ind.sma(v, 20)
    F["dif"], F["dea"], F["osc"] = ind.macd(c)
    F["k"], F["kd_d"] = ind.kd(h, l, c)
    F["rsi"] = ind.rsi(c)
    F["bb_mid"], F["bb_up"], F["bb_lo"], F["bb_bw"] = ind.bollinger(c)
    F["obv"] = ind.obv(c, v)
    F["pdi"], F["mdi"], F["adx"], F["atr"] = ind.dmi(h, l, c)
    F["hh20"], F["hh60"] = ind.rolling_prev_max(h, 20), ind.rolling_prev_max(h, 60)
    F["ll20"] = ind.rolling_prev_min(l, 20)
    F["piv_h"], F["piv_l"] = ind.pivots(h, l, 3)
    return F


def _ok(*xs):
    return all(x is not None for x in xs)


def _cross_up(a, b, i, look):
    for j in range(i, max(i - look, 0), -1):
        if _ok(a[j - 1], b[j - 1], a[j], b[j]) and a[j - 1] <= b[j - 1] and a[j] > b[j]:
            return j
    return None


def _cross_down(a, b, i, look):
    for j in range(i, max(i - look, 0), -1):
        if _ok(a[j - 1], b[j - 1], a[j], b[j]) and a[j - 1] >= b[j - 1] and a[j] < b[j]:
            return j
    return None


def _mean(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else 0.0


def _fmt(p):
    return f"{p:,.2f}".rstrip("0").rstrip(".")


# ---------------------------------------------------------------------- 洗盤
def detect_wash(F, i):
    """多頭回檔量縮、守住支撐 → 疑似主力洗盤。"""
    c, o, h, l, v = F["c"], F["o"], F["h"], F["l"], F["v"]
    ma20, ma60 = F["ma20"], F["ma60"]
    if i < 80 or not _ok(ma20[i], ma60[i], ma60[i - 20]):
        return None
    p = max(range(i - 15, i - 1), key=lambda j: h[j])          # 近期高點（至少 2 天前）
    hi = h[p]
    lo_before = min(l[max(0, p - 60):p])
    if lo_before <= 0 or hi / lo_before - 1 < 0.15:              # 前段需有 15% 以上漲幅
        return None
    if ma60[i] <= ma60[i - 20]:                                  # 季線需上揚（多頭結構）
        return None
    pull_low = min(l[p + 1:i + 1])
    dd = 1 - pull_low / hi
    if not (0.05 <= dd <= 0.22):                                 # 回檔 5%~22%
        return None
    up_vol = _mean(v[max(0, p - 10):p + 1])
    pb_vol = _mean(v[p + 1:i + 1])
    if up_vol <= 0 or pb_vol > 0.75 * up_vol:                    # 回檔量需明顯萎縮
        return None
    if pull_low < ma60[i] * 0.99 or c[i] <= pull_low:            # 守住季線、未續創低
        return None
    support = "月線" if pull_low <= ma20[i] * 1.02 and c[i] >= ma20[i] * 0.97 else "季線"
    stop = None
    for j in range(i - 2, i + 1):
        body, lower = abs(c[j] - o[j]), min(c[j], o[j]) - l[j]
        if lower >= 2 * body and lower >= 0.015 * c[j]:
            stop = "長下影線"
    if F["vma5"][i - 1] and c[i] > c[i - 1] and v[i] > F["vma5"][i - 1]:
        stop = "量增收漲"
    label = f"疑似洗盤：回檔 {dd * 100:.0f}%、量縮至 {pb_vol / up_vol * 100:.0f}%，守住{support}"
    if stop:
        return {"label": f"{label}，出現止跌訊號（{stop}）", "pts": 9}
    return {"label": label, "pts": 5}


# ---------------------------------------------------------------------- 出貨
def detect_distribution(F, i, ctx=None):
    """高檔 + 爆量不漲、量價/指標背離、長上影、帶量跌破、法人賣超 → 疑似出貨。"""
    c, o, h, l, v, d = F["c"], F["o"], F["h"], F["l"], F["v"], F["d"]
    if i < 80:
        return None
    C = c[i]
    hh120 = max(h[max(0, i - 120):i + 1])
    hi10 = max(h[i - 10:i + 1])
    low60 = min(l[i - 60:i + 1])
    if not (C >= hh120 * 0.88 and low60 > 0 and hi10 / low60 - 1 >= 0.35):
        return None
    vm = F["vma20"]
    ev = []
    for j in range(i - 4, i + 1):
        if vm[j - 1] and v[j] >= 2.0 * vm[j - 1]:
            body, upper = abs(c[j] - o[j]), h[j] - max(c[j], o[j])
            if body / o[j] < 0.015 or (upper >= 2 * body and upper >= 0.02 * c[j]) or c[j] < o[j]:
                ev.append(f"{d[j][5:]} 高檔爆量但收不高")
                break
    ob = F["obv"]
    if max(h[i - 4:i + 1]) >= max(h[i - 25:i - 4]) and ob[i] < max(ob[i - 25:i - 4]):
        ev.append("股價創高但 OBV 未創高（量價背離）")
    r = F["rsi"]
    if _ok(*r[i - 25:i + 1]) and max(h[i - 4:i + 1]) > max(h[i - 25:i - 5]) and max(r[i - 4:i + 1]) < max(r[i - 25:i - 5]) - 5:
        ev.append("股價新高但 RSI 走弱（頂背離）")
    cnt = sum(1 for j in range(i - 4, i + 1)
              if h[j] - max(c[j], o[j]) >= max(2 * abs(c[j] - o[j]), 0.015 * c[j]))
    if cnt >= 2:
        ev.append(f"近 5 日 {cnt} 根長上影線")
    if F["ma10"][i] and C < F["ma10"][i] and vm[i - 1] and v[i] > 1.3 * vm[i - 1] and C < o[i]:
        ev.append("帶量跌破 10 日線")
    chips = (ctx or {}).get("chips")
    if chips and sum(x["total"] for x in chips[-3:]) < 0 and sum(1 for x in chips[-3:] if x["total"] < 0) >= 2:
        ev.append("高檔法人賣超")
    if len(ev) >= 2:
        return {"label": "疑似高檔出貨：" + "、".join(ev[:3]), "pts": -12, "evidence": ev}
    if len(ev) == 1:
        return {"label": "高檔出現出貨跡象：" + ev[0], "pts": -4, "evidence": ev}
    return None


# ---------------------------------------------------------------------- 主評分
def evaluate(F, i=None, ctx=None):
    if i is None:
        i = F["n"] - 1
    if i < MIN_BARS - 1 or i >= F["n"]:
        return None
    ctx = ctx or {}
    S = []

    def add(key, label, pts, kind=None, at=None):
        S.append({"key": key, "label": label, "pts": pts,
                  "kind": kind or ("bull" if pts > 0 else "bear" if pts < 0 else "info"), "at": at})

    c, o, h, l, v, d = F["c"], F["o"], F["h"], F["l"], F["v"], F["d"]
    C = c[i]
    ma5, ma10, ma20, ma60 = F["ma5"][i], F["ma10"][i], F["ma20"][i], F["ma60"][i]
    if not _ok(ma5, ma10, ma20, ma60) or C <= 0:
        return None

    # ---------------- 趨勢
    if ma5 > ma10 > ma20 > ma60:
        add("ma_bull", "均線多頭排列（5>10>20>60 日）", 8)
    elif C > ma20 > ma60:
        add("above_ma", "站上月線與季線", 4)
    if F["ma20"][i] > F["ma20"][i - 5]:
        add("ma20_up", "月線上揚", 3)
    if F["ma60"][i] > F["ma60"][i - 10]:
        add("ma60_up", "季線上揚", 2)
    if C < ma60:
        add("below_ma60", "股價位於季線之下", -6)
    elif C < ma20 and F["ma20"][i] < F["ma20"][i - 5]:
        add("below_ma20", "月線下彎且股價在月線下", -4)
    adx, pdi, mdi = F["adx"][i], F["pdi"][i], F["mdi"][i]
    if _ok(adx, pdi, mdi):
        if adx >= 20 and pdi > mdi and adx > (F["adx"][i - 3] or 0):
            add("adx", f"多方趨勢增強（ADX {adx:.0f}）", 4)
        elif mdi > pdi and adx >= 25:
            add("adx_bear", f"空方趨勢明確（ADX {adx:.0f}）", -4)
    bias20 = (C - ma20) / ma20
    if bias20 > 0.15:
        add("bias20", f"月線正乖離過大（+{bias20 * 100:.0f}%），追高風險", -6)
    elif (C - ma60) / ma60 > 0.35:
        add("bias60", f"季線正乖離過大（+{(C - ma60) / ma60 * 100:.0f}%）", -5)

    # ---------------- 動能
    dif, dea, osc = F["dif"], F["dea"], F["osc"]
    j = _cross_up(dif, dea, i, 3)
    jd = _cross_down(dif, dea, i, 3)
    if j is not None and jd is not None:   # 3 天內來回交叉，只採最新的方向
        j, jd = (j, None) if j > jd else (None, jd)
    if j is not None:
        add("macd_gc", "MACD 黃金交叉" + ("（零軸下，初升段）" if dif[j] < 0 else ""), 6, at=j)
    elif jd is None and dif[i] > 0 and osc[i] > osc[i - 1] > osc[i - 2]:
        add("macd_up", "MACD 多頭動能增強", 3)
    if jd is not None:
        add("macd_dc", "MACD 死亡交叉", -5, at=jd)
    K, D = F["k"], F["kd_d"]
    jk = _cross_up(K, D, i, 3)
    jkd = _cross_down(K, D, i, 3)
    if jk is not None and jkd is not None:
        jk, jkd = (jk, None) if jk > jkd else (None, jkd)
    if jk is not None:
        if min(K[jk - 1], D[jk - 1]) < 30 or K[jk] < 40:
            add("kd_low_gc", "KD 低檔黃金交叉", 6, at=jk)
        elif K[jk] <= 80:
            add("kd_gc", "KD 黃金交叉", 3, at=jk)
    if jkd is not None and K[jkd - 1] > 80:
        add("kd_high_dc", "KD 高檔死亡交叉", -4, at=jkd)
    elif K[i] > 80 and D[i] > 80 and K[i - 1] > 80 and K[i - 2] > 80 and C > ma20:
        add("kd_dull", "KD 高檔鈍化（強勢延續）", 2)
    r = F["rsi"][i]
    if r is not None:
        if r > 82:
            add("rsi_hot", f"RSI 過熱（{r:.0f}）", -5)
        elif 50 <= r <= 75:
            add("rsi_ok", f"RSI 位於強勢區（{r:.0f}）", 3)
        elif r < 40:
            add("rsi_weak", f"RSI 偏弱（{r:.0f}）", -3)
        rr = F["rsi"]
        if r >= 50 and any(_ok(rr[x - 1], rr[x]) and rr[x - 1] < 50 <= rr[x] for x in range(i - 4, i + 1)):
            add("rsi_50", "RSI 向上穿越 50", 2)
    rs = ctx.get("rs_pct")
    if rs is not None:
        if rs >= 0.9:
            add("rs", f"相對強勢：20 日漲幅勝過 {rs * 100:.0f}% 個股", 5)
        elif rs >= 0.75:
            add("rs", f"相對強勢：20 日漲幅勝過 {rs * 100:.0f}% 個股", 3)
        elif rs <= 0.2:
            add("rs_weak", "相對弱勢：20 日漲幅落後大盤多數個股", -3)

    # ---------------- 量價
    if ctx.get("vol_est"):
        add("vol_est", "盤中：今日成交量以「預估全日量」換算後比較", 0, "info")
    vm = F["vma20"][i - 1]
    rng = h[i] - l[i]
    clv = ((C - l[i]) - (h[i] - C)) / rng if rng > 0 else 0.0
    chg = C / c[i - 1] - 1
    if vm and v[i] > 1.8 * vm and C > o[i] and chg > 0.025 and clv > 0.5:
        add("vol_red", f"帶量長紅（量為均量 {v[i] / vm:.1f} 倍）", 7)
    if vm and v[i] > 2 * vm and C < o[i] and chg < -0.03:
        add("vol_black", f"爆量長黑（量為均量 {v[i] / vm:.1f} 倍）", -7)
    if c[i] > c[i - 1] > c[i - 2] > c[i - 3] and v[i] > v[i - 1] > v[i - 2]:
        add("pv_up", "量價齊揚（連 3 日價漲量增）", 3)
    ob = F["obv"]
    if ob[i] >= max(ob[i - 20:i]) and C >= c[i - 20]:
        add("obv_high", "OBV 能量潮創 20 日新高", 3)
    lo20 = min(c[i - 20:i + 1])
    if lo20 > 0 and (max(c[i - 20:i + 1]) - lo20) / lo20 < 0.08 and (ob[i] - ob[i - 20]) > 0.15 * sum(v[i - 20:i + 1]):
        add("obv_acc", "價穩量增吸籌（股價盤整、OBV 上升）", 4)

    # ---------------- 內外盤（盤中用即時追蹤，否則用 K 線位置估算）
    flow = ctx.get("flow")
    if flow and flow.get("total", 0) >= flow.get("min_total", 200):
        ratio = flow["outer"] / flow["total"]
        if ratio >= 0.58:
            add("flow_out", f"外盤比 {ratio * 100:.0f}%（主動買盤積極）", 4)
        elif ratio <= 0.42:
            add("flow_in", f"內盤比 {(1 - ratio) * 100:.0f}%（主動賣壓偏重）", -4)
        dep = flow.get("depth")
        if dep and dep >= 1.5:
            add("depth_bid", f"五檔委買量為委賣 {dep:.1f} 倍", 1)
        elif dep and dep <= 0.67:
            add("depth_ask", f"五檔委賣量為委買 {1 / dep:.1f} 倍", -1)
    else:
        nu = de = 0.0
        for x in range(i - 4, i + 1):
            rg = h[x] - l[x]
            if rg > 0:
                nu += ((c[x] - l[x]) - (h[x] - c[x])) / rg * v[x]
                de += v[x]
        if de > 0:
            est = 0.5 + 0.5 * nu / de
            if est >= 0.62:
                add("flow_est", f"估算外盤比 {est * 100:.0f}%（依近 5 日收盤位置與量）", 2)
            elif est <= 0.38:
                add("flow_est_bear", f"估算內盤比 {(1 - est) * 100:.0f}%（近 5 日多收在低檔）", -2)

    # ---------------- 型態
    hh20, hh60, ll20 = F["hh20"][i], F["hh60"][i], F["ll20"][i]
    box = (hh20 - ll20) / ll20 if _ok(hh20, ll20) and ll20 > 0 else None
    if hh60 and C > hh60 and vm and v[i] > 1.5 * vm:
        add("bo60", "帶量突破 60 日新高", 10)
    elif hh20 and C > hh20 and vm and v[i] > 1.3 * vm:
        if box is not None and box < 0.15:
            add("bo_box", f"箱型整理（{box * 100:.0f}%）後帶量突破", 8)
        else:
            add("bo20", "帶量突破 20 日高點", 7)
    elif hh20 and C > hh20:
        add("bo20_weak", "突破 20 日高點（量能不足）", 3)
    else:
        for x in range(i - 3, i):
            if F["hh60"][x] and c[x] > F["hh60"][x] and F["vma20"][x - 1] and v[x] > 1.5 * F["vma20"][x - 1] \
                    and C >= F["hh60"][x] * 0.98:
                add("bo_hold", f"{d[x][5:]} 突破 60 日高後守穩", 5, at=x)
                break
    bw = F["bb_bw"]
    if i >= 130 and bw[i] is not None:
        hist = sorted(x for x in bw[i - 120:i] if x is not None)
        if len(hist) > 50:
            thr = hist[int(len(hist) * 0.2)]
            recent = [x for x in bw[i - 10:i] if x is not None]
            squeezed = bool(recent) and min(recent) <= thr
            if squeezed and F["bb_up"][i] and C > F["bb_up"][i]:
                add("bb_break", "布林通道壓縮後向上突破", 6)
            elif squeezed and bw[i] <= thr:
                add("bb_squeeze", "布林通道壓縮中（醞釀變盤）", 1, "info")
    m5, m10, m20 = F["ma5"], F["ma10"], F["ma20"]

    def spread(x):
        vals = (m5[x], m10[x], m20[x])
        return (max(vals) - min(vals)) / c[x]
    if spread(i - 5) < 0.02 and m5[i] > m10[i] > m20[i] and spread(i) > spread(i - 5) and C > m5[i]:
        add("ma_expand", "均線糾結後向上發散", 5)
    for x in range(i, i - 3, -1):
        if l[x] > h[x - 1] and min(l[x:i + 1]) > h[x - 1]:
            add("gap_up", f"向上跳空缺口未回補（{d[x][5:]}）", 3, at=x)
            break
    for x in range(i, i - 3, -1):
        if h[x] < l[x - 1] and max(h[x:i + 1]) < l[x - 1]:
            add("gap_dn", f"向下跳空缺口（{d[x][5:]}）", -4, at=x)
            break
    lows = [p for p in F["piv_l"] if p[2] <= i and p[0] >= i - 90]
    if len(lows) >= 2:
        (a, la, _), (b, lb, _) = lows[-2], lows[-1]
        if b - a >= 8 and abs(lb - la) / la <= 0.04:
            neck = max(h[a:b + 1])
            if (neck - min(la, lb)) / neck >= 0.06:
                bo = None
                for x in range(max(b + 1, i - 4), i + 1):
                    if c[x] > neck >= c[x - 1]:
                        bo = x
                if bo is not None and C > neck:
                    add("w_bottom", f"W 底突破頸線（{_fmt(neck)}）", 9, at=bo)
                elif neck * 0.96 <= C <= neck and i > b:
                    add("w_forming", f"W 底成形中，接近頸線 {_fmt(neck)}", 3)
    highs = [p for p in F["piv_h"] if p[2] <= i and p[0] >= i - 90]
    if len(highs) >= 2:
        (a, ha, _), (b, hb, _) = highs[-2], highs[-1]
        if b - a >= 8 and abs(hb - ha) / ha <= 0.03:
            neck = min(l[a:b + 1])
            if C < neck and max(c[max(b, i - 5):i + 1]) >= neck:
                add("m_top", f"M 頭跌破頸線（{_fmt(neck)}）", -10)
    if C < ma20 and c[i - 1] >= F["ma20"][i - 1] and vm and v[i] > 1.3 * vm:
        add("break_ma20", "帶量跌破月線", -5)

    # ---------------- 洗盤／出貨
    wash = detect_wash(F, i)
    if wash:
        add("wash", wash["label"], wash["pts"])
    dist = detect_distribution(F, i, ctx)
    if dist:
        add("dist", dist["label"], dist["pts"])

    # ---------------- 三大法人
    chips = ctx.get("chips")
    if chips:
        def streak(key, sign):
            n = 0
            for x in reversed(chips):
                if x[key] * sign > 0:
                    n += 1
                else:
                    break
            return n
        ct, cf = streak("t", 1), streak("f", 1)
        if ct >= 3:
            add("trust_buy", f"投信連買 {ct} 日", 5)
        if cf >= 3:
            add("foreign_buy", f"外資連買 {cf} 日", 4)
        if streak("t", -1) >= 3:
            add("trust_sell", f"投信連賣 {streak('t', -1)} 日", -3)
        if streak("f", -1) >= 3:
            add("foreign_sell", f"外資連賣 {streak('f', -1)} 日", -3)
        n = min(len(chips), 5)
        tot = sum(x["total"] for x in chips[-n:])
        vol = sum(v[i - n + 1:i + 1])
        if vol > 0:
            pct = tot / vol
            if pct >= 0.05:
                add("insti_buy", f"法人近 {n} 日買超（占成交量 {pct * 100:.0f}%）", 3)
            elif pct <= -0.05:
                add("insti_sell", f"法人近 {n} 日賣超（占成交量 {-pct * 100:.0f}%）", -5)

    # ---------------- 新聞
    news = ctx.get("news")
    if news and news.get("n", 0) > 0:
        pts = round(8 * news.get("score", 0))
        if pts >= 2:
            add("news_pos", f"新聞情緒偏多（近 7 日 {news['n']} 則）", pts)
        elif pts <= -2:
            add("news_neg", f"新聞情緒偏空（近 7 日 {news['n']} 則）", pts)
        if news.get("risk"):
            add("news_risk", f"新聞提及風險字眼：{news['risk']}", -3)

    # ---------------- 大盤
    mk = ctx.get("market")
    if mk == "bull":
        add("mkt_bull", "大盤多頭格局", 2)
    elif mk == "bear":
        add("mkt_bear", "大盤偏弱，訊號可靠度下降", -3)

    raw = sum(s["pts"] for s in S)
    return {
        "score": int(clamp(round(raw), 0, 100)), "raw": raw, "signals": S,
        "flags": {"wash": bool(wash), "dist": bool(dist and dist["pts"] <= -10),
                  "dist_hint": bool(dist and dist["pts"] > -10)},
    }


def grade(score):
    if score >= 70:
        return "強烈上漲訊號"
    if score >= 50:
        return "偏多訊號"
    if score >= 30:
        return "中性"
    return "偏弱"


def key_levels(F, i=None):
    if i is None:
        i = F["n"] - 1
    C = F["c"][i]
    lows = [p[1] for p in F["piv_l"] if p[2] <= i and p[1] < C and p[0] >= i - 60]
    sup_c = [x for x in (F["ma20"][i], lows[-1] if lows else None) if x is not None and x < C]
    support = max(sup_c) if sup_c else None
    highs = [p[1] for p in F["piv_h"] if p[2] <= i and p[1] > C and p[0] >= i - 120]
    resistance = min(highs) if highs else None
    atr = F["atr"][i]
    invalid = None
    if support:
        invalid = support - (atr * 0.5 if atr else support * 0.01)
    return {"support": r2(support), "resistance": r2(resistance), "invalid": r2(invalid),
            "atr_pct": r2(atr / C * 100, 1) if atr else None}


def backtest_points(F, start=None, step=1, horizons=(5, 10)):
    """回傳每個歷史交易日的 (技術分數, 5 日後報酬, 10 日後報酬)。只用技術面（無籌碼、新聞）。"""
    hz = max(horizons)
    start = max(start or 130, MIN_BARS - 1)
    out = []
    c = F["c"]
    for i in range(start, F["n"] - hz, step):
        r = evaluate(F, i)
        if r is None:
            continue
        out.append((r["score"], c[i + horizons[0]] / c[i] - 1, c[i + horizons[1]] / c[i] - 1))
    return out


def chart_payload(F, n=120, full=False, signals=None):
    s = max(0, F["n"] - n)
    bars = [[F["d"][k], F["o"][k], F["h"][k], F["l"][k], F["c"][k], int(F["v"][k])] for k in range(s, F["n"])]
    keys = ["ma5", "ma20", "ma60"] + (["ma10", "bb_up", "bb_lo", "dif", "dea", "osc", "k", "kd_d", "rsi"] if full else [])
    out = {"bars": bars, "ind": {k: [r2(x, 3) for x in F[k][s:]] for k in keys}}
    if signals:
        out["marks"] = [{"i": sg["at"] - s, "label": sg["label"], "kind": sg["kind"]}
                        for sg in signals if sg.get("at") is not None and sg["at"] >= s]
    return out


def tf_summary(F):
    """單一週期（30 分、60 分、日 K 共用）的趨勢摘要：只看均線與動能，不看量（盤中的量有開盤、收盤特別大的固定型態）。"""
    i = F["n"] - 1
    c = F["c"][i]
    pts, score = [], 0

    def add(ok, good, bad):
        nonlocal score
        if ok is None:
            return
        score += 1 if ok else -1
        pts.append({"ok": bool(ok), "text": good if ok else bad})
    ma5, ma20, ma60 = F["ma5"][i], F["ma20"][i], F["ma60"][i]
    prev20 = F["ma20"][i - 3] if i >= 3 else None
    add(None if ma20 is None else c >= ma20, "站上 20 均線", "跌破 20 均線")
    add(None if not _ok(ma5, ma20) else ma5 >= ma20, "5 均線在 20 均線之上", "5 均線在 20 均線之下")
    add(None if not _ok(ma20, prev20) else ma20 >= prev20, "20 均線上揚", "20 均線下彎")
    add(None if F["osc"][i] is None else F["osc"][i] >= 0, "MACD 柱為正", "MACD 柱為負")
    add(None if not _ok(F["k"][i], F["kd_d"][i]) else F["k"][i] >= F["kd_d"][i], "KD 向上", "KD 向下")
    state = "bull" if score >= 3 else "bear" if score <= -3 else "flat"
    return {"state": state, "label": {"bull": "偏多", "bear": "偏空", "flat": "整理"}[state], "score": score, "points": pts,
            "rsi": r2(F["rsi"][i], 0), "above60": None if ma60 is None else c >= ma60}


def mtf_text(mtf):
    """三個週期放在一起看的一句話。"""
    st = {m["tf"]: m.get("state") for m in mtf}
    s30, s60, sd = st.get("30m"), st.get("60m"), st.get("d")
    if None in (s30, s60, sd):
        return "部分週期資料不足，先以有資料的週期為準。"
    if s30 == s60 == sd == "bull":
        return "三個週期都偏多，方向一致。"
    if s30 == s60 == sd == "bear":
        return "三個週期都偏空，方向一致，不適合做多。"
    if sd == "bull" and s30 == "bear":
        return "日線偏多、30 分轉弱：短線正在拉回，等 30 分重新轉強再考慮進場。"
    if sd == "bull" and s30 == "bull":
        return "日線與 30 分都偏多，60 分還在整理：短線順著日線方向。"
    if sd == "bull":
        return "日線偏多，短週期在整理：等 30 分或 60 分表態。"
    if sd == "bear" and s30 == "bull":
        return "日線偏空、30 分偏多：屬於反彈，短線可以看，但不適合久抱。"
    if sd == "bear":
        return "日線偏空，短週期也還沒轉強：先觀望。"
    if s30 == "bull" and s60 == "bull":
        return "日線整理中，30 分與 60 分都偏多：短線轉強，留意能不能帶動日線表態。"
    if s30 == "bear" and s60 == "bear":
        return "日線整理中，30 分與 60 分都偏空：短線轉弱。"
    return "各週期方向不一致，沒有明確趨勢。"
