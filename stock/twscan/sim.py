"""模擬戰（當沖）：用一個假想的帳戶，照「當沖模式」的進場建議自動模擬買賣，累積績效。

不會真的下單。規則固定寫在這裡（DEF），由掃描程式在盤中自動執行：雲端版不需要開著網頁。
流程和差價交易網頁的模擬戰相同：
    觀察中 → 準備中（出現「可進場」，等一分鐘再確認）→ 交易中 → 模擬交易歷史
買賣的價位（進場價、停損價、目標價）直接用當沖模式算出來的那一組，所以這裡的賺賠就是那套判斷的成績。

成交的算法（寧可算得保守）：
    買進：確認當下的賣一價。
    停損：價格碰到停損價就賣；發現時已經跌破，用當時的買一價（比停損價更差）。
    停利：價格碰到目標價，以目標價成交（當作事先掛好的限價單）。
    兩次報價之間同時碰到停損和目標：算停損。
    13:15 還沒出場：用當時的買一價全部賣出（13:25 之後只剩最後一盤集合競價）。
"""
import os
import time

from .daytrade import FEE_RATE, MIN_FEE, TAX_DT, TAX_ETF, fmt_price, hm, trade_pnl
from .util import log, r2, read_json, today_iso, write_json

DEF = {
    "start": 350000,         # 起始資金（元）；可用 config.json 的 sim_start 改，只在建立帳戶時生效
    "risk_pct": 1.0,         # 每筆碰到停損最多賠帳戶餘額的幾 %（已含手續費與證交稅）
    "pos_pct": 50,           # 單筆買進金額不超過帳戶餘額的幾 %（使用者自己的習慣：一次不投入超過一半）
    "max_pos": 3,            # 同時最多幾檔（準備中＋交易中）；買進金額合計不超過帳戶餘額（不借錢、不放大）
    "day_stop_pct": 3.0,     # 當天已實現的虧損達到當天開始時餘額的幾 % → 今天不再進場
    "confirm_sec": 60,       # 出現「可進場」後至少等這麼久，再確認一次才買
    "confirm_max_sec": 300,  # 這麼久都沒能確認 → 取消
    "exit_at": "13:15",      # 還沒出場的部位在這個時間全部賣出
    "stale_sec": 180,        # 報價超過這麼久沒更新 → 不進場
    "cands": 5,              # 觀察名單：即時名單中買得起的前幾名（和當沖模式相同）
    "min_balance": 30000,    # 餘額低於這個數字就停止
}
REASONS = {"target": "到目標價", "stop": "停損", "time": "13:15 收盤前出場", "close": "以收盤價結算", "manual": "手動結束"}
MAX_LOG, MAX_DAYS, MAX_HIST, PATH_KEEP, PATH_PTS = 200, 400, 5000, 150, 60


def costs(entry, exit_, lots, disc, etf=False):
    """(買進手續費, 賣出手續費, 證交稅)，算法和 daytrade.trade_pnl 相同。"""
    buy, sell = entry * 1000 * lots, exit_ * 1000 * lots
    fb = max(MIN_FEE, int(buy * FEE_RATE * disc))
    fs = max(MIN_FEE, int(sell * FEE_RATE * disc))
    tax = int(sell * (TAX_ETF if etf else TAX_DT))
    return fb, fs, tax


def thin(path, n=PATH_PTS):
    """走勢只留 n 個點（頭尾一定留）。"""
    if len(path) <= n:
        return list(path)
    step = (len(path) - 1) / (n - 1)
    return [path[round(k * step)] for k in range(n)]


def stats(trades):
    """一批已結束交易的統計。"""
    n = len(trades)
    if not n:
        return {"n": 0}
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] < 0]
    gw, gl = sum(t["pnl"] for t in wins), -sum(t["pnl"] for t in losses)
    by = {}
    for t in trades:
        b = by.setdefault(t["reason"], {"n": 0, "pnl": 0, "win": 0})
        b["n"] += 1
        b["pnl"] += t["pnl"]
        b["win"] += 1 if t["pnl"] > 0 else 0
    best, worst = max(trades, key=lambda t: t["pnl"]), min(trades, key=lambda t: t["pnl"])
    pick = lambda t: {"pnl": t["pnl"], "name": t["name"], "code": t["code"], "date": t["date"]}  # noqa: E731
    return {
        "n": n, "win": len(wins), "loss": len(losses), "flat": n - len(wins) - len(losses),
        "win_rate": r2(len(wins) / n * 100, 1), "pnl": round(gw - gl), "avg": round((gw - gl) / n),
        "avg_win": round(gw / len(wins)) if wins else None, "avg_loss": round(-gl / len(losses)) if losses else None,
        "pf": r2(gw / gl, 2) if gl > 0 else None, "avg_r": r2(sum(t.get("r") or 0 for t in trades) / n, 2),
        "avg_ret": r2(sum(t.get("ret") or 0 for t in trades) / n, 2),
        "avg_hold": round(sum(t.get("hold_min") or 0 for t in trades) / n),
        "cost": round(sum(t.get("cost") or 0 for t in trades)), "by": by, "best": pick(best), "worst": pick(worst),
    }


