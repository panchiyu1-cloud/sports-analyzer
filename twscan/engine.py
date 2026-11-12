"""掃描引擎：背景抓資料 → 計算 → 排名，並提供網頁 API 需要的資料。"""
import bisect
import os
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta

from . import analysis as an
from . import news as nw
from . import sources as src
from . import quotes
from .daytrade import DayTrade
from .net import Http
from .review import Review
from .util import (DATA_DIR, TW, clamp, log, market_phase, now_tw, r2, read_json,
                   today_iso, write_json)

DEFAULT_CONFIG = {
    "port": 8000,
    "host": "0.0.0.0",
    "hist_min_trade_value": 3_000_000,    # 當日成交值 ≥ 300 萬就下載歷史 K 線（股票池要夠大，回測才不會有倖存者偏差）
    "top_min_avg_value": 50_000_000,      # 前十名候選：20 日平均成交值 ≥ 5,000 萬（避免冷門股）
    "top_min_price": 10,
    "include_etf_in_top": False,
    "news_candidates": 30,                # 評分前 N 名才抓個股新聞
    "news_refresh_minutes": 30,
    "rescan_minutes_open": 10,            # 盤中完整重掃間隔
    "rescan_minutes_closed": 60,
    "reanalyze_seconds_open": 60,         # 盤中用即時價重新評分的間隔
    "mis_interval_sec": 1.5,              # 即時行情請求間隔（太快會被證交所暫時封鎖）
    "rt_priority_count": 40,              # 盤中優先追蹤（內外盤）的檔數
    "yahoo_workers": 4,
    "insecure_ssl_fallback": True,
    "model_max_stocks": 400,
    "volume_estimate": True,              # 盤中用「預估全日量」計算量能訊號
    "chase_gap_pct": 3.0,                 # 昨日名單開盤跳空超過幾 % 視為不宜追價
    "fee_discount": 0.28,                 # 券商電子下單手續費折扣（0.28＝2.8 折）
    "review_days": 105,                   # 復盤回補的交易日數（約 5 個月）
}

CONFIG_PATH = os.path.join(os.path.dirname(DATA_DIR), "config.json")


def save_config(cfg):
    import json
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except OSError:
        pass

INDEX_CH = {"t00": ("TAIEX", "加權指數"), "o00": ("TPEX", "櫃買指數")}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    user = read_json(CONFIG_PATH)
    if isinstance(user, dict):
        cfg.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    else:
        write_json(CONFIG_PATH, DEFAULT_CONFIG)
    return cfg


REPAIR_VERSION = 8   # 資料修復版本：第 8 版起收盤價以官方資料為準，舊資料需要重抓一次


