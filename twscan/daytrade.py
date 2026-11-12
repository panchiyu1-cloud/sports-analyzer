"""當沖模式：盤中進場建議、提醒通知、持倉與交易紀錄、盤中名單驗證（盤中復盤）。

只用「當下」拿得到的資料判斷（即時報價、估算的當日均價、盤中評分、排名穩定度）。
盤中名單驗證會在固定時間把即時名單存下來，收盤後對答案，用來檢驗這些建議到底有沒有用。
"""
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request

from .util import log, r2, read_json, today_iso, write_json

FEE_RATE = 0.001425
TAX_DT = 0.0015        # 現股當沖證交稅減半（適用至 2027-12-31）
TAX_STOCK = 0.003
TAX_ETF = 0.001
MIN_FEE = 20           # 每筆最低手續費（一般券商規定，實際以對帳單為準）
CHECKPOINTS = ("09:05", "09:30", "10:00", "11:00")
SNAP_WINDOW = 5        # 檢查點之後幾分鐘內都可以補存
STOP_MIN, STOP_MAX = 0.008, 0.025   # 停損距離下限／上限
RR = 2.0               # 目標價＝進場價＋停損距離 × 2
STABLE_MIN = 10        # 連續在榜幾分鐘才算穩定
VOL_RATIO_MIN = 1.2
EXT_MAX = 2.0          # 離當日均價超過幾 % 視為追高
CHG_MAX = 7.0          # 當日漲幅超過幾 % 不追
SPREAD_MAX = 0.5       # 買賣價差超過幾 % 不做
MARKET_WEAK = -1.0     # 指數跌超過幾 % 視為大盤偏弱
DEFAULTS = {"budget": 390000, "risk": 5000, "notify": True, "ntfy_topic": "",
            "entry_from": "09:10", "entry_to": "12:00", "top_n": 5}
TOPIC_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")


def hm(t):
    return int(t[:2]) * 60 + int(t[3:5])


def tick_size(p, etf=False):
    """臺股升降單位。"""
    if etf:
        return 0.01 if p < 50 else 0.05
    if p < 10:
        return 0.01
    if p < 50:
        return 0.05
    if p < 100:
        return 0.1
    if p < 500:
        return 0.5
    if p < 1000:
        return 1.0
    return 5.0


def floor_tick(p, etf=False):
    t = tick_size(p, etf)
    return round(math.floor(p / t + 1e-6) * t, 2)


def ceil_tick(p, etf=False):
    t = tick_size(p, etf)
    return round(math.ceil(p / t - 1e-6) * t, 2)


def trade_pnl(entry, exit_, lots, disc, etf=False, daytrade=True):
    """一筆買賣扣掉手續費與證交稅後的損益（元）。"""
    buy, sell = entry * 1000 * lots, exit_ * 1000 * lots
    fb = max(MIN_FEE, math.floor(buy * FEE_RATE * disc))
    fs = max(MIN_FEE, math.floor(sell * FEE_RATE * disc))
    tax = math.floor(sell * (TAX_ETF if etf else (TAX_DT if daytrade else TAX_STOCK)))
    return sell - buy - fb - fs - tax


def net_ret(entry, exit_, disc, etf=False):
    """當沖報酬率（%，扣成本、不計最低手續費），用在盤中名單驗證。"""
    fee = FEE_RATE * disc
    tax = TAX_ETF if etf else TAX_DT
    return (exit_ * (1 - fee - tax) - entry * (1 + fee)) / (entry * (1 + fee)) * 100


def fmt_price(p):
    if p is None:
        return "—"
    return f"{p:.2f}" if p < 100 else (f"{p:.1f}" if p < 1000 else f"{p:.0f}")