class Sim:
    def __init__(self, eng):
        self.eng = eng
        self.lock = eng.lock
        self.st = None
        self.hist = []
        self.watch = []        # 觀察中（每次檢查重算，不存檔）
        self.excluded = 0      # 排名更前面、但帳戶買不起的檔數
        self._last = 0.0
        self._saved = 0.0
        self._dirty = False
        self._stats = (None, None)

    # ------------------------------------------------------------------ 存取
    def _p(self, *parts):
        return self.eng._p("sim", *parts)

    def _token(self):
        return str(self.eng.cfg.get("sim_reset") or "")

    def _new(self):
        start = float(self.eng.cfg.get("sim_start") or DEF["start"])
        n = self.eng.clock()
        return {"v": 1, "rev": 0, "reset": self._token(), "start": start, "created": n.strftime("%Y-%m-%d %H:%M"),
                "balance": start, "peak": start, "max_dd": 0.0, "max_dd_pct": 0.0, "seq": 0, "items": [],
                "day": None, "days": [], "log": [], "paused": False}

    def load(self):
        st = None if self.eng.demo else read_json(self._p("state.json"))
        hist = [] if self.eng.demo else read_json(self._p("hist.json"), [])
        if isinstance(st, dict) and st.get("v") == 1 and st.get("reset") != self._token():
            # config.json 的 sim_reset 改了：舊帳戶另存，重新開始
            tag = self.eng.clock().strftime("%Y%m%d-%H%M")
            for fn in ("state.json", "hist.json"):
                try:
                    os.replace(self._p(fn), self._p(f"old-{tag}-{fn}"))
                except OSError:
                    pass
            log(f"[模擬戰] 設定要求重新開始：舊帳戶另存為 old-{tag}-*.json")
            rev = int(st.get("rev") or 0)
            st, hist = self._new(), []
            st["rev"] = rev + 1          # 版本號接著編，雲端備份的舊帳戶才不會被當成比較新
            self._dirty = True
        if not isinstance(st, dict) or st.get("v") != 1:
            st, hist = self._new(), []
            self._dirty = True           # 新帳戶馬上存檔，建立時間才不會每次啟動都變
        self.st = st
        self.hist = hist if isinstance(hist, list) else []
        for k, v in (("items", []), ("days", []), ("log", []), ("paused", False), ("day", None)):
            st.setdefault(k, v)
        # 兩個檔案分開寫：以交易紀錄為準，把餘額對回來；已經進歷史的單不會留在持倉裡
        done = {t["id"] for t in self.hist}
        st["items"] = [it for it in st["items"] if it["id"] not in done]
        bal = st["start"] + sum(t["pnl"] for t in self.hist)
        if abs(bal - st["balance"]) > 0.5:
            log(f"[模擬戰] 餘額與交易紀錄不一致（{st['balance']:.0f} → {bal:.0f}），以交易紀錄為準")
            st["balance"] = bal
            self._dirty = True
        self._settle_old()
        if self._dirty:
            self._save(force=True)

    def _save(self, force=False, hist=False):
        if self.eng.demo or self.st is None:
            self._dirty = False
            return
        if hist:
            write_json(self._p("hist.json"), self.hist)
        if not force and (not self._dirty or time.time() - self._saved < 20):
            return
        self.st["rev"] = int(self.st.get("rev") or 0) + 1
        self.st["saved"] = self.eng.clock().strftime("%Y-%m-%d %H:%M:%S")
        write_json(self._p("state.json"), self.st)
        self._dirty, self._saved = False, time.time()

    # ------------------------------------------------------------------ 小工具
    def _ts(self, now):
        """經過時間用的秒數。示範模式的時鐘固定在 10:32，改用真實時間。"""
        return time.time() if (self.eng.demo and self.eng.demo_live) else now.timestamp()

    def _disc(self):
        return self.eng.cfg.get("fee_discount", 0.28)

    def _log(self, kind, text, code=None, now=None):
        now = now or self.eng.clock()
        self.st["log"].append({"date": now.strftime("%Y-%m-%d"), "time": now.strftime("%H:%M:%S"), "kind": kind, "text": text, "code": code})
        del self.st["log"][:-MAX_LOG]
        log(f"[模擬戰] {text}")
        self._dirty = True

    def _used(self):
        return sum(it["amount"] for it in self.st["items"])

    def _open(self):
        return [it for it in self.st["items"] if it["stage"] == "open"]

    def _ready(self):
        return [it for it in self.st["items"] if it["stage"] == "ready"]

    @staticmethod
    def _bid(s, price):
        """現在賣得掉的價格：買一價（離成交價太遠就當作沒有）。"""
        bids = s.get("bids") or []
        return bids[0] if bids and 0 < bids[0] <= price and price / bids[0] - 1 < 0.01 else price

    def size(self, entry, stop, etf, cash):
        """最多可買幾張：現金夠、不超過單筆上限、而且碰到停損（含成本）不超過每筆風險上限。
        回傳 (張數, 每筆風險上限金額, 一張碰停損賠多少)。"""
        disc = self._disc()
        bal = self.st["balance"]
        risk = bal * DEF["risk_pct"] / 100
        one = entry * 1000 * (1 + FEE_RATE * disc)
        n = int(min(cash, bal * DEF["pos_pct"] / 100) // one) if one > 0 else 0
        per = (entry - stop) * 1000
        if per > 0:
            n = min(n, int(risk // per))
        while n > 0 and -trade_pnl(entry, stop, n, disc, etf) > risk:
            n -= 1
        return max(0, n), risk, -trade_pnl(entry, stop, 1, disc, etf)

    def watch_codes(self):
        with self.lock:
            return [it["code"] for it in (self.st or {}).get("items", [])]

    # ------------------------------------------------------------------ 每天
    def _day(self, now):
        d = now.strftime("%Y-%m-%d")
        day = self.st.get("day")
        if not day or day["date"] != d:
            self._upsert_day()
            self._settle_old(d)
            day = self.st["day"] = {"date": d, "start": self.st["balance"], "pnl": 0, "n": 0, "win": 0, "codes": [],
                                    "stopped": False, "ran": False, "signals": 0, "cancels": 0}
            self._dirty = True
        return day

    def _upsert_day(self):
        """把今天的結果記進每日紀錄（只記程式有在盤中執行的日子）。"""
        day = self.st.get("day")
        if not day or not day.get("ran"):
            return
        rec = {"date": day["date"], "start": round(day["start"]), "end": round(day["start"] + day["pnl"]),
               "pnl": round(day["pnl"]), "n": day["n"], "win": day["win"], "signals": day.get("signals", 0)}
        days = self.st["days"]
        if days and days[-1]["date"] == rec["date"]:
            if days[-1] != rec:
                days[-1] = rec
                self._dirty = True
        else:
            days.append(rec)
            del days[:-MAX_DAYS]
            self._dirty = True

    def _settle_old(self, today=None):
        """前幾天留下來的單（程式在盤中中斷才會發生）：準備中的取消；交易中的用那一天的日 K 補算。"""
        today = today or today_iso()
        for it in list(self.st["items"]):
            if it["date"] >= today:
                continue
            if it["stage"] == "ready":
                self.st["items"].remove(it)
                self._dirty = True
                continue
            bars = self.eng.merged_bars(it["code"]) or []
            bar = next((b for b in reversed(bars[-30:]) if b[0] == it["date"]), None)
            if bar:
                lo, hi, close = bar[3], bar[2], bar[4]
                if lo <= it["stop"] and lo < it.get("day_lo", lo + 1):
                    px, reason = it["stop"], "stop"
                elif hi >= it["target"] and hi > it.get("day_hi", hi - 1):
                    px, reason = it["target"], "target"
                else:
                    px, reason = close, "close"
            else:
                px, reason = it.get("last") or it["entry"], "close"
            self._close(it, px, reason, approx=True, when=(it["date"], "13:30:00"))

    # ------------------------------------------------------------------ 盤中
    def tick(self):
        """盤中每隔幾秒呼叫一次。"""
        if time.time() - self._last < 5:
            return
        self._last = time.time()
        eng = self.eng
        if self.st is None or not eng.is_live():
            return
        with self.lock:
            now = eng.clock()
            day = self._day(now)
            day["ran"] = True
            for it in self._open():
                self._manage(it, now)
            for it in self._ready():
                self._confirm(it, now)
            self._scan(now)
            self._upsert_day()
            self._save()

    def _snap(self, code, today):
        s = self.eng.snap.get(code) or {}
        return s if s.get("date") == today and s.get("price") else None

    def _manage(self, it, now):
        """交易中：追蹤價格，碰到停損、目標價或時間到就出場。"""
        s = self._snap(it["code"], it["date"])
        if not s:
            return
        price = s["price"]
        hhmm = now.strftime("%H:%M")
        if it["path"] and it["path"][-1][0] == hhmm:
            it["path"][-1][1] = price
        else:
            it["path"].append([hhmm, price])
        lo = hi = price
        # 兩次報價之間的最高、最低：用當日高低價有沒有再創新高／新低來補
        if s.get("low") and s["low"] < it["day_lo"]:
            lo, it["day_lo"] = min(lo, s["low"]), s["low"]
        if s.get("high") and s["high"] > it["day_hi"]:
            hi, it["day_hi"] = max(hi, s["high"]), s["high"]
        it["lo"], it["hi"] = min(it["lo"], lo), max(it["hi"], hi)
        it["last"], it["last_time"], it["est"] = price, s.get("time"), bool(s.get("est"))
        self._dirty = True
        bid = self._bid(s, price)
        locked = bool(s.get("limit_down") and price <= s["limit_down"] and not (s.get("bids") or []))
        if lo <= it["stop"] or now.hour * 60 + now.minute >= hm(DEF["exit_at"]):
            if locked:       # 跌停鎖死賣不掉：留到收盤後以收盤價結算
                if not it.get("locked"):
                    it["locked"] = True
                    self._log("warn", f"{it['name']} {it['code']} 跌停鎖住，賣不掉，等收盤後結算", it["code"], now)
                return
            if lo <= it["stop"]:
                self._close(it, min(it["stop"], bid) if price <= it["stop"] else it["stop"], "stop", now=now)
            else:
                self._close(it, bid, "time", now=now)
        elif hi >= it["target"]:
            self._close(it, it["target"], "target", now=now)

    def _confirm(self, it, now):
        """準備中：等滿一分鐘後，條件還在就用當時的賣一價買進。"""
        waited = self._ts(now) - it["sig_ts"]
        if waited < DEF["confirm_sec"]:
            return
        dt = self.eng.daytrade
        a = dt.advise(it["code"], rank=it.get("rank"), now=now)
        why = None
        if a.get("status") == "go":
            why = self._gate(a, now, skip=it)
            if why is None:
                cash = self.st["balance"] - (self._used() - it["amount"])
                lots, risk, _ = self.size(a["entry"], a["stop"], a["etf"], cash)
                if lots < 1:
                    why = "價格變了，現金或每筆風險上限已經不夠買一張"
                else:
                    self._fill(it, a, lots, now)
                    return
        if why or waited >= DEF["confirm_max_sec"]:
            self.st["items"].remove(it)
            self.st["day"]["cancels"] = self.st["day"].get("cancels", 0) + 1
            self._log("cancel", f"取消 {it['name']} {it['code']}：{why or '等了 ' + str(DEF['confirm_max_sec'] // 60) + ' 分鐘條件都沒有回來（' + (a.get('reason') or '') + '）'}", it["code"], now)

    def _fill(self, it, a, lots, now):
        s = self.eng.snap.get(it["code"]) or {}
        disc = self._disc()
        entry, stop, target = a["entry"], a["stop"], a["target"]
        risk = -trade_pnl(entry, stop, lots, disc, a["etf"])
        it.update(stage="open", time=now.strftime("%H:%M:%S"), ts=self._ts(now), entry=entry, stop=stop, target=target,
                  lots=lots, amount=round(entry * 1000 * lots), risk=round(risk), gain=round(trade_pnl(entry, target, lots, disc, a["etf"])),
                  stop_pct=a.get("stop_pct"), target_pct=a.get("target_pct"), bal_before=round(self.st["balance"]),
                  hi=entry, lo=entry, day_hi=s.get("high") or entry, day_lo=s.get("low") or entry, last=a.get("price") or entry,
                  last_time=s.get("time"), path=[[now.strftime("%H:%M"), a.get("price") or entry]],
                  ctx=self._ctx(a), checks=[c["text"] for c in a.get("checks") or [] if c.get("ok")])
        it["events"].append([it["time"], f"確認條件還在，以賣一價 {fmt_price(entry)} 買進 {lots} 張"])
        self.st["day"]["codes"].append(it["code"])
        self._log("buy", f"買進 {it['name']} {it['code']} {fmt_price(entry)} × {lots} 張（{it['amount']:,} 元）｜停損 {fmt_price(stop)}｜目標 {fmt_price(target)}｜最多賠 {it['risk']:,} 元", it["code"], now)
        self._save(force=True)

    def _ctx(self, a):
        mk = self.eng.daytrade.market()
        return {"rank": a.get("rank"), "score": a.get("score"), "chgp": a.get("chgp"), "gap": a.get("gap"), "ext": a.get("ext"),
                "vwap": a.get("vwap"), "open": a.get("open"), "vol_ratio": a.get("vol_ratio"), "outer": a.get("outer"),
                "stable": a.get("stable"), "spread": a.get("spread"), "tags": a.get("tags") or [],
                "taiex": mk.get("taiex"), "tpex": mk.get("tpex"), "market": mk.get("text")}

    def _close(self, it, px, reason, now=None, approx=False, when=None):
        st = self.st
        disc = self._disc()
        now = now or self.eng.clock()
        date, tm = when or (now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"))
        pnl = round(trade_pnl(it["entry"], px, it["lots"], disc, it.get("etf")))
        fb, fs, tax = costs(it["entry"], px, it["lots"], disc, it.get("etf"))
        unit = it["entry"] - it["stop"]
        bal0 = st["balance"]
        st["balance"] = bal0 + pnl
        st["peak"] = max(st["peak"], st["balance"])
        dd = st["peak"] - st["balance"]
        if dd > st["max_dd"]:
            st["max_dd"], st["max_dd_pct"] = round(dd), r2(dd / st["peak"] * 100, 2)
        it.update(stage="hist", exit=px, exit_date=date, exit_time=tm, reason=reason, reason_text=REASONS.get(reason, reason),
                  pnl=pnl, ret=r2(pnl / it["amount"] * 100, 2), r=r2(pnl / it["risk"], 2) if it.get("risk") else None,
                  cost=fb + fs + tax, fee=fb + fs, tax=tax, hold_min=max(0, hm(tm) - hm(it["time"])),
                  mfe_r=r2((it["hi"] - it["entry"]) / unit, 2) if unit > 0 else None,
                  mae_r=r2((it["lo"] - it["entry"]) / unit, 2) if unit > 0 else None,
                  bal_after=round(st["balance"]), eq_pct=r2(pnl / bal0 * 100, 2) if bal0 else None, approx=approx or None)
        it["path"] = thin(it.get("path") or [])
        it["events"].append([tm, f"{it['reason_text']}：以 {fmt_price(px)} 賣出，{'賺' if pnl > 0 else '賠'} {abs(pnl):,} 元" + ("（程式中斷，用日線補算）" if approx else "")])
        for k in ("day_hi", "day_lo", "sig_ts", "ts", "locked", "est"):
            it.pop(k, None)
        st["items"].remove(it)
        self.hist.append(it)
        del self.hist[:-MAX_HIST]
        for old in self.hist[:-PATH_KEEP]:      # 很久以前的交易不留走勢，檔案才不會越來越大
            old.pop("path", None)
        day = st.get("day")
        if day and day["date"] == it["date"]:
            day["pnl"] += pnl
            day["n"] += 1
            day["win"] += 1 if pnl > 0 else 0
        else:                                   # 補算前幾天的單：改那一天的紀錄，之後每一天的餘額也跟著調整
            for rec in st["days"]:
                if rec["date"] == it["date"]:
                    rec.update(pnl=rec["pnl"] + pnl, n=rec["n"] + 1, win=rec["win"] + (1 if pnl > 0 else 0), end=rec["end"] + pnl)
                elif rec["date"] > it["date"]:
                    rec.update(start=rec["start"] + pnl, end=rec["end"] + pnl)
            if day and day["date"] > it["date"]:
                day["start"] += pnl
        self._stats = (None, None)
        self._log("sell", f"{it['reason_text']}：{it['name']} {it['code']} {fmt_price(it['entry'])} → {fmt_price(px)} × {it['lots']} 張，"
                          f"{'賺' if pnl > 0 else '賠'} {abs(pnl):,} 元（餘額 {st['balance']:,.0f}）", it["code"], now)
        if day and day["date"] == it["date"] and not day["stopped"] and day["pnl"] <= -day["start"] * DEF["day_stop_pct"] / 100:
            day["stopped"] = True
            self._log("warn", f"今天已經賠 {abs(round(day['pnl'])):,} 元（達當天開始時餘額的 {DEF['day_stop_pct']}%），今天不再進場", None, now)
        self._upsert_day()
        self._save(force=True, hist=True)

    def _gate(self, a, now, skip=None):
        """帳戶這一邊的條件。可以下單回傳 None，否則回傳原因。"""
        st, day = self.st, self.st["day"]
        code = a["code"]
        if st["paused"]:
            return "模擬已暫停"
        if st["balance"] < DEF["min_balance"]:
            return "帳戶餘額太少，已停止"
        if day["stopped"]:
            return f"今天已停手（當天虧損達 {DEF['day_stop_pct']}%）"
        if code in day["codes"]:
            return "今天已經做過這一檔"
        others = [it for it in st["items"] if it is not skip]
        if any(it["code"] == code for it in others):
            return "已經在準備中或交易中"
        if len(others) >= DEF["max_pos"]:
            return f"部位已滿（同時最多 {DEF['max_pos']} 檔）"
        s = self.eng.snap.get(code) or {}
        t = s.get("time") or ""
        if len(t) >= 8 and not (self.eng.demo and self.eng.demo_live):
            age = (now.hour * 3600 + now.minute * 60 + now.second) - (int(t[:2]) * 3600 + int(t[3:5]) * 60 + int(t[6:8]))
            if age > DEF["stale_sec"]:
                return f"報價已經 {age // 60} 分鐘沒更新"
        return None

    def _scan(self, now):
        """觀察名單：即時名單裡帳戶買得起的前幾名；條件齊了就進「準備中」。"""
        eng, st = self.eng, self.st
        dt = eng.daytrade
        watch, excl, slots = [], 0, 0
        held = {it["code"] for it in st["items"]}
        for k, code in enumerate(list(getattr(eng, "live_cands", []) or [])[:40]):
            a = dt.advise(code, rank=k + 1, now=now)
            if a.get("status") == "nodata":
                continue
            full, risk, loss1 = self.size(a["entry"], a["stop"], a["etf"], st["balance"])
            if full < 1:
                excl += 1
                continue
            slots += 1                      # 和當沖模式一樣：只看買得起的前幾名
            if slots > DEF["cands"]:
                break
            if code in held:                # 已經在準備中或交易中的，在那兩個分頁看
                continue
            cash = st["balance"] - self._used()
            lots, _, _ = self.size(a["entry"], a["stop"], a["etf"], cash)
            why, ok = a.get("reason"), False
            if a["status"] == "go":
                why = self._gate(a, now)
                if why is None and lots < 1:
                    why = f"現金被其他部位占用，剩 {max(0, cash):,.0f} 元，不夠買一張（{a['entry'] * 1000:,.0f} 元）"
                ok = why is None
            disc = self._disc()
            n = lots or full
            row = {"code": code, "name": a["name"], "rank": a["rank"], "score": a.get("score"), "etf": a["etf"],
                   "price": a["price"], "chgp": a.get("chgp"), "est": a.get("est"), "status": a["status"], "label": a["label"],
                   "kind": a["kind"], "why": why, "entry": a["entry"], "stop": a["stop"], "target": a["target"],
                   "stop_pct": a.get("stop_pct"), "target_pct": a.get("target_pct"), "lots": n, "amount": round(a["entry"] * 1000 * n),
                   "loss": round(trade_pnl(a["entry"], a["stop"], n, disc, a["etf"])), "gain": round(trade_pnl(a["entry"], a["target"], n, disc, a["etf"])),
                   "checks": a.get("checks") or [], "blocked": a["status"] == "go" and not ok}
            if ok:
                self._order(a, lots, now)
                held.add(code)
                continue
            watch.append(row)
        self.watch, self.excluded = watch, excl

    def _order(self, a, lots, now):
        st = self.st
        st["seq"] += 1
        disc = self._disc()
        tm = now.strftime("%H:%M:%S")
        it = {"id": f"s{now:%y%m%d}-{st['seq']}", "code": a["code"], "name": a["name"], "etf": a["etf"], "market": a.get("market"),
              "stage": "ready", "date": now.strftime("%Y-%m-%d"), "sig_time": tm, "sig_ts": self._ts(now), "sig_price": a["price"],
              "rank": a.get("rank"), "score": a.get("score"), "entry": a["entry"], "stop": a["stop"], "target": a["target"],
              "stop_pct": a.get("stop_pct"), "target_pct": a.get("target_pct"), "lots": lots, "amount": round(a["entry"] * 1000 * lots),
              "risk": round(-trade_pnl(a["entry"], a["stop"], lots, disc, a["etf"])), "gain": round(trade_pnl(a["entry"], a["target"], lots, disc, a["etf"])),
              "events": [[tm, f"出現「可進場」（即時名單第 {a.get('rank')} 名），進入準備中"]]}
        st["items"].append(it)
        st["day"]["signals"] = st["day"].get("signals", 0) + 1
        if a["code"] not in self.eng.rt_priority:
            self.eng.rt_priority.insert(0, a["code"])     # 馬上列入高頻更新
        self._log("signal", f"準備買 {a['name']} {a['code']}：現價 {fmt_price(a['entry'])}，預計 {lots} 張，{DEF['confirm_sec']} 秒後確認", a["code"], now)
        self._save(force=True)

    # ------------------------------------------------------------------ 收盤後
    def on_close(self):
        """收盤後呼叫：今天還沒出場的（程式在 13:15 前後中斷、或跌停鎖住）用收盤價結算，並記下今天的結果。"""
        if self.st is None:
            return
        eng = self.eng
        with self.lock:
            if eng.phase() in ("open", "preopen"):
                return
            today = today_iso()
            self._settle_old(today)
            for it in list(self.st["items"]):
                if it["stage"] == "ready":
                    self.st["items"].remove(it)
                    self._dirty = True
                    continue
                s = self._snap(it["code"], today)
                if s:
                    self._close(it, s["price"], "close", when=(today, "13:30:00"))
            self._upsert_day()
            self._save()

    # ------------------------------------------------------------------ 操作（電腦版）
    def action(self, d):
        act = d.get("action")
        with self.lock:
            if act in ("pause", "resume"):
                self.st["paused"] = act == "pause"
                self._log("info", "已暫停：不再開新倉（已經買的照規則出場）" if act == "pause" else "已恢復")
                self._save(force=True)
                return {"ok": True}
            if act == "reset":
                if d.get("confirm") != "reset":
                    return {"error": "請再按一次確認"}
                if not self.eng.demo:
                    tag = self.eng.clock().strftime("%Y%m%d-%H%M")
                    for fn in ("state.json", "hist.json"):
                        try:
                            os.replace(self._p(fn), self._p(f"old-{tag}-{fn}"))
                        except OSError:
                            pass
                rev = int(self.st.get("rev") or 0)
                self.st, self.hist, self.watch, self._stats = self._new(), [], [], (None, None)
                self.st["rev"] = rev + 1
                self._log("info", f"帳戶已重設為 {self.st['start']:,.0f} 元")
                self._save(force=True, hist=True)
                return {"ok": True}
        return {"error": "不支援的操作"}

    # ------------------------------------------------------------------ API
    def _view(self, it, today):
        v = {k: x for k, x in it.items() if k not in ("sig_ts", "ts", "day_hi", "day_lo")}
        if it["stage"] == "open":
            s = self._snap(it["code"], today)
            price = s["price"] if s else it.get("last")
            if price:
                pnl = round(trade_pnl(it["entry"], price, it["lots"], self._disc(), it.get("etf")))
                unit = it["entry"] - it["stop"]
                v.update(price=price, pnl=pnl, ret=r2(pnl / it["amount"] * 100, 2), r=r2(pnl / it["risk"], 2) if it.get("risk") else None,
                         move_r=r2((price - it["entry"]) / unit, 2) if unit > 0 else None)
            v["path"] = thin(it.get("path") or [], 90)
        return v

    def _state(self, live, now):
        st, day = self.st, self.st.get("day") or {}
        t = now.hour * 60 + now.minute
        dt = self.eng.daytrade.settings
        if st["paused"]:
            return "pause", "已暫停"
        if st["balance"] < DEF["min_balance"]:
            return "halt", "餘額太少，已停止"
        if not live:
            ph = self.eng.phase()
            return ("wait", "等開盤") if ph == "preopen" else ("closed", "目前不是交易時間")
        if day.get("date") == now.strftime("%Y-%m-%d") and day.get("stopped"):
            return "daystop", "今天已停手"
        if t < hm(dt["entry_from"]):
            return "wait", f"{dt['entry_from']} 開始找進場點"
        if t >= hm(DEF["exit_at"]):
            return "closed", "今天已全部出場"
        if t >= hm(dt["entry_to"]):
            return "run", f"{dt['entry_to']} 後不再進場" + ("，等出場" if self._open() else "")
        return "run", "運作中"

    def _all_stats(self):
        key = (len(self.hist), self.st["balance"])
        if self._stats[0] != key:
            self._stats = (key, stats(self.hist))
        return self._stats[1]

    def api(self):
        eng = self.eng
        with self.lock:
            if self.st is None:
                return {"on": False}
            st = self.st
            now = eng.clock()
            today = now.strftime("%Y-%m-%d")
            live = eng.is_live()
            day = st.get("day") if (st.get("day") or {}).get("date") == today else None
            opens = [self._view(it, today) for it in self._open()]
            ready = [dict(self._view(it, today), waited=max(0, int(self._ts(now) - it["sig_ts"]))) for it in self._ready()]
            fl = sum(o.get("pnl") or 0 for o in opens)
            used = self._used()
            code, text = self._state(live, now)
            todays = [t for t in self.hist if t["date"] == today]
            days = st["days"][-60:]
            dt = eng.daytrade
            return {
                "on": True, "v": 1, "now": now.strftime("%Y-%m-%d %H:%M:%S"), "date": today, "live": live, "phase": eng.phase(),
                "demo": eng.demo, "created": st["created"], "start": st["start"],
                "account": {"balance": round(st["balance"]), "equity": round(st["balance"] + fl), "float": round(fl),
                            "realized": round(st["balance"] - st["start"]), "ret_pct": r2((st["balance"] + fl) / st["start"] * 100 - 100, 2),
                            "used": round(used), "avail": round(st["balance"] - used), "peak": round(st["peak"]),
                            "max_dd": round(st["max_dd"]), "max_dd_pct": st["max_dd_pct"], "state": code, "state_text": text,
                            "paused": st["paused"]},
                "today": {"pnl": round((day or {}).get("pnl", 0) + fl), "realized": round((day or {}).get("pnl", 0)), "n": (day or {}).get("n", 0),
                          "win": (day or {}).get("win", 0), "signals": (day or {}).get("signals", 0), "cancels": (day or {}).get("cancels", 0),
                          "stopped": bool((day or {}).get("stopped")), "ran": bool((day or {}).get("ran"))},
                "watch": self.watch if live else [], "excluded": self.excluded if live else 0,
                "ready": ready, "open": opens, "trades_today": todays[::-1],
                "stats": self._all_stats(), "days": days[::-1],
                "curve": [[st["created"][:10], round(st["start"])]] + [[d["date"], d["end"]] for d in st["days"][-250:]],
                "days_run": len(st["days"]), "days_traded": sum(1 for d in st["days"] if d["n"]),
                "days_up": sum(1 for d in st["days"] if d["pnl"] > 0), "days_down": sum(1 for d in st["days"] if d["pnl"] < 0),
                "hist_n": len(self.hist), "log": st["log"][-40:][::-1], "market": dt.market() if live else None,
                "rules": {**{k: DEF[k] for k in ("risk_pct", "pos_pct", "max_pos", "day_stop_pct", "confirm_sec", "confirm_max_sec", "exit_at", "cands")},
                          "entry_from": dt.settings["entry_from"], "entry_to": dt.settings["entry_to"],
                          "fee_discount": self._disc(), "tax": TAX_DT * 100, "min_fee": MIN_FEE},
            }

    def hist_api(self):
        with self.lock:
            if self.st is None:
                return {"n": 0, "trades": []}
            return {"n": len(self.hist), "start": self.st["start"], "created": self.st["created"],
                    "trades": self.hist[::-1][:600], "stats": self._all_stats()}