class Engine:
    def __init__(self, cfg, demo=False):
        self.cfg = cfg
        self.demo = demo
        self.http = Http(intervals={"mis.twse.com.tw": cfg["mis_interval_sec"]},
                         insecure_fallback=cfg["insecure_ssl_fallback"])
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.universe, self.snap, self.hist, self.hist_meta, self.yahoo_meta = {}, {}, {}, {}, {}
        self.analysis, self.chips, self.flow, self.news = {}, {}, {}, {}
        self.market_news = {"fetched": 0, "items": []}
        self.index = {}
        self.index_hist = {"TAIEX": [], "TPEX": []}
        self.regime = None
        self.close_payload = None     # 收盤確認名單（某個交易日收盤後的評估，隔天盤中固定不變）
        self.live_payload = None      # 盤中暫定名單（每分鐘依即時價重算）
        self.close_analysis = None
        self.demo_live = False
        self.model = None
        self.rt_priority = []
        self.mis_cursor = 0
        self.last_sweep = None
        self.last_analyze = 0.0
        self.sources = {}
        self.status = {"phase": "init", "msg": "啟動中…", "done": 0, "total": 0,
                       "last_scan": None, "scanning": False, "first_scan_done": False}
        self.data_dir = os.path.join(DATA_DIR, "demo" if demo else "live")
        self.review = Review(self)
        self.official = {}            # 官方收盤資料（證交所／櫃買中心）：date -> {code: [開, 高, 低, 收, 成交股數]}
        self.official_got = {}        # 各市場最新一份官方收盤資料的日期：{"TWSE": date, "TPEx": date}
        self._official_try = 0.0
        self.quality = {"official": [], "repair": None}   # 報價／K 線核對紀錄（模型與資料頁顯示）
        self.repair_pending = set()   # 一次性修復：需要向 Yahoo 重抓近 3 個月 K 線的股票
        self.intra = {}               # 分時 K 線快取：(code, "30m"|"60m") -> {"t": 抓取時間, "bars": [...]}
        self.live_cands = []          # 盤中即時名單的完整排序（當沖模式用）
        self.daytrade = DayTrade(self)

    # ================================================================== 基本
    def _p(self, *parts):
        return os.path.join(self.data_dir, *parts)

    def set_status(self, phase, msg, done=0, total=0):
        with self.lock:
            self.status.update(phase=phase, msg=msg, done=done, total=total)
        if phase not in ("history", "news"):
            log(msg)

    def src_ok(self, name, msg=""):
        with self.lock:
            self.sources[name] = {"ok": True, "at": now_tw().strftime("%m/%d %H:%M:%S"), "msg": msg}

    def src_fail(self, name, err):
        with self.lock:
            prev = self.sources.get(name, {})
            self.sources[name] = {"ok": False, "at": now_tw().strftime("%m/%d %H:%M:%S"),
                                  "msg": str(err)[:240], "last_ok": prev.get("at") if prev.get("ok") else prev.get("last_ok")}
        log(f"[資料來源失敗] {name}: {str(err)[:160]}")

    def load_cache(self):
        os.makedirs(self._p("hist"), exist_ok=True)
        os.makedirs(self._p("chips"), exist_ok=True)
        os.makedirs(self._p("close_lists"), exist_ok=True)
        u = read_json(self._p("universe.json"), {})
        self.universe = u.get("universe", {}) if isinstance(u, dict) else {}
        self.snap = read_json(self._p("snap.json"), {}) or {}
        self.hist_meta = read_json(self._p("hist_meta.json"), {}) or {}
        self.yahoo_meta = read_json(self._p("yahoo_meta.json"), {}) or {}
        st = read_json(self._p("settings.json"), {}) or {}
        if isinstance(st, dict):
            self.cfg.update({k: v for k, v in st.items() if k in ("fee_discount",)})
        for fn in os.listdir(self._p("hist")):
            if fn.endswith(".json"):
                bars = read_json(self._p("hist", fn))
                if isinstance(bars, list) and bars:
                    self.hist[fn[:-5]] = bars
        for fn in sorted(os.listdir(self._p("chips")))[-140:]:
            m = read_json(self._p("chips", fn))
            if isinstance(m, dict):
                self.chips[fn[:-5]] = m
        self.index_hist = read_json(self._p("index_hist.json"), self.index_hist) or self.index_hist
        n = read_json(self._p("news.json"), {})
        if isinstance(n, dict):
            self.news = n.get("stocks", {})
            self.market_news = n.get("market", self.market_news)
        self.model = read_json(self._p("model.json"))
        odir = self._p("official")
        if os.path.isdir(odir):
            for fn in sorted(f for f in os.listdir(odir) if f.endswith(".json"))[-60:]:
                m = read_json(os.path.join(odir, fn))
                if isinstance(m, dict) and m:
                    self.official[fn[:-5]] = m
        q = read_json(self._p("quality.json"), None)
        if isinstance(q, dict):
            self.quality.update({k: v for k, v in q.items() if k in self.quality})
        rep = read_json(self._p("repair.json"), None)
        if not isinstance(rep, dict) or rep.get("v") != REPAIR_VERSION:
            # 第一次執行這一版：舊版收盤後可能用不完整的即時報價補了日 K，近 3 個月的 K 線全部向 Yahoo 重抓一次
            rep = {"v": REPAIR_VERSION, "pending": sorted(self.hist), "total": len(self.hist), "started": now_tw().strftime("%Y-%m-%d %H:%M")}
            write_json(self._p("repair.json"), rep)
        self.repair_pending = set(rep.get("pending") or [])
        self.quality["repair"] = {"total": rep.get("total") or 0, "left": len(self.repair_pending), "done": rep.get("done")}
        self.review.load()
        self.daytrade.load()
        self._update_regime()
        if self.universe:
            log(f"載入快取：{len(self.universe)} 檔股票、{len(self.hist)} 檔歷史 K 線")

    def start(self):
        if self.demo:
            from .demo import populate, populate_daytrade
            populate(self)
            self._demo_dt = populate_daytrade
        else:
            self.load_cache()
        if self.hist and self.universe:
            try:
                self.analyze_all()
                self.rank()
                if self.demo:
                    self._demo_dt(self)
            except Exception:
                traceback.print_exc()
        threading.Thread(target=self._scan_loop, name="scan", daemon=True).start()
        threading.Thread(target=self._rt_loop, name="realtime", daemon=True).start()

    def trigger_rescan(self):
        self.wake.set()

    # ================================================================== 交易日
    def last_trading_date(self):
        th = self.index_hist.get("TAIEX") or []
        d = th[-1][0] if th else None
        sd = max((s.get("date") or "" for s in self.snap.values()), default="")
        return max(x for x in (d or "", sd) if x is not None) or today_iso()

    def recent_trading_dates(self, n):
        th = self.index_hist.get("TAIEX") or []
        ds = [b[0] for b in th]
        ltd = self.last_trading_date()
        if ltd and (not ds or ds[-1] < ltd):
            ds.append(ltd)
        if not ds:
            d = now_tw().date()
            while len(ds) < n:
                if d.weekday() < 5:
                    ds.insert(0, d.isoformat())
                d -= timedelta(days=1)
        return ds[-n:]

    @staticmethod
    def _regime_from(c):
        if len(c) < 65:
            return None
        ma20 = sum(c[-20:]) / 20
        ma20p = sum(c[-25:-5]) / 20
        ma60 = sum(c[-60:]) / 60
        if c[-1] > ma20 > ma60:
            return "bull"
        if c[-1] < ma20 and ma20 < ma20p:
            return "bear"
        return "neutral"

    def _update_regime(self):
        th = self.index_hist.get("TAIEX") or []
        c = [b[4] for b in th]
        live = self.index.get("TAIEX", {}).get("price")
        if c and live and self.index.get("TAIEX", {}).get("date") == today_iso():
            if th[-1][0] == today_iso():
                c[-1] = live
            else:
                c.append(live)
        self.regime = self._regime_from(c)

    # ================================================================== 掃描流程
    def _scan_loop(self):
        while not self.stop.is_set():
            try:
                self.full_scan()
            except Exception as e:
                traceback.print_exc()
                self.set_status("error", f"掃描發生錯誤：{e}")
            finally:
                with self.lock:
                    self.status["scanning"] = False
            phase0 = market_phase()
            wait = self.cfg["rescan_minutes_open" if phase0 == "open" else "rescan_minutes_closed"] * 60
            end = time.time() + wait
            while time.time() < end and not self.stop.is_set():
                if self.wake.wait(timeout=5):
                    self.wake.clear()
                    break
                ph = market_phase()
                if ph != phase0 and ph in ("post", "open"):
                    break  # 開盤或收盤時立即重掃
                if self.is_live() and time.time() - self.last_analyze > self.cfg["reanalyze_seconds_open"]:
                    try:
                        self.analyze_all()
                        self.rank()
                    except Exception:
                        traceback.print_exc()

    def full_scan(self):
        with self.lock:
            self.status["scanning"] = True
        t0 = time.time()
        if not self.demo:
            self.set_status("universe", "更新上市櫃股票清單與日行情…")
            self.update_universe()
            self.set_status("index", "更新大盤指數…")
            self.update_index_hist()
            self.update_histories()
            self.set_status("chips", "更新三大法人買賣超…")
            self.update_chips()
        self.set_status("analyze", "計算技術指標與評分…")
        self.analyze_all()
        cands = self.rank()
        if not self.demo:
            self.set_status("news", "抓取即時新聞並分析情緒…")
            self.update_news(list(dict.fromkeys(cands[: self.cfg["news_candidates"]] + [r["code"] for r in self.top_payload_list()])))
            self.update_market_news()
            self.analyze_all(list(dict.fromkeys(cands[: self.cfg["news_candidates"]] + [r["code"] for r in self.top_payload_list()])))
            self.rank()
        self.set_status("model", "以歷史資料回測評分模型…")
        self.update_model()
        self.review.ensure()
        with self.lock:
            self.status["last_scan"] = now_tw().strftime("%Y-%m-%d %H:%M:%S")
            self.status["first_scan_done"] = True
        self.set_status("idle", f"掃描完成（{time.time() - t0:.0f} 秒），共分析 {len(self.analysis)} 檔")

    # ------------------------------------------------------------------ 股票清單與日行情
    def update_universe(self):
        daily = []
        for name, fn in (("證交所 OpenAPI 日行情", src.fetch_twse_daily_all),
                         ("櫃買中心 OpenAPI 日行情", src.fetch_tpex_daily_all)):
            try:
                rows = fn(self.http)
                daily += rows
                self.src_ok(name, f"{len(rows)} 檔")
            except Exception as e:
                self.src_fail(name, e)
        cached = read_json(self._p("universe.json"), {}) or {}
        isin = cached.get("isin") or {}
        if cached.get("date") != today_iso() or not isin:
            fresh = {}
            for mode, mkt in ((2, "TWSE"), (4, "TPEx")):
                try:
                    m = src.fetch_isin(self.http, mode)
                    for code, info in m.items():
                        info["market"] = mkt
                    fresh.update(m)
                    self.src_ok(f"證交所 ISIN 清單（{'上市' if mode == 2 else '上櫃'}）", f"{len(m)} 檔")
                except Exception as e:
                    self.src_fail(f"證交所 ISIN 清單（{'上市' if mode == 2 else '上櫃'}）", e)
            if fresh:
                isin = fresh
        uni = {}
        for code, info in isin.items():
            uni[code] = {"code": code, "name": info["name"], "market": info.get("market", "TWSE"),
                         "type": info["type"], "industry": info.get("industry", "")}
        for r in daily:
            u = uni.get(r["code"])
            if u is None:
                uni[r["code"]] = {"code": r["code"], "name": r["name"], "market": r["market"],
                                  "type": r["type"], "industry": "ETF" if r["type"] == "etf" else ""}
            elif not u["name"]:
                u["name"] = r["name"]
        with self.lock:
            if uni:
                self.universe = uni
                write_json(self._p("universe.json"), {"date": today_iso(), "isin": isin, "universe": uni})
        self.apply_daily(daily)

    # ------------------------------------------------------------------ 官方收盤資料
    def apply_daily(self, daily):
        """套用證交所／櫃買中心的官方日行情：同一天以官方收盤為準（覆蓋即時報價），存檔並修正歷史 K 線。"""
        if not daily:
            return
        with self.lock:
            for r in daily:
                if r["close"] is None or not r.get("date"):
                    continue
                s = self.snap.get(r["code"])
                sd = (s or {}).get("date") or ""
                if s is None or sd < r["date"] or (sd == r["date"] and s.get("src") != "daily"):
                    keep = {k: s.get(k) for k in ("bids", "bid_vols", "asks", "ask_vols", "limit_up", "limit_down")} \
                        if s and sd == r["date"] else {}
                    self.snap[r["code"]] = {
                        "price": r["close"], "prev": r["prev"], "open": r["open"], "high": r["high"], "low": r["low"],
                        "vol": (r["volume"] or 0) / 1000, "value": r["value"], "date": r["date"], "time": "收盤",
                        "src": "daily", "est": False, **keep,
                    }
                mk = r.get("market")
                if mk and r["date"] > (self.official_got.get(mk) or ""):
                    self.official_got[mk] = r["date"]
            write_json(self._p("snap.json"), self.snap)
        self.apply_official(daily)

    @staticmethod
    def _bar_off(b, row):
        """日 K 和官方資料是否不同：回傳 (價格不同, 成交量不同)。"""
        price = any(abs(b[k] - row[k - 1]) > 1e-6 for k in (1, 2, 3, 4))
        vol = abs((b[5] or 0) - row[4]) > max(1000, row[4] * 0.002)
        return price, vol

    @staticmethod
    def _adjusted(b, row):
        """這根日 K 是不是「等比例還原」過的價格（之後有配股、減資或分割，Yahoo 會把以前的 K 線
        全部乘上同一個比例）。這種 K 線不能用官方原始價格蓋回去，否則圖會在中間斷一截。"""
        try:
            o, h, l, c = row[0], row[1], row[2], row[3]
            if not (o and h and l and c) or h <= l:
                return False        # 一字線看不出來 → 當作一般誤差，以官方為準
            r = b[4] / c
            if abs(r - 1) <= 0.015:
                return False
            return all(abs(b[i] / x / r - 1) <= 0.004 for i, x in ((1, o), (2, h), (3, l)))
        except (TypeError, ZeroDivisionError, IndexError):
            return False

    def apply_official(self, rows):
        by_date = {}
        for r in rows:
            if r.get("date") and r.get("close") and r.get("open") and r.get("high") and r.get("low"):
                by_date.setdefault(r["date"], {})[r["code"]] = [r["open"], r["high"], r["low"], r["close"], int(r.get("volume") or 0)]
        for d, m in sorted(by_date.items()):
            with self.lock:
                cur = self.official.setdefault(d, {})
                cur.update(m)
                data = dict(cur)
                for old_d in sorted(self.official)[:-60]:
                    self.official.pop(old_d, None)
            if not self.demo:
                write_json(self._p("official", f"{d}.json"), data)
                self._prune_official()
            self._patch_hist_official(d, m)

    def _prune_official(self):
        odir = self._p("official")
        try:
            files = sorted(f for f in os.listdir(odir) if f.endswith(".json"))
        except OSError:
            return
        for f in files[:-300]:
            try:
                os.remove(os.path.join(odir, f))
            except OSError:
                pass

    def _patch_hist_official(self, d, m):
        """用官方收盤資料修正（或補上）歷史 K 線上 d 這一天。"""
        with self.lock:
            known = {b[0] for b in (self.index_hist.get("TAIEX") or [])} | set(self.official)
        prev_td = max((x for x in known if x < d), default=None)
        stamp = f"{d}T14:30"
        changed, fixed, vol_only, appended, adjusted, examples = [], 0, 0, 0, 0, []
        with self.lock:
            checked = 0
            for code, row in m.items():
                bars = self.hist.get(code)
                if not bars:
                    continue
                idx = None
                for k in range(len(bars) - 1, max(-1, len(bars) - 40), -1):
                    if bars[k][0] == d:
                        idx = k
                        break
                    if bars[k][0] < d:
                        break
                o, h, l, c, v = row
                if idx is not None:
                    checked += 1
                    b = bars[idx]
                    off_p, off_v = self._bar_off(b, row)
                    if off_p and self._adjusted(b, row):
                        adjusted += 1          # 已等比例還原（除權、減資）→ 保留
                    elif off_p or off_v:
                        factor = b[6] / b[4] if len(b) > 6 and b[4] else 1.0
                        if off_p:
                            fixed += 1
                            if len(examples) < 8:
                                examples.append({"code": code, "name": (self.universe.get(code) or {}).get("name", code),
                                                 "date": d, "before": b[4], "after": c})
                        else:
                            vol_only += 1
                        bars[idx] = [d, o, h, l, c, v, round(c * factor, 4)]
                        changed.append(code)
                elif bars[-1][0] < d and prev_td and bars[-1][0] == prev_td:
                    last = bars[-1]
                    factor = last[6] / last[4] if len(last) > 6 and last[4] else 1.0
                    self.hist[code] = (bars + [[d, o, h, l, c, v, round(c * factor, 4)]])[-330:]
                    appended += 1
                    changed.append(code)
                else:
                    continue
                if (self.hist_meta.get(code) or "") < stamp:
                    self.hist_meta[code] = stamp
            rec = {"date": d, "checked": checked, "fixed": fixed, "vol_only": vol_only, "appended": appended,
                   "adjusted": adjusted, "examples": examples, "at": now_tw().strftime("%m/%d %H:%M")}
            q = [x for x in self.quality["official"] if x["date"] != d]
            prev = next((x for x in self.quality["official"] if x["date"] == d), None)
            if prev:   # 同一天上市、上櫃分兩次到：累加
                for k in ("checked", "fixed", "vol_only", "appended", "adjusted"):
                    rec[k] += prev.get(k, 0)
                rec["examples"] = (prev.get("examples") or []) + examples
                rec["examples"] = rec["examples"][:8]
            self.quality["official"] = sorted(q + [rec], key=lambda x: x["date"])[-10:]
            quality = dict(self.quality)
        if self.demo:
            return
        for code in changed:
            write_json(self._p("hist", f"{code}.json"), self.hist[code])
        if changed:
            write_json(self._p("hist_meta.json"), self.hist_meta)
            log(f"官方收盤資料 {d}：核對 {checked} 檔，修正 {fixed} 檔價格、{vol_only} 檔成交量，補上 {appended} 檔")
        write_json(self._p("quality.json"), quality)

    def _reapply_official(self, code, bars):
        """Yahoo 重新抓回的 K 線，遇到手上有官方收盤資料的日子，以官方為準。"""
        with self.lock:
            rows = {d: m[code] for d, m in self.official.items() if code in m}
        if not rows:
            return bars
        out = []
        for b in bars:
            row = rows.get(b[0])
            if row and any(self._bar_off(b, row)) and not self._adjusted(b, row):
                factor = b[6] / b[4] if len(b) > 6 and b[4] else 1.0
                b = [b[0], row[0], row[1], row[2], row[3], row[4], round(row[3] * factor, 4)]
            out.append(b)
        return out

    def _maybe_poll_official(self):
        """收盤後（約 13:50 起）每 10 分鐘看一次官方收盤資料出來了沒有，出來就立刻套用。"""
        n = now_tw()
        if self.demo or n.weekday() >= 5 or n.hour * 60 + n.minute < 13 * 60 + 50 or self.is_holiday():
            return False
        today = today_iso()
        todo = [(mk, fn, nm) for mk, fn, nm in (("TWSE", src.fetch_twse_daily_all, "證交所 OpenAPI 日行情"),
                                                ("TPEx", src.fetch_tpex_daily_all, "櫃買中心 OpenAPI 日行情"))
                if (self.official_got.get(mk) or "") < today]
        if not todo or time.time() - self._official_try < 600:
            return False
        self._official_try = time.time()
        rows = []
        for mk, fn, nm in todo:
            try:
                r = fn(self.http)
                rows += r
                self.src_ok(nm, f"{len(r)} 檔（{max((x['date'] or '' for x in r), default='')}）")
            except Exception as e:
                self.src_fail(nm, e)
        if any(r.get("date") == today for r in rows):
            log("官方收盤資料已公布，以官方收盤價更新報價與日 K")
            self.apply_daily(rows)
            return True
        return False

    def update_index_hist(self):
        for key, sym in (("TAIEX", "^TWII"), ("TPEX", "^TWOII")):
            try:
                bars = src.fetch_yahoo_history(self.http, sym, "1y")
                if not market_phase() == "closed" or now_tw().hour < 14:
                    bars = [b for b in bars if b[0] != today_iso()]
                if bars:
                    self.index_hist[key] = bars
                self.src_ok(f"Yahoo 指數（{sym}）", f"{len(bars)} 天")
            except Exception as e:
                self.src_fail(f"Yahoo 指數（{sym}）", e)
        write_json(self._p("index_hist.json"), self.index_hist)
        self._update_regime()

    # ------------------------------------------------------------------ 歷史 K 線
    def _hist_final(self, code, ltd):
        """該檔的歷史資料是在 ltd 當天收盤（14:30）之後抓的，就視為已是最新。"""
        if not self.hist.get(code):
            return False
        return (self.hist_meta.get(code) or "") >= f"{ltd}T14:30"

    def _append_from_snapshot(self, ltd):
        """收盤後直接用當天的收盤快照補上日 K，不必每天向 Yahoo 重抓上千檔。"""
        if market_phase() in ("open", "preopen"):
            return 0
        n = now_tw()
        if ltd == today_iso() and n.hour * 60 + n.minute < 13 * 60 + 35:
            return 0
        prev = [d for d in self.recent_trading_dates(15) if d < ltd]
        if not prev:
            return 0
        prev_td = prev[-1]
        stamp = max(n.strftime("%Y-%m-%dT%H:%M"), f"{ltd}T14:30")
        changed = []
        with self.lock:
            for code, bars in list(self.hist.items()):
                s = self.snap.get(code) or {}
                if s.get("date") != ltd or not bars or bars[-1][0] != prev_td or len(bars[-1]) < 7:
                    continue
                o, h, l, c, v = s.get("open"), s.get("high"), s.get("low"), s.get("price"), s.get("vol")
                if not (o and h and l and c and v) or s.get("est"):
                    continue  # 推估的價格不寫進日 K，等官方收盤資料
                last = bars[-1]
                factor = last[6] / last[4] if last[4] else 1.0
                self.hist[code] = (bars + [[ltd, o, h, l, c, int(v * 1000), round(c * factor, 4)]])[-330:]
                self.hist_meta[code] = stamp
                changed.append(code)
        for code in changed:
            write_json(self._p("hist", f"{code}.json"), self.hist[code])
        if changed:
            log(f"用收盤資料補上 {ltd} 日 K：{len(changed)} 檔")
        return len(changed)

    def update_histories(self):
        ltd = self.last_trading_date()
        ph = market_phase()
        n = now_tw()
        if n.weekday() < 5 and (n.hour < 14 or ph in ("open", "preopen", "post")):
            # 今天的日 K 還沒定案（盤中由即時行情補上），只要求到上一個交易日
            ds = [d for d in self.recent_trading_dates(10) if d < today_iso()]
            if ds:
                ltd = ds[-1]
        self._append_from_snapshot(self.last_trading_date())
        with self.lock:
            snap = dict(self.snap)
            uni = dict(self.universe)
        today = today_iso()
        recent = self.review.recent_codes(15)
        todo = []
        for code, u in uni.items():
            s = snap.get(code) or {}
            value = s.get("value") or 0
            if value >= self.cfg["hist_min_trade_value"] or code in self.hist:
                bars = self.hist.get(code)
                if not bars or len(bars[0]) < 7 or not self._hist_final(code, ltd):
                    todo.append(code)
                elif code in recent and (self.yahoo_meta.get(code) or "") < today:
                    todo.append(code)  # 近期名單股每天向 Yahoo 更新一次，取得最新的還原權息價格
                elif code in self.repair_pending:
                    todo.append(code)  # 一次性修復：重抓近 3 個月
        total = len(todo)
        if not total:
            return
        n_rep = sum(1 for c in todo if c in self.repair_pending)
        self.set_status("history", f"下載歷史 K 線 0/{total}", 0, total)
        log(f"需要更新歷史 K 線：{total} 檔（首次執行約需數分鐘）" + (f"，其中 {n_rep} 檔是修正舊版 K 線" if n_rep else ""))
        done = fails = 0
        consecutive_fail = 0
        drop_today = ph in ("open", "preopen") or now_tw().hour < 14

        def job(code):
            u = uni[code]
            bars = self.hist.get(code)
            full = (not bars or len(bars[0]) < 7 or len(bars) < 120
                    or (date.fromisoformat(ltd) - date.fromisoformat(bars[-1][0])).days >= 60)
            sym = f"{code}.TW" if u["market"] == "TWSE" else f"{code}.TWO"
            new = src.fetch_yahoo_history(self.http, sym, "1y" if full else "3mo")
            if drop_today:
                new = [b for b in new if b[0] != today]
            return code, new, full

        with ThreadPoolExecutor(max_workers=self.cfg["yahoo_workers"]) as ex:
            futs = [ex.submit(job, c) for c in todo]
            for fu in as_completed(futs):
                done += 1
                try:
                    code, new, full = fu.result()
                    if new:
                        old = self.hist.get(code) or []
                        if full:
                            old = [b for b in old if b[0] < new[0][0] and len(b) >= 7]
                        merged = {b[0]: b for b in old}
                        merged.update({b[0]: b for b in new})
                        bars = self._reapply_official(code, [merged[k] for k in sorted(merged)][-330:])
                        with self.lock:
                            self.hist[code] = bars
                            self.hist_meta[code] = max(now_tw().strftime("%Y-%m-%dT%H:%M"), self.hist_meta.get(code) or "")
                            self.yahoo_meta[code] = today
                            self.repair_pending.discard(code)
                        write_json(self._p("hist", f"{code}.json"), bars)
                    consecutive_fail = 0
                except Exception as e:
                    fails += 1
                    consecutive_fail += 1
                    if consecutive_fail == 25:
                        self.src_fail("Yahoo 歷史 K 線", f"連續 25 檔失敗，暫停本輪下載：{e}")
                        for f2 in futs:
                            f2.cancel()
                        break
                if done % 20 == 0 or done == total:
                    self.set_status("history", f"下載歷史 K 線 {done}/{total}", done, total)
        write_json(self._p("hist_meta.json"), self.hist_meta)
        write_json(self._p("yahoo_meta.json"), self.yahoo_meta)
        self._save_repair()
        if fails < total:
            self.src_ok("Yahoo 歷史 K 線", f"本輪 {total - fails}/{total} 檔成功")
        log(f"歷史 K 線更新完成：成功 {total - fails}、失敗 {fails}")

    def _save_repair(self):
        with self.lock:
            left = sorted(c for c in self.repair_pending if c in self.hist)
            self.repair_pending = set(left)
            rep = read_json(self._p("repair.json"), {}) or {}
            rep.update(v=REPAIR_VERSION, pending=left)
            if not left and not rep.get("done"):
                rep["done"] = now_tw().strftime("%Y-%m-%d %H:%M")
                log("舊版歷史 K 線修正完成")
            self.quality["repair"] = {"total": rep.get("total") or 0, "left": len(left), "done": rep.get("done")}
        if not self.demo:
            write_json(self._p("repair.json"), rep)

    def ensure_history(self, code):
        """個股頁點開時：沒有歷史資料就即時下載。"""
        if code in self.hist and len(self.hist[code]) >= an.MIN_BARS:
            return True
        u = self.universe.get(code)
        if not u or self.demo:
            return code in self.hist
        bars = []
        try:
            sym = f"{code}.TW" if u["market"] == "TWSE" else f"{code}.TWO"
            bars = src.fetch_yahoo_history(self.http, sym, "1y")
        except Exception as e:
            self.src_fail("Yahoo 歷史 K 線", e)
        if not bars and u["market"] == "TWSE":
            d = now_tw().date().replace(day=1)
            months = []
            for _ in range(7):
                months.append(d.strftime("%Y%m"))
                d = (d - timedelta(days=1)).replace(day=1)
            for ym in reversed(months):
                try:
                    bars += src.fetch_twse_month(self.http, code, ym)
                except Exception as e:
                    self.src_fail("證交所個股月成交（備援）", e)
                    break
        if bars:
            bars = self._reapply_official(code, bars) if len(bars[0]) >= 7 else bars
            with self.lock:
                self.hist[code] = bars[-330:]
                self.hist_meta[code] = now_tw().strftime("%Y-%m-%dT%H:%M")
                if len(bars[0]) >= 7:
                    self.yahoo_meta[code] = today_iso()
            write_json(self._p("hist", f"{code}.json"), bars[-330:])
            return True
        return False

    # ------------------------------------------------------------------ 三大法人
    def update_chips(self):
        dates = self.recent_trading_dates(5)
        hour = now_tw().hour + now_tw().minute / 60
        for d in dates:
            if (self.chips.get(d) or {}).get("__twse__"):
                continue  # 這一天的上市法人資料已經抓過
            if d == today_iso() and hour < 16:
                continue  # 當日法人資料約下午 3~4 點後公布
            try:
                m = src.fetch_t86(self.http, d.replace("-", ""))
                if m:
                    with self.lock:
                        self.chips.setdefault(d, {}).update(m)
                        self.chips[d]["__twse__"] = 1
                    write_json(self._p("chips", f"{d}.json"), self.chips[d])
                    self.src_ok("證交所三大法人（T86）", f"{d} {len(m)} 檔")
            except Exception as e:
                self.src_fail("證交所三大法人（T86）", e)
                break
        try:
            d, m = src.fetch_tpex_insti(self.http)
            if d and m:
                with self.lock:
                    self.chips.setdefault(d, {}).update(m)
                write_json(self._p("chips", f"{d}.json"), self.chips[d])
                self.src_ok("櫃買中心三大法人", f"{d} {len(m)} 檔")
        except Exception as e:
            self.src_fail("櫃買中心三大法人", e)
        with self.lock:
            for k in sorted(self.chips)[:-140]:
                del self.chips[k]

    def chips_for(self, code, n=5, asof=None):
        days = sorted(d for d in self.chips if not asof or d <= asof)[-n:]
        out = []
        for d in days:
            x = self.chips[d].get(code)
            if isinstance(x, dict):
                out.append({"date": d, **x})
        return out

    # ------------------------------------------------------------------ 新聞
    def update_news(self, codes):
        now = time.time()
        todo = [c for c in codes if now - (self.news.get(c) or {}).get("fetched", 0) > self.cfg["news_refresh_minutes"] * 60]
        for k, code in enumerate(todo):
            self.set_status("news", f"抓取個股新聞 {k + 1}/{len(todo)}", k + 1, len(todo))
            self.fetch_stock_news(code)
        self._save_news()

    def fetch_stock_news(self, code):
        u = self.universe.get(code) or {}
        name = u.get("name", "")
        base = name.replace("-KY", "").replace("*", "").strip()
        try:
            items = src.fetch_google_news(self.http, f"{base} {code}" if base else code)
            s = nw.summarize(items, code, name)
            with self.lock:
                self.news[code] = {"fetched": time.time(), **s}
            self.src_ok("Google 新聞（個股）", f"{code} {s['n']} 則")
        except Exception as e:
            self.src_fail("Google 新聞（個股）", e)

    def update_market_news(self):
        if time.time() - self.market_news.get("fetched", 0) < 15 * 60:
            return
        items = []
        try:
            items += src.fetch_google_news(self.http, "台股", days=2)
            self.src_ok("Google 新聞（台股）", f"{len(items)} 則")
        except Exception as e:
            self.src_fail("Google 新聞（台股）", e)
        try:
            cn = src.fetch_cnyes_news(self.http)
            items += cn
            self.src_ok("鉅亨網台股新聞", f"{len(cn)} 則")
        except Exception as e:
            self.src_fail("鉅亨網台股新聞", e)
        if not items:
            return
        s = nw.summarize(items)
        names = sorted(((u["name"], c) for c, u in self.universe.items() if u["type"] == "stock" and len(u["name"]) >= 2),
                       key=lambda x: -len(x[0]))
        for it in s["items"]:
            tags = []
            for nm, c in names:
                if nm in it["title"] and c not in tags:
                    tags.append(c)
                if len(tags) >= 3:
                    break
            it["codes"] = tags
        s["items"] = s["items"][:40] if len(s["items"]) > 12 else s["items"]
        with self.lock:
            self.market_news = {"fetched": time.time(), **s}
        self._save_news()

    def _save_news(self):
        with self.lock:
            write_json(self._p("news.json"), {"stocks": self.news, "market": self.market_news})

    # ================================================================== 即時行情
    def _rt_loop(self):
        while not self.stop.is_set():
            try:
                if self.demo:
                    if self.demo_live:
                        from .demo import tick
                        tick(self)
                        self.daytrade.tick()
                    time.sleep(3)
                    continue
                ph = market_phase()
                if not self.universe:
                    time.sleep(3)
                    continue
                if ph in ("open", "preopen"):
                    chans = [f"tse_t00.tw", "otc_o00.tw"]
                    with self.lock:
                        pri = [c for c in self.rt_priority if c in self.universe][:self.cfg["rt_priority_count"]]
                    chans += [src.mis_channel(c, self.universe[c]["market"]) for c in pri]
                    for k in range(0, len(chans), 60):
                        self.poll_mis(chans[k:k + 60])
                    if self.is_holiday():
                        time.sleep(120)
                        continue
                    self.daytrade.tick()   # 當沖模式：記錄分鐘價、檢查提醒
                    for _ in range(2):
                        self.poll_mis(self._next_chunk())
                    time.sleep(1)
                elif self._need_sweep():
                    log("同步全市場最新報價…")
                    chans = ["tse_t00.tw", "otc_o00.tw"]
                    self.poll_mis(chans)
                    codes = sorted(self.universe)
                    for k in range(0, len(codes), 60):
                        self.poll_mis([src.mis_channel(c, self.universe[c]["market"]) for c in codes[k:k + 60]])
                        if self.stop.is_set():
                            break
                    self.last_sweep = now_tw()
                    with self.lock:
                        write_json(self._p("snap.json"), self.snap)
                    log("全市場報價同步完成")
                    if self.status.get("first_scan_done"):
                        self.analyze_all()
                        self.rank()
                elif self._maybe_poll_official():
                    if self.status.get("first_scan_done"):
                        self.analyze_all()
                        self.rank()
                else:
                    time.sleep(20)
            except Exception:
                traceback.print_exc()
                time.sleep(10)

    def is_holiday(self):
        """平日但休市（國定假日、颱風假）：開盤 5 分鐘後指數日期仍不是今天。"""
        n = now_tw()
        if n.weekday() >= 5 or n.hour * 60 + n.minute < 9 * 60 + 5:
            return False
        d = (self.index.get("TAIEX") or {}).get("date")
        return bool(d) and d != today_iso()

    def phase(self):
        if self.demo:
            return "open" if self.demo_live else "closed"
        ph = market_phase()
        if ph in ("open", "post") and self.is_holiday():
            return "closed"
        return ph

    def _need_sweep(self):
        if self.last_sweep is None:
            return True
        n = now_tw()
        if n.weekday() >= 5:
            return False
        close_t = n.replace(hour=13, minute=36, second=0, microsecond=0)
        return n >= close_t and self.last_sweep < close_t

    def _next_chunk(self, size=60):
        codes = sorted(self.universe)
        if not codes:
            return []
        if self.mis_cursor >= len(codes):
            self.mis_cursor = 0
        part = codes[self.mis_cursor:self.mis_cursor + size]
        self.mis_cursor += size
        return [src.mis_channel(c, self.universe[c]["market"]) for c in part]

    def poll_mis(self, chans):
        if not chans:
            return
        try:
            quotes = src.fetch_mis(self.http, chans)
            self.src_ok("證交所即時行情（MIS）", f"{len(quotes)} 筆")
        except Exception as e:
            self.src_fail("證交所即時行情（MIS）", e)
            time.sleep(5)
            return
        with self.lock:
            for q in quotes:
                self.apply_quote(q)

    def apply_quote(self, q):
        code = q["code"]
        if code in INDEX_CH:
            key, name = INDEX_CH[code]
            price = q["price"] or (self.index.get(key) or {}).get("price")
            if price:
                self.index[key] = {"name": name, "price": price, "prev": q["prev"], "date": q["date"], "time": q["time"]}
            return
        old = self.snap.get(code) or {}
        if not q.get("date") or (old.get("date") or "") > q["date"]:
            return
        if old.get("date") == q["date"] and old.get("src") == "daily":
            # 官方收盤資料已經到了：價格、開高低、成交量以官方為準，只更新五檔與漲跌停
            for k in ("bids", "bid_vols", "asks", "ask_vols", "limit_up", "limit_down"):
                old[k] = q.get(k)
            return
        r = quotes.resolve(q, old)
        if r is None or r.get("price") is None:
            return
        price = r["price"]
        vol = q["vol"] or 0
        hi, lo, op = q["high"] or price, q["low"] or price, q["open"] or price
        snap = {
            "price": price, "prev": q["prev"], "open": q["open"], "high": q["high"], "low": q["low"], "vol": vol,
            "value": vol * 1000 * (op + hi + lo + price) / 4, "date": q["date"], "time": q["time"], "src": "mis",
            "est": bool(r.get("est")), "lt": r.get("lt"), "lt_t": r.get("lt_t"), "lt_v": r.get("lt_v"),
            "bids": q.get("bids") or [], "bid_vols": q.get("bid_vols") or [], "asks": q.get("asks") or [],
            "ask_vols": q.get("ask_vols") or [], "limit_up": q.get("limit_up"), "limit_down": q.get("limit_down"),
        }
        if r.get("no_trade"):
            snap["no_trade"] = True
        if r.get("trial") is not None:
            snap["trial"] = r["trial"]
        self.snap[code] = snap
        self._track_flow(q, price)

    def _track_flow(self, q, price):
        code, d = q["code"], q["date"]
        f = self.flow.get(code)
        if not f or f["date"] != d:
            f = self.flow[code] = {"date": d, "outer": 0.0, "inner": 0.0, "last_v": None,
                                   "bid": None, "ask": None, "since": q["time"]}
        v = q["vol"]
        if v is not None and f["last_v"] is not None and v > f["last_v"] and q["price"] is not None:
            dv = v - f["last_v"]
            pb, pa = f["bid"], f["ask"]
            if q["limit_up"] and price >= q["limit_up"]:
                f["outer"] += dv
            elif q["limit_down"] and price <= q["limit_down"]:
                f["inner"] += dv
            elif pa and price >= pa:
                f["outer"] += dv
            elif pb and price <= pb:
                f["inner"] += dv
            elif pa and pb:
                mid = (pa + pb) / 2
                if price > mid:
                    f["outer"] += dv
                elif price < mid:
                    f["inner"] += dv
                else:
                    f["outer"] += dv / 2
                    f["inner"] += dv / 2
            else:
                f["outer"] += dv / 2
                f["inner"] += dv / 2
        if v:
            # 估算當日均價：第一次看到時用（開＋高＋低＋現價）/4 代表之前的成交，之後用每次的增量 × 當時成交價累計
            if f.get("pv_v") is None:
                f["pv"] = v * ((q["open"] or price) + (q["high"] or price) + (q["low"] or price) + price) / 4
                f["pv_v"] = v
            elif v > f["pv_v"]:
                f["pv"] += (v - f["pv_v"]) * price
                f["pv_v"] = v
        if v is not None:
            f["last_v"] = v
        if q["bids"]:
            f["bid"] = q["bids"][0]
        if q["asks"]:
            f["ask"] = q["asks"][0]
        bv, av = sum(q["bid_vols"] or []), sum(q["ask_vols"] or [])
        f["depth"] = (bv / av) if bv > 0 and av > 0 else None
        f["updated"] = q["time"]

    def flow_ctx(self, code):
        f = self.flow.get(code)
        s = self.snap.get(code) or {}
        if not f or f["date"] != s.get("date"):
            return None
        total = f["outer"] + f["inner"]
        return {"outer": f["outer"], "inner": f["inner"], "total": total, "depth": f.get("depth"),
                "since": f.get("since"), "min_total": max(100.0, 0.1 * (s.get("vol") or 0))}

    # ================================================================== 分析與排名
    # 臺股盤中「累計成交量占全日比例」的近似曲線（開盤與收盤集合競價量大，中午量小）
    VOL_CURVE = [(0, 0.0), (5, 0.08), (15, 0.16), (30, 0.26), (60, 0.39), (90, 0.49), (120, 0.57),
                 (150, 0.65), (180, 0.72), (210, 0.79), (240, 0.86), (265, 0.93), (270, 1.0)]

    def clock(self):
        if self.demo and self.demo_live:
            return now_tw().replace(hour=10, minute=32, second=0, microsecond=0)
        return now_tw()

    def volume_fraction(self):
        n = self.clock()
        m = n.hour * 60 + n.minute + n.second / 60 - 9 * 60
        if m >= 270:
            return 1.0
        f = 0.0
        for (m0, f0), (m1, f1) in zip(self.VOL_CURVE, self.VOL_CURVE[1:]):
            if m0 <= m <= m1:
                f = f0 + (f1 - f0) * (m - m0) / (m1 - m0)
                break
        return max(0.12, f)

    def is_live(self):
        """盤中且已有今天的成交資料 → 前十名分成「收盤確認」與「盤中暫定」兩份。"""
        if self.phase() != "open":
            return False
        t = today_iso()
        with self.lock:
            return any(s.get("date") == t for s in self.snap.values())

    def close_date(self):
        """最近一個「整天已收完」的交易日。盤中時是昨天（上一個交易日）。"""
        ds = self.recent_trading_dates(10)
        if self.phase() in ("open", "preopen"):
            prev = [d for d in ds if d < today_iso()]
            if prev:
                return prev[-1]
        return ds[-1]

    def merged_bars(self, code, est_frac=None):
        bars = self.hist.get(code)
        if not bars:
            return None
        s = self.snap.get(code)
        if s and s.get("date") and s.get("price") and s.get("open") and s.get("high") and s.get("low") and s.get("vol"):
            vol = s["vol"]
            if est_frac and s["date"] == today_iso():
                vol = vol / est_frac  # 盤中換算成預估全日量
            bar = [s["date"], s["open"], s["high"], s["low"], s["price"], int(vol * 1000)]
            if bars[-1][0] == s["date"]:
                bars = bars[:-1] + [bar]
            elif bars[-1][0] < s["date"]:
                bars = bars + [bar]
        return bars

    def bars_asof(self, code, asof):
        bars = self.merged_bars(code)
        if not bars or not asof:
            return bars
        return [b for b in bars if b[0] <= asof]

    def news_ctx(self, code):
        n = self.news.get(code)
        if n and time.time() - n.get("fetched", 0) < 12 * 3600:
            return n
        return None

    def build_ctx(self, code, rs_pct=None, asof=None, est=False, regime=None):
        f = self.flow.get(code) or {}
        flow = self.flow_ctx(code) if (asof is None or f.get("date") == asof) else None
        return {"rs_pct": rs_pct, "market": regime if asof else self.regime,
                "chips": self.chips_for(code, asof=asof) or None, "flow": flow,
                "news": self.news_ctx(code), "vol_est": est}

    def regime_asof(self, asof):
        th = [b for b in (self.index_hist.get("TAIEX") or []) if b[0] <= asof]
        return self._regime_from([b[4] for b in th])

    def _est_frac(self):
        if self.cfg.get("volume_estimate", True) and self.is_live():
            return self.volume_fraction()
        return None

    def _analyze(self, codes=None, asof=None, est_frac=None):
        with self.lock:
            if not asof:
                self._update_regime()
            getb = (lambda c: self.bars_asof(c, asof)) if asof else (lambda c: self.merged_bars(c, est_frac))
            allbars = {c: getb(c) for c in self.hist if c in self.universe}
            regime = self.regime_asof(asof) if asof else self.regime
        rets = {c: b[-1][4] / b[-21][4] - 1 for c, b in allbars.items() if b and len(b) > 21 and b[-21][4]}
        rs_sorted = sorted(rets.values())
        res = {}
        for code in (codes if codes is not None else list(allbars)):
            bars = allbars.get(code)
            if not bars or len(bars) < an.MIN_BARS:
                continue
            try:
                F = an.compute_features(bars)
            except Exception:
                continue
            rs = bisect.bisect_left(rs_sorted, rets[code]) / len(rs_sorted) if code in rets and len(rs_sorted) > 50 else None
            with self.lock:
                ctx = self.build_ctx(code, rs, asof, bool(est_frac), regime)
            r = an.evaluate(F, None, ctx)
            if not r:
                continue
            n = F["n"]
            res[code] = {"score": r["score"], "raw": r["raw"], "signals": r["signals"], "flags": r["flags"],
                         "avg_value": sum(F["c"][k] * F["v"][k] for k in range(n - 20, n)) / 20,
                         "date": F["d"][-1], "price": F["c"][-1]}
        return res, (rets, rs_sorted)

    def analyze_all(self, codes=None):
        est = self._est_frac()
        res, rs = self._analyze(codes, None, est)
        with self.lock:
            if codes is None:
                self.analysis = res
            else:
                self.analysis.update(res)
            self._rets, self._rets_sorted = rs
            self._est = est
        self.last_analyze = time.time()

    def _eligible(self, code, r):
        u = self.universe.get(code) or {}
        if u.get("type") == "etf" and not self.cfg["include_etf_in_top"]:
            return False
        if (r.get("price") or 0) < self.cfg["top_min_price"]:
            return False
        return r["avg_value"] >= self.cfg["top_min_avg_value"] and not r["flags"]["dist"]

    def _build_payload(self, results, kind, date, asof=None, rs=None, est=None):
        with self.lock:
            ranked = sorted(((c, r) for c, r in results.items() if self._eligible(c, r)),
                            key=lambda x: (x[1]["score"], x[1]["raw"], x[1]["avg_value"]), reverse=True)
            cands = [c for c, _ in ranked]
            alerts = sorted(((c, r) for c, r in results.items()
                             if r["flags"]["dist"] and r["avg_value"] >= self.cfg["top_min_avg_value"]),
                            key=lambda x: x[1]["raw"])[:8]
        cards = [self.stock_card(c, rank=k + 1, asof=asof, result=results[c], rs=rs, est=est)
                 for k, c in enumerate(cands[:10])]
        alert_rows = []
        for c, r in alerts:
            u, s = self.universe.get(c, {}), self.snap.get(c, {})
            dsig = next((x for x in r["signals"] if x["key"] == "dist"), None)
            price = r.get("price") if asof else s.get("price")
            chgp = None
            if asof:
                b = self.bars_asof(c, asof)
                chgp = r2((b[-1][4] / b[-2][4] - 1) * 100) if b and len(b) > 1 else None
            else:
                chgp = self._chgp(s)
            alert_rows.append({"code": c, "name": u.get("name"), "price": price, "chgp": chgp,
                               "score": r["score"], "reason": dsig["label"] if dsig else ""})
        payload = {"kind": kind, "date": date, "generated": self.clock().strftime("%Y-%m-%d %H:%M:%S"),
                   "top": [x for x in cards if x], "alerts": alert_rows, "analyzed": len(results),
                   "eligible": len(cands), "est_frac": r2(est, 3) if est else None}
        return payload, cands

    def rank(self):
        live = self.is_live()
        if live:
            payload, cands = self._build_payload(self.analysis, "live", today_iso(), est=getattr(self, "_est", None))
            with self.lock:
                self.live_payload = payload
                self.live_cands = cands
            self.refresh_close()
            try:
                self.daytrade.on_live_rank(cands)
            except Exception:
                traceback.print_exc()
        else:
            cd = self.close_date()
            payload, cands = self._build_payload(self.analysis, "close", cd)
            with self.lock:
                self.live_payload = None
                self.live_cands = []
                self.close_payload = payload
            self._save_close(payload)
            self.review.on_close_list(payload)
            try:
                self.daytrade.on_close()
            except Exception:
                traceback.print_exc()
        with self.lock:
            pri, seen = [], set()
            keep = [c["code"] for c in (self.close_payload or {}).get("top", [])]
            for c in self.daytrade.watch_codes() + keep + cands:   # 當沖候選與持倉優先，報價才會幾秒更新一次
                if c not in seen:
                    seen.add(c)
                    pri.append(c)
            self.rt_priority = pri[: self.cfg["rt_priority_count"]]
        return cands

    def refresh_close(self, force=False):
        """盤中：載入（或補算）上一個交易日收盤後的名單，整天固定不變。"""
        cd = self.close_date()
        if not force and self.close_payload and self.close_payload.get("date") == cd:
            return
        saved = None if (force or self.demo) else read_json(self._p("close_lists", f"{cd}.json"))
        if saved and saved.get("top"):
            with self.lock:
                self.close_payload = saved
            return
        res, rs = self._analyze(None, asof=cd)
        payload, _ = self._build_payload(res, "close", cd, asof=cd, rs=rs)
        with self.lock:
            self.close_payload = payload
            self.close_analysis = res
        self._save_close(payload)
        if cd not in self.review.lists:
            self.review.on_close_list(payload)

    def _save_close(self, payload):
        if self.demo or not payload.get("top"):
            return
        write_json(self._p("close_lists", f"{payload['date']}.json"), payload)
        try:
            files = sorted(f for f in os.listdir(self._p("close_lists")) if f.endswith(".json"))
            for f in files[:-40]:
                os.remove(self._p("close_lists", f))
        except OSError:
            pass

    def prev_close_list(self, before):
        """前一個交易日收盤後的名單（用來回顧它隔天的實際表現）。沒存過就用歷史資料重建一次。"""
        prev_days = [d for d in self.recent_trading_dates(15) if d < before]
        if not prev_days:
            return None
        pd = prev_days[-1]
        cache = getattr(self, "_prev_cache", None)
        if cache and cache.get("date") == pd:
            return cache
        saved = None if self.demo else read_json(self._p("close_lists", f"{pd}.json"))
        rec = self.review.lists.get(pd)
        if not (saved and saved.get("top")) and rec and rec.get("picks"):
            # 用復盤存檔的名單，確保首頁和復盤頁看到的是同一份
            saved = {"date": pd, "reconstructed": bool(rec.get("reconstructed")),
                     "top": [{"rank": p["rank"], "code": p["code"], "name": p["name"], "score": p.get("score"),
                              "price": p.get("close"), "levels": {"invalid": p.get("invalid")}} for p in rec["picks"]]}
        if not (saved and saved.get("top")):
            res, rs = self._analyze(None, asof=pd)
            saved, _ = self._build_payload(res, "close", pd, asof=pd, rs=rs)
            saved["reconstructed"] = True
            self._save_close(saved)
        self._prev_cache = saved
        return saved

    def api_top(self):
        live = self.is_live()
        with self.lock:
            close, livep = self.close_payload, self.live_payload
        if close is None and livep is None:
            return {"pending": True, "mode": "live" if live else "close"}
        track = None
        try:
            if close and live:
                track = {"mode": "live", "list_date": close["date"], "trade_date": today_iso(),
                         **self.track_rows(close, today_iso(), True)}
            elif close:
                prev = self.prev_close_list(close["date"])
                if prev:
                    track = {"mode": "review", "list_date": prev["date"], "trade_date": close["date"],
                             "reconstructed": bool(prev.get("reconstructed")),
                             **self.track_rows(prev, close["date"], False)}
        except Exception:
            traceback.print_exc()
        return {"mode": "live" if live else "close", "close": close, "live": livep if live else None,
                "track": track, "now": self.clock().strftime("%H:%M")}

    def top_payload_list(self):
        out = list((self.close_payload or {}).get("top", []))
        out += (self.live_payload or {}).get("top", [])
        return out

    # ------------------------------------------------------------------ 收盤名單的隔日追蹤
    def track_rows(self, payload, trade_date, live):
        chase = self.cfg.get("chase_gap_pct", 3.0)
        rows, n_up, chs = [], 0, []
        counts = {}
        with self.lock:
            for c in payload.get("top", []):
                code = c["code"]
                s = self.snap.get(code) or {}
                row = {"code": code, "name": c["name"], "rank": c["rank"], "list_score": c.get("score"),
                       "invalid": (c.get("levels") or {}).get("invalid")}
                base = s.get("prev") or c.get("price")
                if s.get("date") != trade_date or not s.get("price") or not base:
                    row.update(status="wait", label="尚未成交", kind="info")
                    rows.append(row)
                    continue
                price, op, hi, lo = s["price"], s.get("open"), s.get("high"), s.get("low")
                gap = (op / base - 1) * 100 if op else None
                chg = (price / base - 1) * 100
                hchg = (hi / base - 1) * 100 if hi else None
                f = self.flow_ctx(code)
                outer = f["outer"] / f["total"] if f and f["total"] > 0 else None
                inv = row["invalid"]
                broke = bool(inv and lo and lo < inv)
                now_score = (self.analysis.get(code) or {}).get("score")
                row.update(price=price, gap=r2(gap), chgp=r2(chg), high_chgp=r2(hchg), open=op, est=bool(s.get("est")),
                           outer=r2(outer * 100, 0) if outer is not None else None,
                           now_score=now_score, broke=broke, time=s.get("time"))
                if live:
                    if inv and price < inv:
                        st = ("broken", "跌破失效價", "bear")
                    elif gap is not None and gap >= chase and price >= op * 0.995:
                        st = ("chase", f"跳空 {gap:.1f}% 勿追", "warn")
                    elif gap is not None and gap >= 1 and price < op * 0.985:
                        st = ("fade", "開高走低", "warn")
                    elif chg >= 0 and (outer is None or outer >= 0.5) and (now_score is None or now_score >= 45):
                        st = ("ok", "條件符合", "bull")
                    elif chg < 0:
                        st = ("weak", "走勢偏弱", "info")
                    else:
                        st = ("watch", "觀察中", "info")
                else:
                    st = ("result", ("曾跌破失效價" if broke else "收盤 " + (f"+{chg:.2f}%" if chg > 0 else f"{chg:.2f}%")),
                          "bear" if broke else ("bull" if chg > 0 else "info"))
                    n_up += chg > 0
                    chs.append(chg)
                row.update(status=st[0], label=st[1], kind=st[2])
                counts[st[0]] = counts.get(st[0], 0) + 1
                rows.append(row)
        summary = {"counts": counts}
        if not live and chs:
            summary.update(avg=r2(sum(chs) / len(chs)), up=n_up, n=len(chs), broke=sum(1 for r in rows if r.get("broke")))
        return {"rows": rows, "summary": summary}

    @staticmethod
    def _chgp(s):
        p, y = s.get("price"), s.get("prev")
        return r2((p / y - 1) * 100) if p and y else None

    def stock_card(self, code, rank=None, full=False, asof=None, result=None, rs=None, est=None):
        with self.lock:
            u = dict(self.universe.get(code) or {})
            s = dict(self.snap.get(code) or {})
            bars = self.bars_asof(code, asof) if asof else self.merged_bars(code)
            ev_bars = self.merged_bars(code, est) if (est and not asof) else bars
            r = result if result is not None else (None if asof else self.analysis.get(code))
            fl = self.flow.get(code) or {}
            flow = self.flow_ctx(code) if (asof is None or fl.get("date") == asof) else None
            chips = self.chips_for(code, asof=asof)
            news = self.news_ctx(code) or self.news.get(code)
            rets, rsorted = rs if rs else (getattr(self, "_rets", {}), getattr(self, "_rets_sorted", []))
            rsv = bisect.bisect_left(rsorted, rets[code]) / len(rsorted) if code in rets and len(rsorted) > 50 else None
            ctx = self.build_ctx(code, rsv, asof, bool(est and not asof), self.regime_asof(asof) if asof else None)
        if not bars or len(bars) < 2:
            return None
        F = an.compute_features(bars)
        Fe = an.compute_features(ev_bars) if ev_bars is not bars else F
        if full or r is None:
            rr = an.evaluate(Fe, None, ctx)
            if rr:
                r = {**(r or {}), "score": rr["score"], "raw": rr["raw"], "signals": rr["signals"], "flags": rr["flags"],
                     "avg_value": (r or {}).get("avg_value") or sum(F["c"][k] * F["v"][k] for k in range(F["n"] - 20, F["n"])) / 20}
        sig = sorted((r or {}).get("signals", []), key=lambda x: -abs(x["pts"]))
        pts = an.backtest_points(F) if F["n"] >= 150 else []
        bt = None
        if pts:
            thr = 60 if sum(1 for p in pts if p[0] >= 60) >= 5 else 45
            sel = [p for p in pts if p[0] >= thr]
            if sel:
                bt = {"threshold": thr, "n": len(sel), "win10": r2(sum(1 for p in sel if p[2] > 0) / len(sel) * 100, 0),
                      "avg10": r2(sum(p[2] for p in sel) / len(sel) * 100, 1),
                      "base_win10": r2(sum(1 for p in pts if p[2] > 0) / len(pts) * 100, 0)}
        if asof:
            last = bars[-1]
            q = {"price": last[4], "prev": bars[-2][4], "open": last[1], "high": last[2], "low": last[3],
                 "vol": last[5] / 1000, "time": None, "date": last[0], "src": "close"}
        else:
            q = {"price": s.get("price") or F["c"][-1], "prev": s.get("prev") or F["c"][-2], "open": s.get("open"),
                 "high": s.get("high"), "low": s.get("low"), "vol": s.get("vol"), "time": s.get("time"),
                 "date": s.get("date") or F["d"][-1], "src": s.get("src") if s.get("price") else "close",
                 "est": bool(s.get("est")), "lt": s.get("lt"), "lt_t": s.get("lt_t"), "no_trade": bool(s.get("no_trade"))}
        card = {
            "rank": rank, "code": code, "name": u.get("name") or code, "market": u.get("market"),
            "type": u.get("type"), "industry": u.get("industry") or "", **q,
            "score": (r or {}).get("score"), "grade": an.grade((r or {}).get("score") or 0) if r else None,
            "flags": (r or {}).get("flags"), "signals": sig,
            "levels": an.key_levels(F) if F["n"] >= 30 else {},
            "chart": an.chart_payload(F, n=250 if full else 120, full=full, signals=sig),
            "chips": chips, "flow": flow, "backtest": bt, "asof": asof,
            "news": {"score": news.get("score"), "n": news.get("n"), "items": news.get("items", [])[: (12 if full else 4)]} if news else None,
        }
        card["chgp"] = self._chgp(card)
        if ev_bars is not bars:
            card["vol_est"] = {"est": r2(ev_bars[-1][5] / 1000, 0), "actual": r2(s.get("vol"), 0), "frac": r2(est, 3)}
        if full:
            card["depth"] = {k: s.get(k) for k in ("bids", "bid_vols", "asks", "ask_vols", "limit_up", "limit_down")}
            card["avg_value"] = (r or {}).get("avg_value")
        return card

    # ================================================================== 模型回測
    def update_model(self):
        key = self.last_trading_date()
        if self.model and self.model.get("key") == key and self.model.get("demo") == self.demo:
            return
        with self.lock:
            codes = sorted(((c, r["avg_value"]) for c, r in self.analysis.items()
                            if (self.universe.get(c) or {}).get("type") == "stock"), key=lambda x: -x[1])
            codes = [c for c, _ in codes[: self.cfg["model_max_stocks"]]]
            hist = {c: self.hist[c] for c in codes if c in self.hist}
        edges = [0, 20, 40, 60, 80, 101]
        buckets = [[] for _ in range(len(edges) - 1)]
        allp = []
        for k, c in enumerate(codes):
            bars = hist.get(c)
            if not bars or len(bars) < 160:
                continue
            if k % 25 == 0:
                self.set_status("model", f"回測評分模型 {k}/{len(codes)}", k, len(codes))
            F = an.compute_features(bars)
            for sc, f5, f10 in an.backtest_points(F, step=2):
                allp.append((f5, f10))
                for b in range(len(edges) - 1):
                    if edges[b] <= sc < edges[b + 1]:
                        buckets[b].append((f5, f10))
                        break

        def stat(lst):
            if not lst:
                return {"n": 0}
            n = len(lst)
            return {"n": n, "win5": r2(sum(1 for x in lst if x[0] > 0) / n * 100, 1),
                    "win10": r2(sum(1 for x in lst if x[1] > 0) / n * 100, 1),
                    "avg5": r2(sum(x[0] for x in lst) / n * 100, 2), "avg10": r2(sum(x[1] for x in lst) / n * 100, 2)}
        self.model = {
            "key": key, "demo": self.demo, "stocks": len(hist), "generated": now_tw().strftime("%Y-%m-%d %H:%M"),
            "buckets": [{"range": f"{edges[b]}–{min(edges[b + 1], 100)}", **stat(buckets[b])} for b in range(len(buckets))],
            "baseline": stat(allp),
        }
        write_json(self._p("model.json"), self.model)

    # ================================================================== API 資料
    def api_status(self):
        with self.lock:
            idx = {}
            for key in ("TAIEX", "TPEX"):
                live = self.index.get(key) or {}
                th = self.index_hist.get(key) or []
                price = live.get("price") or (th[-1][4] if th else None)
                prev = live.get("prev") or (th[-2][4] if len(th) > 1 else None)
                closes = [b[4] for b in th[-60:]]
                if live.get("price") and live.get("date") == today_iso() and (not th or th[-1][0] != today_iso()):
                    closes = closes[1:] + [live["price"]]
                idx[key] = {"name": "加權指數" if key == "TAIEX" else "櫃買指數", "price": price, "prev": prev,
                            "chgp": r2((price / prev - 1) * 100) if price and prev else None,
                            "time": live.get("time"), "spark": closes}
            up = down = flat = 0
            for code, u in self.universe.items():
                if u.get("type") != "stock":
                    continue
                cp = self._chgp(self.snap.get(code) or {})
                if cp is None:
                    continue
                if cp > 0:
                    up += 1
                elif cp < 0:
                    down += 1
                else:
                    flat += 1
            return {
                **self.status, "market_phase": self.phase(), "now": self.clock().strftime("%Y-%m-%d %H:%M:%S"),
                "breadth": {"up": up, "down": down, "flat": flat},
                "live": self.is_live(), "close_date": (self.close_payload or {}).get("date"),
                "alerts": self.daytrade.recent_alerts(),
                "demo": self.demo, "regime": self.regime, "index": idx, "universe": len(self.universe),
                "with_history": len(self.hist), "analyzed": len(self.analysis),
                "model": self.model, "market_news": self.market_news.get("items", [])[:20],
            }

    def api_market(self):
        with self.lock:
            rows = []
            up = down = flat = 0
            total_value = 0.0
            inds = {}
            for code, u in self.universe.items():
                s = self.snap.get(code) or {}
                p = s.get("price")
                chgp = self._chgp(s)
                a = self.analysis.get(code)
                f = self.flow_ctx(code)
                outer = r2(f["outer"] / f["total"] * 100, 0) if f and f["total"] > 0 else None
                rows.append([code, u["name"], u["market"], u["type"], u.get("industry") or "", p,
                             r2(p - s["prev"]) if p and s.get("prev") else None, chgp, r2(s.get("vol"), 0),
                             r2((s.get("value") or 0) / 1e8, 2), s.get("open"), s.get("high"), s.get("low"),
                             s.get("prev"), a["score"] if a else None, outer, 1 if s.get("est") else 0,
                             s.get("lt_t") or s.get("time")])
                if chgp is not None:
                    if chgp > 0:
                        up += 1
                    elif chgp < 0:
                        down += 1
                    else:
                        flat += 1
                if u["type"] == "stock" and chgp is not None:
                    val = s.get("value") or 0
                    total_value += val
                    ind = u.get("industry") or "其他"
                    g = inds.setdefault(ind, {"name": ind, "value": 0.0, "wsum": 0.0, "n": 0, "up": 0, "down": 0, "lead": None})
                    g["value"] += val
                    g["wsum"] += chgp * val
                    g["n"] += 1
                    g["up"] += chgp > 0
                    g["down"] += chgp < 0
                    if val > 0 and (g["lead"] is None or val > g["lead"][2]):
                        g["lead"] = (code, u["name"], val, chgp)
            industries = []
            for g in inds.values():
                if g["n"] < 2:
                    continue
                industries.append({"name": g["name"], "value": r2(g["value"] / 1e8, 1), "n": g["n"], "up": g["up"],
                                   "down": g["down"], "chgp": r2(g["wsum"] / g["value"], 2) if g["value"] else 0,
                                   "lead": g["lead"][:2] + (r2(g["lead"][3]),) if g["lead"] else None})
            industries.sort(key=lambda x: -x["value"])
            return {
                "fields": ["code", "name", "market", "type", "industry", "price", "chg", "chgp", "vol", "value",
                          "open", "high", "low", "prev", "score", "outer", "est", "qtime"],
                "rows": rows, "breadth": {"up": up, "down": down, "flat": flat, "value": r2(total_value / 1e8, 0)},
                "industries": industries, "generated": self.clock().strftime("%Y-%m-%d %H:%M:%S"),
                "live": self.is_live(), "close_date": (self.close_payload or {}).get("date"),
            }

    def api_stock(self, code):
        code = (code or "").strip().upper()
        if code not in self.universe:
            return {"error": f"找不到股票代號 {code}"}
        if not self.ensure_history(code):
            with self.lock:
                u, s = self.universe[code], self.snap.get(code, {})
            return {"code": code, "name": u["name"], "industry": u.get("industry"), "market": u["market"],
                    "price": s.get("price"), "chgp": self._chgp(s), "error": "無法取得歷史 K 線（資料來源暫時無回應），請稍後再試。"}
        if not self.demo and market_phase() in ("open", "preopen"):
            self.poll_mis([src.mis_channel(code, self.universe[code]["market"])])
        if not self.demo and time.time() - (self.news.get(code) or {}).get("fetched", 0) > self.cfg["news_refresh_minutes"] * 60:
            self.fetch_stock_news(code)
        live = self.is_live()
        est = self._est_frac() if live else None
        card = self.stock_card(code, full=True, est=est)
        if card is None:
            return {"error": "資料不足"}
        card["live"] = live
        if live:
            cd = self.close_date()
            with self.lock:
                bars = self.bars_asof(code, cd)
                ctx = self.build_ctx(code, None, cd, False, self.regime_asof(cd))
            if bars and len(bars) >= an.MIN_BARS:
                r = an.evaluate(an.compute_features(bars), None, ctx)
                if r:
                    card["close_eval"] = {"date": cd, "score": r["score"], "grade": an.grade(r["score"])}
        return card

    # ================================================================== 多週期（30 分／60 分／日）
    TF = {"30m": (30, "1mo", "30 分"), "60m": (60, "3mo", "60 分")}

    def intraday_bars(self, code, tf):
        """30 分或 60 分 K 線：Yahoo 的分時資料（有快取），最後一根用即時報價補上。"""
        size, rng, _ = self.TF[tf]
        key = (code, tf)
        with self.lock:
            cached = self.intra.get(key)
            u = self.universe[code]
        ttl = 90 if self.phase() in ("open", "preopen") else 1800
        if cached and time.time() - cached["t"] <= ttl:
            bars = cached["bars"]
        elif cached is None and time.time() - self.intra.get(("fail",) + key, 0) < 60:
            raise RuntimeError("分時資料剛剛抓取失敗，一分鐘內不重試")
        else:
            try:
                if self.demo:
                    from .demo import intraday
                    bars = intraday(self, code, size)
                else:
                    sym = f"{code}.TW" if u["market"] == "TWSE" else f"{code}.TWO"
                    bars = src.fetch_yahoo_intraday(self.http, sym, f"{size}m", rng)
                    self.src_ok("Yahoo 分時 K 線", f"{code} {tf}：{len(bars)} 根")
            except Exception as e:
                if not self.demo:
                    self.src_fail("Yahoo 分時 K 線", e)
                if not cached:
                    with self.lock:
                        self.intra[("fail",) + key] = time.time()
                    raise
                bars = cached["bars"]   # 抓不到就先用舊的
            with self.lock:
                self.intra[key] = {"t": time.time(), "bars": bars}
                self.intra.pop(("fail",) + key, None)
                data_keys = [k for k in self.intra if k[0] != "fail"]
                if len(data_keys) > 80:
                    for k in sorted(data_keys, key=lambda k: self.intra[k]["t"])[:20]:
                        self.intra.pop(k, None)
        with self.lock:
            return self._patch_intraday(code, bars, size)

    def _patch_intraday(self, code, bars, size):
        """Yahoo 的分時資料會延遲，用手上的即時報價更新（或補上）目前這一根。"""
        s = self.snap.get(code) or {}
        today = today_iso()
        if s.get("date") != today or not s.get("price"):
            return bars
        t = s.get("time") or ""
        try:
            minutes = int(t[:2]) * 60 + int(t[3:5])
        except ValueError:
            minutes = 13 * 60 + 30          # 收盤後的報價
        label = src.bucket_label(today, minutes, size)
        price = s["price"]
        total = int((s.get("vol") or 0) * 1000)
        out = [list(b) if b[0][:10] == today else b for b in bars if b[0] <= label]
        todays = [b for b in out if b[0][:10] == today]
        cur = next((b for b in todays if b[0] == label), None)
        if cur is None:
            if todays:
                o = todays[-1][4]
                cur = [label, o, max(o, price), min(o, price), price, 0]
            else:   # Yahoo 還沒有今天的資料：先用一根代表今天到目前為止
                cur = [label, s.get("open") or price, s.get("high") or price, s.get("low") or price, price, 0]
            out.append(cur)
            todays.append(cur)
        cur[2], cur[3], cur[4] = max(cur[2], price), min(cur[3], price), price
        others = sum(b[5] for b in todays if b is not cur)
        if total - others > cur[5]:
            cur[5] = total - others
        return out

    def api_stock_tf(self, code, tf):
        code = (code or "").strip().upper()
        if code not in self.universe:
            return {"error": f"找不到股票代號 {code}"}
        if tf not in ("30m", "60m"):
            tf = "d"
        out = {"code": code, "tf": tf, "mtf": []}
        series = {}
        for t in self.TF:
            try:
                series[t] = self.intraday_bars(code, t)
            except Exception as e:
                log(f"分時 K 線 {code} {t} 取得失敗：{e}")
        with self.lock:
            series["d"] = self.merged_bars(code) or []
            s = dict(self.snap.get(code) or {})
            pos = next((p for p in self.daytrade.positions if p["code"] == code), None)
            today = s.get("date") == today_iso() and s.get("price")
            if today:
                out["day"] = {"open": s.get("open"), "prev": s.get("prev"), "vwap": r2(self.daytrade.vwap(code, s), 2)}
        for t, name in (("30m", "30 分"), ("60m", "60 分"), ("d", "日")):
            bars = series.get(t) or []
            if len(bars) < 30:
                out["mtf"].append({"tf": t, "name": name, "state": None, "label": "資料不足", "points": []})
                continue
            F = an.compute_features(bars)
            out["mtf"].append({"tf": t, "name": name, **an.tf_summary(F)})
            if t == tf and t != "d":
                out["chart"] = an.chart_payload(F, n=len(bars), full=True)
        out["mtf_text"] = an.mtf_text(out["mtf"])
        if pos:
            out["position"] = {k: pos.get(k) for k in ("entry", "stop", "target", "lots")}
        out["live"] = self.is_live()
        out["updated"] = self.clock().strftime("%H:%M")
        if tf != "d" and "chart" not in out:
            out["error"] = "目前拿不到分時 K 線（資料來源暫時無回應，或這檔股票沒有分時資料），請稍後再試。"
        return out

    def api_settings(self, data):
        changed = {}
        if "fee_discount" in data:
            try:
                v = float(data["fee_discount"])
            except (TypeError, ValueError):
                return {"error": "折扣格式不正確"}
            if v > 1:
                v = v / 10  # 輸入 2.8 代表 2.8 折
            if not 0 < v <= 1:
                return {"error": "折扣需介於 0.1 到 10 折之間"}
            self.cfg["fee_discount"] = round(v, 4)
            changed["fee_discount"] = self.cfg["fee_discount"]
        if changed:
            st = read_json(self._p("settings.json"), {}) or {}
            st.update(changed)
            write_json(self._p("settings.json"), st)
            with self.review.lock:
                self.review._cache = None
        return {"ok": True, **changed}

    def api_diag(self):
        with self.lock:
            return {"sources": self.sources, "hosts": self.http.stats, "config": self.cfg,
                    "status": self.status, "chips_days": sorted(self.chips), "quality": self.quote_quality()}

    def quote_quality(self):
        """報價品質摘要：今天的報價有幾檔是成交價、幾檔是推估，官方收盤資料核對結果。"""
        with self.lock:
            d = self.last_trading_date()
            cnt = {"exact": 0, "est": 0, "no_trade": 0, "official": 0}
            for c, x in self.snap.items():
                if x.get("date") != d or c not in self.universe:
                    continue
                if x.get("src") == "daily":
                    cnt["official"] += 1
                elif x.get("no_trade"):
                    cnt["no_trade"] += 1
                elif x.get("est"):
                    cnt["est"] += 1
                else:
                    cnt["exact"] += 1
            return {"date": d, "counts": cnt, "official_got": dict(self.official_got),
                    "official": list(self.quality.get("official") or [])[-5:], "repair": self.quality.get("repair"),
                    "archive_days": len(self.official)}

    def api_pricecheck(self, code):
        """核對報價：重新抓一次即時報價，並列出官方收盤資料、歷史 K 線，檢查是否一致。"""
        from .daytrade import tick_size
        code = (code or "").strip().upper()
        u = self.universe.get(code)
        if not u:
            return {"error": f"找不到股票代號 {code}"}
        live, live_err = None, None
        if self.demo:
            with self.lock:
                s0 = dict(self.snap.get(code) or {})
            live = {"raw_z": "-" if s0.get("est") else s0.get("price"), "price": None if s0.get("est") else s0.get("price"),
                    "prev": s0.get("prev"), "open": s0.get("open"), "high": s0.get("high"), "low": s0.get("low"),
                    "vol": s0.get("vol"), "bids": (s0.get("bids") or [])[:1], "asks": (s0.get("asks") or [])[:1],
                    "time": s0.get("time"), "date": s0.get("date")}
        else:
            try:
                qs = src.fetch_mis(self.http, [src.mis_channel(code, u["market"])])
                live = next((x for x in qs if x["code"] == code), None)
                if live:
                    with self.lock:
                        self.apply_quote(live)
                else:
                    live_err = "證交所即時資料沒有回傳這檔股票"
            except Exception as e:
                live_err = str(e)[:200]
        with self.lock:
            s = dict(self.snap.get(code) or {})
            bars = [list(b) for b in (self.hist.get(code) or [])[-5:]]
            off = {d: list(m[code]) for d, m in self.official.items() if code in m}
        etf = u.get("type") == "etf"
        checks = []
        price = s.get("price")
        if price:
            t = tick_size(price, etf)
            ok = abs(round(price / t) * t - price) < 1e-6
            checks.append({"ok": ok, "text": f"顯示的價格 {price:g} 符合升降單位" if ok else f"顯示的價格 {price:g} 不是合法的升降單位（每檔 {t:g} 元）"})
        if price and s.get("high") and s.get("low"):
            ok = s["low"] - 1e-9 <= price <= s["high"] + 1e-9
            checks.append({"ok": ok, "text": "價格在今天的最高、最低之間" if ok else f"價格不在今天的最高 {s['high']:g}～最低 {s['low']:g} 之間"})
        for b in bars:
            row = off.get(b[0])
            if row:
                bad = self._bar_off(b, row)[0]
                if bad and self._adjusted(b, row):
                    checks.append({"ok": None, "text": f"{b[0][5:]} 日 K 收 {round(b[4], 2):g}、官方原始收盤 {row[3]:g}："
                                                       "之後有配股或減資，日 K 已等比例還原，屬正常"})
                    continue
                checks.append({"ok": not bad, "text": f"{b[0][5:]} 日 K 與官方收盤一致（收 {row[3]:g}）" if not bad
                               else f"{b[0][5:]} 日 K 收盤 {b[4]:g}，官方是 {row[3]:g}（下次更新會自動修正）"})
        prev_bar = next((b for b in reversed(bars) if s.get("date") and b[0] < s["date"]), None)
        if s.get("prev") and prev_bar:
            t = tick_size(prev_bar[4], etf)
            ok = abs(prev_bar[4] - s["prev"]) < t / 2
            checks.append({"ok": ok if ok else None,
                           "text": f"昨收 {s['prev']:g} 與日 K 前一天收盤一致" if ok else
                           f"昨收（平盤參考價）{s['prev']:g} 和日 K 前一天收盤 {prev_bar[4]:g} 不同：今天除權息就是正常的，否則代表歷史資料有誤"})
        if s.get("src") == "mis" and s.get("est"):
            checks.append({"ok": None, "text": "目前顯示的是推估價：證交所這一刻沒有揭示成交價，程式依最佳一檔買賣價推估，誤差通常在一檔以內"})
        lv = None
        if live:
            lv = {k: live.get(k) for k in ("raw_z", "price", "prev", "open", "high", "low", "vol", "time", "date")}
            lv["bid"] = (live.get("bids") or [None])[0]
            lv["ask"] = (live.get("asks") or [None])[0]
        return {
            "code": code, "name": u.get("name") or code, "market": u.get("market"), "type": u.get("type"),
            "shown": {k: s.get(k) for k in ("price", "prev", "open", "high", "low", "vol", "date", "time", "src", "est",
                                            "lt", "lt_t", "no_trade", "trial")},
            "live": lv, "live_error": live_err,
            "official": [{"date": d, "open": r[0], "high": r[1], "low": r[2], "close": r[3], "vol": r[4]}
                         for d, r in sorted(off.items())[-3:]],
            "bars": [{"date": b[0], "open": b[1], "high": b[2], "low": b[3], "close": b[4], "vol": b[5]} for b in bars[-4:]],
            "checks": checks, "now": self.clock().strftime("%H:%M:%S"),
        }
