"""開盤前試撮（08:30–09:00）：蒐集與判讀。

證交所從 08:30 開始接受委託，每 5 秒揭示一次「模擬撮合」的結果（試算成交價、成交張數、最佳五檔），
09:00 才真的開盤成交。這裡把每一檔這 30 分鐘的試撮價量記下來，換算成預估的開盤漲跌，
讓使用者在 09:00 之前就知道昨天選出來的股票今天大概會怎麼開。

要小心的地方（判讀時都會標出來）：
- 試撮單在開盤前可以撤掉，有人會掛大單把試撮價拉高再撤單：量很小、或價格忽高忽低的不能當真。
- 越接近 09:00 越可靠；08:55 之前只能看個方向。
- 開盤前最後一分鐘試撮價波動超過 3.5%，或撤單改單超過三成，該檔會暫緩開盤 2 分鐘。
這些判讀規則還沒有用歷史資料驗證過。開盤後會把「最後的試撮價」和「實際開盤價」對照並存檔，
累積一段時間就能檢查準不準。
"""
import os

from .util import now_tw, r2, read_json, write_json

OPEN_TIME = "09:00:00"
START_MIN, OPEN_MIN, SHOW_UNTIL_MIN = 8 * 60 + 30, 9 * 60, 9 * 60 + 40
RELIABLE_MIN = 8 * 60 + 55          # 這之後的試撮才比較接近真正的開盤
SAVE_MIN = 9 * 60 + 10              # 開盤 10 分鐘後把試撮與實際開盤的對照存檔
THIN_LOTS, THIN_SHARE = 10, 0.005   # 試撮量不到 10 張，或不到近 5 日均量的 0.5%：不能當真
UNSTABLE_PCT = 2.0                  # 最近 5 分鐘試撮價的高低差（占昨收的 %）到這個程度算「忽高忽低」
OK_MIN_PCT, WEAK_PCT = 0.3, -1.0    # 預估開高 0.3% 以上算小幅開高；開低 1% 以上算開低
STRONG_POOL, STRONG_MAX = 80, 10    # 「試撮轉強」從模型分數前 80 名裡挑，最多列 10 檔


def judge(gap, vol, avg5, swing, chase, limit=None):
    """把一檔的試撮狀況變成一句話。回傳 (代號, 文字, 顏色)。

    gap：預估開盤漲跌 %（試撮價 ÷ 昨收）；vol：試撮張數；avg5：近 5 日平均成交張數；
    swing：最近 5 分鐘試撮價的高低差 %；chase：開高超過這個 % 就不追（和盤中追蹤同一個設定）。
    """
    if gap is None:
        return "none", "還沒有試撮價", "info"
    if limit == "up":
        return "limit", "漲停，買不到", "warn"
    if limit == "down":
        return "limit", "跌停", "bear"
    if vol is None or vol < THIN_LOTS or (avg5 and vol < THIN_SHARE * avg5):
        return "thin", "量太少，不準", "info"
    if swing is not None and swing >= UNSTABLE_PCT:
        return "unstable", "價格忽高忽低", "warn"
    if gap >= chase:
        return "chase", f"開高 {gap:.1f}%，勿追", "warn"
    if gap >= OK_MIN_PCT:
        return "ok", "小幅開高，可留意", "bull"
    if gap > WEAK_PCT:
        return "flat", "平盤附近", "info"
    return "weak", "開低，先觀望", "bear"