class DayTrade:
    def __init__(self, eng):
        self.eng = eng
        self.lock = eng.lock
        self.settings = dict(DEFAULTS)
        self.positions, self.trades = [], []
        self.day = None            # 今天的盤中狀態（排名紀錄、分鐘價、盤中名單、提醒）
        self.history = {}          # 已對完答案的盤中名單：date -> rec
        self.pending = {}          # 還沒對答案的舊日期
        self._last_tick = 0.0
        self._last_save = 0.0
        self._dirty = False
        self._go_seen = {}
        self._excluded = 0

    # ------------------------------------------------------------------ 存取
    def _p(self, *parts):
        return self.eng._p("daytrade", *parts)

    def load(self):
        st = read_json(self.eng._p("settings.json"), {}) or {}
        if isinstance(st.get("daytrade"), dict):
            self.settings.update({k: v for k, v in st["daytrade"].items() if k in DEFAULTS})
        state = read_json(self._p("state.json"), {}) or {}
        self.positions = state.get("positions") or []
        self.trades = state.get("trades") or []
        ddir = self._p("days")
        if os.path.isdir(ddir):
            for fn in sorted(os.listdir(ddir))[-260:]:
                rec = read_json(os.path.join(ddir, fn)) if fn.endswith(".json") else None
                if not isinstance(rec, dict) or not rec.get("date"):
                    continue
                if rec.get("final"):
                    self.history[rec["date"]] = rec
                if rec["date"] == today_iso():
                    self.day = self._norm(rec)
                elif not rec.get("final") and rec.get("snaps"):
                    self.pending[rec["date"]] = rec

    @staticmethod
    def _norm(rec):
        for k, v in (("ranks", {}), ("px", {}), ("snaps", {}), ("alerts", []), ("akeys", []), ("seq", 0)):
            rec.setdefault(k, v)
        return rec

    def _day(self):
        d = today_iso()
        if not self.day or self.day["date"] != d:
            if self.day and self.day.get("snaps") and not self.day.get("final"):
                self.pending[self.day["date"]] = self.day
            self.day = self._norm({"date": d})
            self._go_seen = {}
        return self.day

    def _save_state(self):
        if not self.eng.demo:
            write_json(self._p("state.json"), {"positions": self.positions, "trades": self.trades})

    def _save_day(self, rec=None, force=False):
        rec = rec or self.day
        if not rec or self.eng.demo:
            return
        if rec is self.day:
            if not force and (not self._dirty or time.time() - self._last_save < 60):
                return
            self._dirty = False
            self._last_save = time.time()
        write_json(self._p("days", f"{rec['date']}.json"), rec)

    # ------------------------------------------------------------------ 即時指標
    def vwap(self, code, s):
        """當日均價（估算）：用每次輪詢到的成交量增量 × 當時成交價累計。"""
        f = self.eng.flow.get(code) or {}
        if f.get("date") == s.get("date") and f.get("pv") and f.get("pv_v"):
            return f["pv"] / f["pv_v"]
        vals = [x for x in (s.get("open"), s.get("high"), s.get("low"), s.get("price")) if x]
        return sum(vals) / len(vals) if vals else None

    def stable_minutes(self, code, now):
        r = (self._day()["ranks"]).get(code)
        if not r or not r.get("since"):
            return 0
        return max(0, now.hour * 60 + now.minute - hm(r["since"]))

    def _index_chg(self, key):
        ix = self.eng.index.get(key) or {}
        if ix.get("price") and ix.get("prev") and ix.get("date") == today_iso():
            return (ix["price"] / ix["prev"] - 1) * 100
        return None

    def market(self):
        t, o = self._index_chg("TAIEX"), self._index_chg("TPEX")
        vals = [x for x in (t, o) if x is not None]
        if not vals:
            light, text = "info", "尚無指數資料"
        elif min(vals) <= MARKET_WEAK:
            light, text = "bear", "大盤偏弱，今天建議少做或不做"
        elif max(vals) < 0:
            light, text = "warn", "大盤小跌，挑條件最好的做"
        else:
            light, text = "bull", "大盤沒有拖累，可以做"
        return {"taiex": r2(t), "tpex": r2(o), "light": light, "text": text}

    def sizing(self, entry, stop, etf):
        """依當沖額度與每筆停損金額算最多可買幾張。回傳 (張數, 額度可買張數)。"""
        budget, risk = float(self.settings["budget"]), float(self.settings["risk"])
        disc = self.eng.cfg.get("fee_discount", 0.28)
        lots_b = int(budget // (entry * 1000)) if entry > 0 else 0
        per = (entry - stop) * 1000
        n = min(lots_b, int(risk // per) if per > 0 else lots_b)
        while n > 0 and -trade_pnl(entry, stop, n, disc, etf) > risk:
            n -= 1
        return max(0, n), lots_b

    def levels(self, entry, op, vw, low, etf, lim_up=None):
        """停損價：開盤價與當日均價較高者再往下一檔（距離限制在 0.8%～2.5%）；目標價：停損距離的 2 倍。"""
        t = tick_size(entry, etf)
        refs = [x for x in (op, vw) if x and x <= entry]
        base = max(refs) if refs else (low or entry)
        stop = floor_tick(base - t * 0.999, etf)
        dist = (entry - stop) / entry
        if dist < STOP_MIN:
            stop = floor_tick(entry * (1 - STOP_MIN), etf)
        elif dist > STOP_MAX:
            stop = ceil_tick(entry * (1 - STOP_MAX), etf)
        if stop >= entry:
            stop = floor_tick(entry - t * 0.999, etf)
        target = floor_tick(entry + RR * (entry - stop), etf)
        if lim_up and target >= lim_up:
            target = floor_tick(lim_up - tick_size(lim_up, etf) * 0.999, etf)
        return stop, max(target, entry)

    def advise(self, code, rank=None, now=None):
        """單一股票的進場建議。"""
        eng = self.eng
        now = now or eng.clock()
        u = eng.universe.get(code) or {}
        s = eng.snap.get(code) or {}
        a = eng.analysis.get(code) or {}
        etf = u.get("type") == "etf"
        out = {"code": code, "name": u.get("name") or code, "rank": rank, "score": a.get("score"),
               "market": u.get("market"), "etf": etf, "fit": True}
        price, prev, op = s.get("price"), s.get("prev"), s.get("open")
        if s.get("date") != today_iso() or not price or not prev or not op:
            out.update(status="nodata", label="尚未成交", kind="info", reason="今天還沒有成交", checks=[])
            return out
        vw = self.vwap(code, s) or price
        gap = (op / prev - 1) * 100
        chg = (price / prev - 1) * 100
        ext = (price / vw - 1) * 100
        lim_up = s.get("limit_up")
        at_limit = bool(lim_up and price >= lim_up)
        bids, asks = s.get("bids") or [], s.get("asks") or []
        spread = (asks[0] - bids[0]) / price * 100 if bids and asks and asks[0] > bids[0] > 0 else None
        f = eng.flow_ctx(code)
        outer = f["outer"] / f["total"] * 100 if f and f["total"] > 0 and f["total"] >= f["min_total"] else None
        bars = [b for b in (eng.hist.get(code) or [])[-21:] if b[0] < today_iso()][-20:]
        avg20 = sum(b[5] for b in bars) / len(bars) / 1000 if bars else None
        vr = (s.get("vol") or 0) / eng.volume_fraction() / avg20 if avg20 else None
        st_min = self.stable_minutes(code, now)
        mkey = "TPEX" if u.get("market") == "TPEx" else "TAIEX"
        mchg = self._index_chg(mkey)

        entry = asks[0] if asks and abs(asks[0] / price - 1) < 0.01 else price
        stop, target = self.levels(entry, op, vw, s.get("low"), etf, lim_up)
        lots, lots_b = self.sizing(entry, stop, etf)
        disc = eng.cfg.get("fee_discount", 0.28)

        chase_gap = float(eng.cfg.get("chase_gap_pct", 3.0))
        above_o, above_v = price >= op, price >= vw
        checks = []
        if above_o and above_v:
            checks.append({"k": "pos", "ok": True, "text": "站上開盤價與當日均價"})
        elif not above_o and not above_v:
            checks.append({"k": "pos", "ok": False, "text": "跌破開盤價與當日均價，走勢轉弱"})
        elif not above_o:
            checks.append({"k": "pos", "ok": False, "text": f"低於開盤價 {fmt_price(op)}，等站回再說"})
        else:
            checks.append({"k": "pos", "ok": False, "text": f"低於當日均價 {fmt_price(vw)}，等站回再說"})
        hard = None
        if at_limit:
            hard = "漲停鎖住，買不到也不要追"
        elif gap >= chase_gap:
            hard = f"開盤跳空 {gap:+.1f}%，過大不追"
        elif chg >= CHG_MAX:
            hard = f"已漲 {chg:+.1f}%，離漲停太近"
        if hard:
            checks.append({"k": "chase", "ok": False, "text": hard})
        elif ext >= EXT_MAX:
            pull = floor_tick(vw * (1 + EXT_MAX / 100), etf)
            checks.append({"k": "chase", "ok": False, "text": f"離均價 {ext:+.1f}% 太遠，等拉回到 {fmt_price(pull)} 以下"})
        else:
            checks.append({"k": "chase", "ok": True, "text": f"沒有追高（跳空 {gap:+.1f}%、離均價 {ext:+.1f}%）"})
        if vr is None:
            checks.append({"k": "vol", "ok": None, "text": "量能資料不足"})
        elif vr >= VOL_RATIO_MIN:
            checks.append({"k": "vol", "ok": True, "text": f"預估量是均量的 {vr:.1f} 倍"})
        else:
            checks.append({"k": "vol", "ok": False, "text": f"預估量只有均量的 {vr:.1f} 倍"})
        if outer is None:
            checks.append({"k": "flow", "ok": None, "text": "外盤比資料累積中"})
        elif outer >= 50:
            checks.append({"k": "flow", "ok": True, "text": f"外盤比 {outer:.0f}%，買盤較積極"})
        else:
            checks.append({"k": "flow", "ok": False, "text": f"外盤比 {outer:.0f}% 偏低"})
        if st_min >= STABLE_MIN:
            checks.append({"k": "stable", "ok": True, "text": f"已連續在榜 {st_min} 分鐘"})
        else:
            checks.append({"k": "stable", "ok": False, "text": f"剛進榜 {st_min} 分鐘，還不穩定"})
        if mchg is not None and mchg <= MARKET_WEAK:
            checks.append({"k": "mkt", "ok": False, "text": f"{'櫃買' if mkey == 'TPEX' else '加權'}指數 {mchg:+.1f}%，大盤偏弱"})

        t_now = now.hour * 60 + now.minute
        fails = [c for c in checks if c["ok"] is False]
        if hard:
            status, label, kind, reason = "avoid", "不要追", "info", hard
        elif not above_o and not above_v:
            status, label, kind, reason = "avoid", "走弱", "bear", checks[0]["text"]
        elif spread is not None and spread > SPREAD_MAX + 0.01:   # 一檔的價差最大剛好 0.5%，超過代表掛單稀疏
            status, label, kind, reason = "avoid", "不適合", "info", f"買賣價差 {spread:.2f}% 太大，進出都吃虧"
        elif t_now >= hm(self.settings["entry_to"]):
            status, label, kind = "late", "今天不再進場", "info"
            reason = f"已過 {self.settings['entry_to']}，當沖剩餘時間不足"
        elif t_now < hm(self.settings["entry_from"]):
            status, label, kind = "wait", "等一下", "warn"
            reason = f"{self.settings['entry_from']} 前波動大，先觀察"
        elif fails:
            status, label, kind, reason = "wait", "等一下", "warn", fails[0]["text"]
        else:
            status, label, kind, reason = "go", "可進場", "bull", "條件都符合"
        sig = sorted((x for x in a.get("signals", []) if x.get("pts", 0) > 0), key=lambda x: -x["pts"])
        out.update(
            status=status, label=label, kind=kind, reason=reason, checks=checks,
            price=price, prev=prev, open=op, high=s.get("high"), low=s.get("low"), time=s.get("time"),
            est=bool(s.get("est")), lt=s.get("lt"), lt_t=s.get("lt_t"),
            chgp=r2(chg), gap=r2(gap), vwap=r2(vw, 2), ext=r2(ext), vol_ratio=r2(vr, 1),
            outer=r2(outer, 0), stable=st_min, spread=r2(spread),
            entry=entry, stop=stop, target=target,
            stop_pct=r2((stop / entry - 1) * 100), target_pct=r2((target / entry - 1) * 100),
            lots=lots, lots_budget=lots_b, fit=lots >= 1,
            amount=round(entry * 1000 * lots), loss=round(trade_pnl(entry, stop, lots, disc, etf)) if lots else None,
            gain=round(trade_pnl(entry, target, lots, disc, etf)) if lots else None,
            tags=[x["label"] for x in sig[:3]],
        )
        return out

    def candidates(self, now=None):
        """即時名單裡、額度內買得起的前幾名。"""
        eng = self.eng
        out, excl = [], 0
        for k, code in enumerate(list(getattr(eng, "live_cands", []) or [])[:40]):
            a = self.advise(code, rank=k + 1, now=now)
            if a["status"] == "nodata":
                continue
            if not a.get("fit"):
                excl += 1
                continue
            out.append(a)
            if len(out) >= int(self.settings.get("top_n", 5)):
                break
        self._excluded = excl
        return out

    def watch_codes(self):
        """需要高頻更新報價的股票：持倉、候選、今天盤中名單裡的股票。"""
        with self.lock:
            codes = [p["code"] for p in self.positions]
            codes += list(getattr(self.eng, "live_cands", []) or [])[:12]
            for sn in ((self.day or {}).get("snaps") or {}).values():
                codes += [p["code"] for p in sn.get("picks", [])]
            seen, out = set(), []
            for c in codes:
                if c not in seen and c in self.eng.universe:
                    seen.add(c)
                    out.append(c)
            return out[:30]

    # ------------------------------------------------------------------ 盤中流程
    def on_live_rank(self, cands):
        """每次盤中重新評分後呼叫：更新排名穩定度，並在固定時間存下即時名單。"""
        with self.lock:
            day = self._day()
            now = self.eng.clock()
            hhmm = now.strftime("%H:%M")
            top = list(cands[:20])
            for k, code in enumerate(top):
                r = day["ranks"].get(code)
                if not r or not r.get("since"):
                    r = day["ranks"][code] = {"since": hhmm, "first": (r or {}).get("first") or hhmm}
                r.update(last=hhmm, rank=k + 1, miss=0)
            tops = set(top)
            for code, r in day["ranks"].items():
                if code not in tops and r.get("since"):
                    r["miss"] = r.get("miss", 0) + 1
                    if r["miss"] >= 3:
                        r["since"] = None
            self._maybe_snapshot(now, cands)
            self._dirty = True

    def _maybe_snapshot(self, now, cands):
        day = self._day()
        t_now = now.hour * 60 + now.minute
        for cp in CHECKPOINTS:
            if cp in day["snaps"] or not (hm(cp) <= t_now < hm(cp) + SNAP_WINDOW):
                continue
            picks = []
            for k, code in enumerate(cands[:10]):
                a = self.advise(code, rank=k + 1, now=now)
                if a["status"] == "nodata":
                    continue
                picks.append({"rank": k + 1, "code": code, "name": a["name"], "score": a.get("score"), "etf": a["etf"],
                              "price": a["price"], "entry": a["entry"], "stop": a["stop"], "target": a["target"],
                              "adv": a["status"], "fit": a["fit"], "hi": a["price"], "lo": a["price"], "hit": None})
            if picks:
                day["snaps"][cp] = {"time": now.strftime("%H:%M"), "picks": picks}
                log(f"盤中名單驗證：已存下 {cp} 的即時前十名（{len(picks)} 檔）")
                self._save_day(force=True)

    def tick(self):
        """盤中每隔幾秒呼叫：記錄分鐘價、追蹤盤中名單、檢查要不要發提醒。"""
        if time.time() - self._last_tick < 8:
            return
        self._last_tick = time.time()
        eng = self.eng
        if not eng.is_live():
            return
        with self.lock:
            day = self._day()
            now = eng.clock()
            hhmm = now.strftime("%H:%M")
            cands = self.candidates(now)
            watch = {c["code"] for c in cands} | {p["code"] for p in self.positions}
            for sn in day["snaps"].values():
                watch |= {p["code"] for p in sn["picks"]}
            for code in watch:
                s = eng.snap.get(code) or {}
                if s.get("date") != day["date"] or not s.get("price"):
                    continue
                px = day["px"].setdefault(code, [])
                if not px and s.get("open") and hhmm > "09:01":
                    px.append(["09:00", s["open"]])
                if px and px[-1][0] == hhmm:
                    px[-1][1] = s["price"]
                else:
                    px.append([hhmm, s["price"]])
            for sn in day["snaps"].values():
                for p in sn["picks"]:
                    price = (eng.snap.get(p["code"]) or {}).get("price")
                    if not price:
                        continue
                    p["hi"], p["lo"] = max(p["hi"], price), min(p["lo"], price)
                    if p["hit"] is None:
                        if price <= p["stop"]:
                            p["hit"] = ["stop", hhmm]
                        elif price >= p["target"]:
                            p["hit"] = ["target", hhmm]
            self._check_alerts(cands, now)
            self._dirty = True
            self._save_day()

    # ------------------------------------------------------------------ 提醒
    def _alert(self, kind, key, title, body, code=None):
        day = self._day()
        if key in day["akeys"]:
            return
        day["akeys"].append(key)
        day["seq"] += 1
        al = {"id": f"{day['date']}-{day['seq']}", "ts": time.time(), "time": self.eng.clock().strftime("%H:%M"),
              "kind": kind, "code": code, "title": title, "body": body}
        day["alerts"].append(al)
        log(f"[提醒] {title}｜{body}")
        self._save_day(force=True)
        if self.settings.get("notify") and self.settings.get("ntfy_topic") and not self.eng.demo:
            threading.Thread(target=self.push, args=(al,), daemon=True).start()

    def _check_alerts(self, cands, now):
        eng = self.eng
        for a in cands:
            code = a["code"]
            if a["status"] == "go":
                self._go_seen[code] = self._go_seen.get(code, 0) + 1
                if self._go_seen[code] >= 2:   # 連續兩次檢查都符合才提醒，避免一閃而過
                    self._alert("entry", f"entry:{code}", f"可進場：{a['name']} {code}",
                                f"現價 {fmt_price(a['entry'])}｜停損 {fmt_price(a['stop'])}｜目標 {fmt_price(a['target'])}｜最多 {a['lots']} 張", code)
            else:
                self._go_seen[code] = 0
        t_now = now.hour * 60 + now.minute
        today_pos = []
        for p in self.positions:
            s = eng.snap.get(p["code"]) or {}
            price = s.get("price") if s.get("date") == today_iso() else None
            if p.get("date") == today_iso():
                today_pos.append(p["name"])
            if not price:
                continue
            if p.get("stop") and price <= p["stop"]:
                self._alert("stop", f"stop:{p['id']}", f"停損：{p['name']} {p['code']} 跌到 {fmt_price(price)}",
                            f"已碰到停損價 {fmt_price(p['stop'])}（進場 {fmt_price(p['entry'])} × {p['lots']} 張），建議出場。", p["code"])
            elif p.get("target") and price >= p["target"]:
                self._alert("target", f"target:{p['id']}", f"到目標價：{p['name']} {p['code']} {fmt_price(price)}",
                            f"已到目標價 {fmt_price(p['target'])}（進場 {fmt_price(p['entry'])} × {p['lots']} 張），可以獲利了結。", p["code"])
        if today_pos:
            for key, t0, txt in (("close:1310", 13 * 60 + 10, "13:25 前"), ("close:1320", 13 * 60 + 20, "馬上")):
                if t_now >= t0:
                    self._alert("close", key, "收盤前提醒：還有當沖部位沒出場",
                                f"{'、'.join(today_pos)} 還沒賣出，請{txt}出場（13:25 後只剩最後一盤集合競價）。")

    def push(self, al, topic=None):
        """用 ntfy 推播到手機。成功回傳 None，失敗回傳原因。"""
        topic = topic or self.settings.get("ntfy_topic")
        if not topic:
            return "尚未設定推播主題"
        tag = {"entry": "white_check_mark", "stop": "rotating_light", "target": "dart", "close": "alarm_clock"}.get(al.get("kind"), "bell")
        body = {"topic": topic, "title": al["title"], "message": al["body"],
                "priority": 5 if al.get("kind") in ("stop", "close") else 4, "tags": [tag]}
        try:
            from .net import _strict_off_context
            req = urllib.request.Request("https://ntfy.sh/", data=json.dumps(body).encode("utf-8"),
                                         headers={"Content-Type": "application/json", "User-Agent": "tw-stock-scanner"})
            with urllib.request.urlopen(req, timeout=12, context=_strict_off_context()) as r:
                r.read()
            self.eng.src_ok("手機推播（ntfy）", al["title"][:40])
            return None
        except Exception as e:  # 推播失敗不能影響主流程
            reason = str(getattr(e, "reason", e))
            if "CERTIFICATE" in reason.upper():
                reason = "憑證驗證失敗：請到「應用程式 › Python 3.x」資料夾雙擊 Install Certificates.command 後再試"
            self.eng.src_fail("手機推播（ntfy）", reason)
            return reason[:200]

    # ------------------------------------------------------------------ 收盤後對答案
    def on_close(self):
        eng = self.eng
        with self.lock:
            today = today_iso()
            swept = bool(eng.last_sweep and eng.last_sweep.strftime("%Y-%m-%d %H:%M") >= f"{today} 13:36")
            recs = list(self.pending.values())
            if self.day and self.day.get("snaps") and not self.day.get("final"):
                recs.append(self.day)
            for rec in recs:
                if rec["date"] < today or (swept and eng.phase() not in ("open", "preopen")):
                    self._finalize(rec)

    def _finalize(self, rec):
        eng = self.eng
        disc = eng.cfg.get("fee_discount", 0.28)
        missing = 0
        for sn in rec["snaps"].values():
            for p in sn["picks"]:
                bars = eng.merged_bars(p["code"]) or []
                bar = next((b for b in reversed(bars[-15:]) if b[0] == rec["date"]), None)
                if not bar:
                    missing += 1
                    continue
                close = bar[4]
                p["close"] = close
                p["ret"] = r2(net_ret(p["entry"], close, disc, p.get("etf")), 3)
                hit = p.get("hit")
                ex = p["stop"] if hit and hit[0] == "stop" else p["target"] if hit and hit[0] == "target" else close
                p["rule"] = r2(net_ret(p["entry"], ex, disc, p.get("etf")), 3)
        age = (time.time() - time.mktime(time.strptime(rec["date"], "%Y-%m-%d"))) / 86400
        if missing and age < 7:
            return   # 收盤價還沒進來，下次再試
        rec["final"] = True
        rec["px"], rec["ranks"] = {}, {}   # 分鐘價與排名紀錄不用留，檔案才不會越存越大
        self.history[rec["date"]] = rec
        self.pending.pop(rec["date"], None)
        if not eng.demo:
            write_json(self._p("days", f"{rec['date']}.json"), rec)
        log(f"盤中名單驗證：{rec['date']} 已對完答案")

    # ------------------------------------------------------------------ 持倉與交易紀錄
    def _pos_view(self, p):
        eng = self.eng
        s = eng.snap.get(p["code"]) or {}
        price = s.get("price")
        disc = eng.cfg.get("fee_discount", 0.28)
        v = dict(p)
        if price:
            same_day = p.get("date") == today_iso()
            pnl = trade_pnl(p["entry"], price, p["lots"], disc, p.get("etf"), same_day)
            v.update(price=price, est=bool(s.get("est")), pnl=round(pnl), ret=r2(pnl / (p["entry"] * 1000 * p["lots"]) * 100),
                     chgp=r2((price / s["prev"] - 1) * 100) if s.get("prev") else None, quote_time=s.get("time"),
                     state="stop" if p.get("stop") and price <= p["stop"] else "target" if p.get("target") and price >= p["target"] else "hold")
        return v

    def _trade_summary(self, trades):
        n = len(trades)
        if not n:
            return {"n": 0}
        pnl = sum(t["pnl"] for t in trades)
        return {"n": n, "win": sum(1 for t in trades if t["pnl"] > 0), "pnl": round(pnl),
                "avg_ret": r2(sum(t["ret"] for t in trades) / n)}

    def action(self, data):
        act = data.get("action")
        fn = getattr(self, f"_act_{act}", None) if isinstance(act, str) and act.isidentifier() else None
        if not fn:
            return {"error": "不支援的操作"}
        try:
            with self.lock:
                return fn(data)
        except (TypeError, ValueError, KeyError):
            return {"error": "輸入格式不正確"}

    def _stock(self, code):
        code = str(code or "").strip().upper()
        u = self.eng.universe.get(code)
        return (code, u) if u else (code, None)

    def _act_settings(self, d):
        s = dict(self.settings)
        if "budget" in d:
            v = float(d["budget"])
            if not 10000 <= v <= 1e9:
                return {"error": "當沖額度請輸入 1 萬元以上"}
            s["budget"] = round(v)
        if "risk" in d:
            v = float(d["risk"])
            if not 100 <= v <= s["budget"]:
                return {"error": "每筆停損金額需介於 100 元與當沖額度之間"}
            s["risk"] = round(v)
        if "notify" in d:
            s["notify"] = bool(d["notify"])
        if "ntfy_topic" in d:
            t = str(d["ntfy_topic"] or "").strip()
            if t and not TOPIC_RE.match(t):
                return {"error": "推播主題只能用英文字母、數字、- 和 _，長度 6～64"}
            s["ntfy_topic"] = t
        self.settings = s
        if not self.eng.demo:
            st = read_json(self.eng._p("settings.json"), {}) or {}
            st["daytrade"] = s
            write_json(self.eng._p("settings.json"), st)
        return {"ok": True, "settings": s}

    def _act_test_push(self, d):
        topic = str(d.get("ntfy_topic") or self.settings.get("ntfy_topic") or "").strip()
        if not TOPIC_RE.match(topic):
            return {"error": "請先填入推播主題（英文字母、數字、- 和 _，長度 6～64）"}
        if self.eng.demo:
            return {"error": "示範模式不會連網，請在正式模式測試"}
        err = self.push({"kind": "entry", "title": "臺股強勢掃描：測試推播", "body": "收到這則訊息代表手機推播設定成功。"}, topic)
        return {"error": "推播失敗：" + err} if err else {"ok": True}

    def _act_buy(self, d):
        code, u = self._stock(d.get("code"))
        if not u:
            return {"error": f"找不到股票代號 {code}"}
        entry, lots = float(d["price"]), int(d["lots"])
        if entry <= 0 or lots < 1 or lots > 9999:
            return {"error": "買進價與張數需大於 0"}
        etf = u.get("type") == "etf"
        s = self.eng.snap.get(code) or {}
        stop, target = d.get("stop"), d.get("target")
        if not stop or not target:
            st, tg = self.levels(entry, s.get("open"), self.vwap(code, s), s.get("low"), etf, s.get("limit_up"))
            stop, target = stop or st, target or tg
        stop, target = float(stop), float(target)
        if not stop < entry < target:
            return {"error": "停損價要低於買進價，目標價要高於買進價"}
        now = self.eng.clock()
        a = self.eng.analysis.get(code) or {}
        lc = list(getattr(self.eng, "live_cands", []) or [])
        rank = lc.index(code) + 1 if code in lc else None
        p = {"id": f"p{int(time.time() * 1000)}", "code": code, "name": u.get("name") or code, "etf": etf,
             "date": today_iso(), "time": now.strftime("%H:%M"), "entry": entry, "lots": lots,
             "stop": stop, "target": target, "score": a.get("score"), "rank": rank}
        self.positions.append(p)
        if code not in self.eng.rt_priority:
            self.eng.rt_priority.insert(0, code)   # 馬上列入高頻更新，不用等下一次排名
        self._save_state()
        return {"ok": True, "position": p}

    def _find_pos(self, pid):
        return next((p for p in self.positions if p["id"] == pid), None)

    def _act_edit(self, d):
        p = self._find_pos(d.get("id"))
        if not p:
            return {"error": "找不到這筆持倉"}
        stop, target = float(d["stop"]), float(d["target"])
        if not 0 < stop < target:
            return {"error": "停損價要低於目標價"}
        p.update(stop=stop, target=target)
        day = self._day()   # 改了價位就允許重新提醒
        day["akeys"] = [k for k in day["akeys"] if k not in (f"stop:{p['id']}", f"target:{p['id']}")]
        self._save_state()
        return {"ok": True}

    def _act_cancel(self, d):
        p = self._find_pos(d.get("id"))
        if not p:
            return {"error": "找不到這筆持倉"}
        self.positions.remove(p)
        self._save_state()
        return {"ok": True}

    def _record(self, code, name, etf, lots, buy, sell, date, buy_time, sell_time, daytrade, extra=None):
        disc = self.eng.cfg.get("fee_discount", 0.28)
        pnl = trade_pnl(buy, sell, lots, disc, etf, daytrade)
        t = {"id": f"t{int(time.time() * 1000)}", "date": date, "code": code, "name": name, "etf": etf, "lots": lots,
             "buy": buy, "sell": sell, "buy_time": buy_time, "sell_time": sell_time, "daytrade": daytrade,
             "pnl": round(pnl), "ret": r2(pnl / (buy * 1000 * lots) * 100), **(extra or {})}
        self.trades.append(t)
        self.trades.sort(key=lambda x: (x["date"], x.get("sell_time") or ""))
        self._save_state()
        return t

    def _act_sell(self, d):
        p = self._find_pos(d.get("id"))
        if not p:
            return {"error": "找不到這筆持倉"}
        price = float(d["price"])
        if price <= 0:
            return {"error": "賣出價需大於 0"}
        now = self.eng.clock()
        t = self._record(p["code"], p["name"], p.get("etf"), p["lots"], p["entry"], price, today_iso(), p.get("time"),
                         now.strftime("%H:%M"), p.get("date") == today_iso(),
                         {"buy_date": p.get("date"), "score": p.get("score"), "rank": p.get("rank")})
        self.positions.remove(p)
        self._save_state()
        return {"ok": True, "trade": t}

    def _act_log(self, d):
        code, u = self._stock(d.get("code"))
        if not u:
            return {"error": f"找不到股票代號 {code}"}
        buy, sell, lots = float(d["buy"]), float(d["sell"]), int(d["lots"])
        if buy <= 0 or sell <= 0 or lots < 1 or lots > 9999:
            return {"error": "買價、賣價與張數需大於 0"}
        date = str(d.get("date") or today_iso())
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date) or date > today_iso():
            return {"error": "日期格式不正確"}
        t = self._record(code, u.get("name") or code, u.get("type") == "etf", lots, buy, sell, date, None, None,
                         d.get("daytrade", True) is not False, {"manual": True})
        return {"ok": True, "trade": t}

    def _act_del_trade(self, d):
        n = len(self.trades)
        self.trades = [t for t in self.trades if t["id"] != d.get("id")]
        if len(self.trades) == n:
            return {"error": "找不到這筆交易"}
        self._save_state()
        return {"ok": True}

    # ------------------------------------------------------------------ API
    def recent_alerts(self, n=6):
        with self.lock:
            if not self.settings.get("notify"):
                return []
            return list(((self.day or {}).get("alerts") or [])[-n:])

    def _spark(self, code):
        px = (self._day()["px"]).get(code) or []
        step = max(1, len(px) // 90)
        out = px[::step]
        if px and out[-1] is not px[-1]:
            out.append(px[-1])
        return out

    def api(self):
        eng = self.eng
        with self.lock:
            live = eng.is_live()
            now = eng.clock()
            day = self._day()
            cands = self.candidates(now) if live else []
            order = {"go": 0, "wait": 1}
            cands.sort(key=lambda c: (order.get(c["status"], 2), c["rank"]))   # 可進場的排最前面
            for c in cands:
                c["spark"] = self._spark(c["code"])
            today = today_iso()
            todays = [t for t in self.trades if t["date"] == today]
            return {
                "live": live, "phase": eng.phase(), "now": now.strftime("%H:%M:%S"), "date": today,
                "settings": {**self.settings, "fee_discount": eng.cfg.get("fee_discount")},
                "market": self.market(), "cands": cands, "excluded": self._excluded if live else 0,
                "positions": [self._pos_view(p) for p in self.positions],
                "alerts": list(day["alerts"][-30:])[::-1],
                "trades": todays[::-1], "trades_summary": self._trade_summary(todays),
                "snaps": {cp: (day["snaps"].get(cp) or {}).get("time") for cp in CHECKPOINTS},
                "rules": {"stable": STABLE_MIN, "vol": VOL_RATIO_MIN, "ext": EXT_MAX, "chg": CHG_MAX,
                          "gap": eng.cfg.get("chase_gap_pct", 3.0), "stop_min": STOP_MIN * 100, "stop_max": STOP_MAX * 100, "rr": RR},
            }

    def review(self):
        """盤中名單驗證的統計，加上我的交易紀錄。"""
        with self.lock:
            recs = [self.history[d] for d in sorted(self.history)]
            groups = (("all", lambda p: True), ("top3", lambda p: p["rank"] <= 3), ("top1", lambda p: p["rank"] == 1),
                      ("go", lambda p: p.get("adv") == "go"))
            cps = {}
            for cp in CHECKPOINTS:
                g = {}
                for name, pred in groups:
                    rows, days = [], set()
                    for rec in recs:
                        for p in ((rec["snaps"].get(cp) or {}).get("picks") or []):
                            if p.get("ret") is not None and pred(p):
                                rows.append(p)
                                days.add(rec["date"])
                    n = len(rows)
                    g[name] = {"n": n, "days": len(days)} if not n else {
                        "n": n, "days": len(days),
                        "avg": r2(sum(p["ret"] for p in rows) / n), "win": r2(sum(1 for p in rows if p["ret"] > 0) / n * 100, 1),
                        "rule_avg": r2(sum(p["rule"] for p in rows) / n), "rule_win": r2(sum(1 for p in rows if p["rule"] > 0) / n * 100, 1),
                        "target": r2(sum(1 for p in rows if p.get("hit") and p["hit"][0] == "target") / n * 100, 0),
                        "stop": r2(sum(1 for p in rows if p.get("hit") and p["hit"][0] == "stop") / n * 100, 0)}
                cps[cp] = g
            recent = []
            for rec in recs[-12:][::-1]:
                row = {"date": rec["date"], "cps": {}}
                for cp in CHECKPOINTS:
                    ps = [p for p in ((rec["snaps"].get(cp) or {}).get("picks") or []) if p.get("ret") is not None]
                    if ps:
                        row["cps"][cp] = {"avg": r2(sum(p["ret"] for p in ps) / len(ps)), "n": len(ps),
                                          "top1": next((p["ret"] for p in ps if p["rank"] == 1), None)}
                recent.append(row)
            today = self.day or {}
            return {"days": len(recs), "since": recs[0]["date"] if recs else None, "checkpoints": list(CHECKPOINTS),
                    "cps": cps, "recent": recent, "pending": len(self.pending) + (1 if today.get("snaps") and not today.get("final") else 0),
                    "trades": self.trades[::-1][:200], "trades_summary": self._trade_summary(self.trades),
                    "settings": {"fee_discount": self.eng.cfg.get("fee_discount"), "min_fee": MIN_FEE}}