class PreOpen:
    def __init__(self, eng):
        self.eng = eng
        self.date = None
        self.rows = {}        # 代號 -> 今天的試撮紀錄
        self.samples = {}     # 代號 -> 幾筆證交所原始欄位（核對用）
        self.first_at = self.last_at = None
        self.saved = None     # 已存檔的日期

    # ---------------------------------------------------------------- 蒐集
    def feed(self, q, now=None):
        """每收到一筆即時報價就呼叫一次（在引擎的鎖裡）。只收今天 08:30–09:00、還沒開盤的試撮資料。"""
        now = now or now_tw()
        hm = now.hour * 60 + now.minute
        if now.weekday() >= 5 or not (START_MIN <= hm < OPEN_MIN):
            return
        t = q.get("time") or ""
        today = now.date().isoformat()
        if not t or t >= OPEN_TIME or (q.get("date") and q["date"] != today):
            return        # 昨天留下來的報價（時間是 13:30 之類），或這一檔已經開盤
        if self.date != today:
            self.date, self.rows, self.samples, self.first_at = today, {}, {}, None
        code = q["code"]
        if q.get("raw") is not None:
            self._sample(code, hm, q["raw"])
        prev = q.get("prev")
        if not prev:
            return
        r = self.rows.get(code)
        if r is None:
            r = self.rows[code] = {"prev": prev, "pts": [], "n": 0, "first": None, "p": None, "v": None}
        r["n"] += 1
        r["t"] = t
        r["lu"], r["ld"] = q.get("limit_up"), q.get("limit_down")
        r["bv"], r["av"] = sum(q.get("bid_vols") or []), sum(q.get("ask_vols") or [])
        price = q.get("trial") or q.get("price")
        if price and price > 0:
            r["p"] = price
            r["v"] = q.get("trial_vol") if q.get("trial_vol") is not None else q.get("tick_vol")
            if r["first"] is None:
                r["first"] = [t, price]
            pts = r["pts"]            # 每分鐘一筆：[分鐘, 最後價, 最高, 最低]
            if pts and pts[-1][0] == hm:
                pts[-1][1] = price
                pts[-1][2] = max(pts[-1][2], price)
                pts[-1][3] = min(pts[-1][3], price)
            else:
                pts.append([hm, price, price, price])
        else:
            r["p"] = None             # 這一刻買賣沒有交集：沒有試撮價
        if self.first_at is None:
            self.first_at = now
        self.last_at = now

    def _sample(self, code, hm, raw):
        """留三筆原始資料：第一筆、08:45 之後的第一筆、最新一筆。"""
        s = self.samples.setdefault(code, [])
        rec = {"hm": f"{hm // 60:02d}:{hm % 60:02d}", "raw": {k: raw.get(k) for k in sorted(raw)}}
        if not s:
            s.append(rec)
        elif len(s) == 1:
            if hm >= 8 * 60 + 45:
                s.append(rec)
        elif len(s) == 2:
            s.append(rec)
        else:
            s[2] = rec

    # ---------------------------------------------------------------- 判讀
    def _avg5(self, code):
        bars = self.eng.hist.get(code) or []
        vols = [b[5] for b in bars[-5:] if len(b) > 5 and b[5]]
        return sum(vols) / len(vols) / 1000 if vols else None    # 歷史資料的成交量是股數，換成張

    def _item(self, code, hm, chase, rank=None, score=None):
        eng = self.eng
        r = self.rows.get(code) or {}
        u = eng.universe.get(code) or {}
        s = eng.snap.get(code) or {}
        prev, p, v = r.get("prev"), r.get("p"), r.get("v")
        gap = (p / prev - 1) * 100 if p and prev else None
        avg5 = self._avg5(code)
        recent = [x for x in r.get("pts") or [] if x[0] >= hm - 5]
        swing = (max(x[2] for x in recent) - min(x[3] for x in recent)) / prev * 100 if recent and prev else None
        first = r.get("first")
        limit = "up" if p and r.get("lu") and p >= r["lu"] else "down" if p and r.get("ld") and p <= r["ld"] else None
        st = judge(gap, v, avg5, swing, chase, limit)
        it = {"code": code, "name": u.get("name") or code, "rank": rank, "score": score, "prev": prev, "trial": p,
              "gap": r2(gap), "vol": v, "vol_pct": r2(v / avg5 * 100, 1) if v is not None and avg5 else None,
              "swing": r2(swing), "move": r2((p - first[1]) / prev * 100) if p and first and prev else None,
              "time": r.get("t"), "status": st[0], "label": st[1], "kind": st[2]}
        if self.date and s.get("date") == self.date and s.get("open") and prev:   # 已經開盤：附上實際開盤價
            it["open"] = s["open"]
            it["open_gap"] = r2((s["open"] / prev - 1) * 100)
        return it

    def board(self, now=None):
        """網頁「開盤前」區塊要的資料。"""
        eng = self.eng
        now = now or now_tw()
        today, hm = now.date().isoformat(), now.hour * 60 + now.minute
        chase = eng.cfg.get("chase_gap_pct", 3.0)
        with eng.lock:
            have = self.date == today and bool(self.rows)
            if now.weekday() < 5 and START_MIN <= hm < OPEN_MIN:
                phase = "collect"                       # 正在蒐集試撮
            elif have and OPEN_MIN <= hm < SHOW_UNTIL_MIN:
                phase = "opened"                        # 剛開盤：對照實際開盤價
            else:
                phase = "done" if have else "none"
            out = {"date": today, "phase": phase, "now": now.strftime("%H:%M:%S"), "reliable": hm >= RELIABLE_MIN,
                   "chase_gap": chase, "universe": len(eng.universe), "n": 0, "picks": [], "strong": []}
            if not have:
                return out
            close = eng.close_payload or {}
            top = close.get("top") or []
            picks = [self._item(c["code"], hm, chase, rank=c.get("rank"), score=c.get("score")) for c in top]
            seen = {c["code"] for c in top}
            ranked = sorted(((c, r) for c, r in eng.analysis.items() if c not in seen and eng._eligible(c, r)),
                            key=lambda x: (x[1]["score"], x[1]["raw"]), reverse=True)[:STRONG_POOL]
            strong = []
            for code, r in ranked:
                it = self._item(code, hm, chase, score=r.get("score"))
                if it["status"] == "ok":
                    strong.append(it)
                    if len(strong) >= STRONG_MAX:
                        break
            gaps, diffs = [], []
            for code, r in self.rows.items():
                if (eng.universe.get(code) or {}).get("type") != "stock" or not r.get("p"):
                    continue
                gaps.append((r["p"] / r["prev"] - 1) * 100)
                s = eng.snap.get(code) or {}
                if s.get("date") == today and s.get("open"):
                    diffs.append(abs(s["open"] / r["p"] - 1) * 100)
            gaps.sort()
            out.update(
                n=sum(1 for r in self.rows.values() if r.get("p")), seen=len(self.rows),
                first=self.first_at.strftime("%H:%M:%S") if self.first_at else None,
                last=self.last_at.strftime("%H:%M:%S") if self.last_at else None,
                list_date=close.get("date"), picks=picks, strong=strong,
                mood={"up": sum(1 for g in gaps if g > 0.1), "down": sum(1 for g in gaps if g < -0.1),
                      "flat": sum(1 for g in gaps if -0.1 <= g <= 0.1),
                      "median": r2(gaps[len(gaps) // 2]) if gaps else None},
                samples=[{"code": c, **x} for c, lst in sorted(self.samples.items()) for x in lst])
            if diffs:      # 最後的試撮價和實際開盤價差多少（檢查試撮準不準）
                out["check"] = {"n": len(diffs), "avg": r2(sum(diffs) / len(diffs)),
                                "within": r2(sum(1 for d in diffs if d <= 0.5) / len(diffs) * 100, 0)}
        if phase in ("opened", "done") and hm >= SAVE_MIN and self.saved != today and out.get("check") and not eng.demo:
            self._save(out)
        return out

    def _save(self, out):
        """每天存一份「試撮 vs 實際開盤」的對照（小檔），之後才能回頭檢查判讀規則準不準。"""
        try:
            path = self.eng._p("preopen", f"{out['date']}.json")
            keep = {k: out.get(k) for k in ("date", "n", "first", "last", "list_date", "mood", "check", "picks", "strong")}
            write_json(path, keep)
            self.saved = out["date"]
            folder = os.path.dirname(path)
            for f in sorted(x for x in os.listdir(folder) if x.endswith(".json"))[:-120]:
                os.remove(os.path.join(folder, f))
        except OSError:
            pass

    def history(self, n=20):
        """最近幾天存下來的對照（由新到舊）。"""
        folder = self.eng._p("preopen")
        try:
            files = sorted((f for f in os.listdir(folder) if f.endswith(".json")), reverse=True)[:n]
        except OSError:
            return []
        return [d for d in (read_json(os.path.join(folder, f)) for f in files) if d]
