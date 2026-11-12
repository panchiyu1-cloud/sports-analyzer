"""復盤：每日「收盤確認名單」的存檔、歷史重建，以及之後 5／10 個交易日的實際表現（已扣交易成本）。

假設的操作方式（策略 A）：名單公布後的下一個交易日以開盤價買進，持有 5 或 10 個交易日，以收盤價賣出。
策略 B：同上，但開盤跳空 ≥ chase_gap_pct 就不買；持有期間盤中跌破「訊號失效價」就停損出場。
"""
import bisect
import os
import threading
import time
import traceback

from . import analysis as an
from . import sources as src
from .util import log, now_tw, r2, read_json, today_iso, write_json

FEE_RATE = 0.001425   # 法定手續費率上限（買賣各收一次），再乘上券商折扣
TAX_STOCK = 0.003     # 股票證交稅（賣出時）
TAX_ETF = 0.001       # ETF 證交稅
TAX_DAYTRADE = 0.0015  # 現股當沖證交稅減半（適用至 2027-12-31）
HOLDS = (1, 2, 5, 10)  # 1＝當沖：隔天開盤買、當天收盤賣
PATH_DAYS = 10         # 「報酬發生在哪一天」拆解的天數
VERSION = 2  # 重建版本：2＝以「當日」成交值決定股票池、記錄訊號代碼


def _f(b):
    """還原權息係數（Yahoo adjclose / 收盤價）；沒有資料時視為 1。"""
    return b[6] / b[4] if len(b) > 6 and b[6] and b[4] else 1.0


class Review:
    def __init__(self, eng):
        self.eng = eng
        self.lists = {}
        self.status = {"building": False, "msg": "", "done": 0, "total": 0, "built": None}
        self.lock = threading.Lock()
        self._cache = None
        self._thread = None

    @property
    def dir(self):
        return self.eng._p("review")

    # ------------------------------------------------------------------ 存取
    def load(self):
        os.makedirs(self.dir, exist_ok=True)
        for fn in os.listdir(self.dir):
            if fn.endswith(".json"):
                rec = read_json(os.path.join(self.dir, fn))
                if isinstance(rec, dict) and rec.get("picks") is not None:
                    self.lists[rec["date"]] = rec

    def _save(self, rec):
        with self.lock:
            old = self.lists.get(rec["date"])
            if old and not old.get("reconstructed") and rec.get("reconstructed"):
                return  # 不用重建的版本覆蓋當天真正顯示過的名單
            self.lists[rec["date"]] = rec
            self._cache = None
        if not self.eng.demo:
            write_json(os.path.join(self.dir, f"{rec['date']}.json"), rec)

    def on_close_list(self, payload, reconstructed=False):
        if not payload or not payload.get("top"):
            return
        picks = []
        for c in payload["top"]:
            picks.append({
                "rank": c["rank"], "code": c["code"], "name": c["name"], "type": c.get("type"),
                "score": c.get("score"), "close": c.get("price"),
                "invalid": (c.get("levels") or {}).get("invalid"),
                "tags": [s["label"] for s in c.get("signals", []) if s["pts"] > 0][:3],
                "keys": [s["key"] for s in c.get("signals", []) if s["pts"] > 0],
            })
        self._save({"date": payload["date"], "reconstructed": reconstructed, "v": VERSION,
                    "saved": now_tw().strftime("%Y-%m-%d %H:%M"), "picks": picks})

    def recent_codes(self, n=15):
        with self.lock:
            ds = sorted(self.lists)[-n:]
            return {p["code"] for d in ds for p in self.lists[d]["picks"]}

    # ------------------------------------------------------------------ 建立歷史
    def ensure(self):
        """背景補齊過去 N 個交易日：先補抓上市三大法人，再重建沒有存檔的日期。"""
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._build, name="review", daemon=True)
        self._thread.start()

    def _set(self, msg, done=0, total=0, building=True):
        self.status.update(msg=msg, done=done, total=total, building=building)

    def _build(self):
        eng = self.eng
        try:
            n = int(eng.cfg.get("review_days", 105))
            cd = eng.close_date()
            dates = [d for d in eng.recent_trading_dates(n + 1) if d <= cd][-n:]
            if not dates:
                return
            if not eng.demo:
                self._backfill_chips(dates)
            missing = [d for d in dates if d not in self.lists
                       or (self.lists[d].get("reconstructed") and self.lists[d].get("v", 1) < VERSION)]
            if missing:
                self._set(f"重建過去的每日名單（{len(missing)} 天）…", 0, len(missing))
                t0 = time.time()
                recs = reconstruct(eng, missing, progress=lambda k, t: self._set(
                    f"重建過去的每日名單：計算第 {k}/{t} 檔股票", k, t))
                for rec in recs.values():
                    self._save(rec)
                log(f"復盤資料重建完成：{len(recs)} 天（{time.time() - t0:.0f} 秒）")
            self.status["built"] = now_tw().strftime("%Y-%m-%d %H:%M")
        except Exception as e:
            traceback.print_exc()
            self.status["msg"] = f"復盤資料建立失敗：{e}"
        finally:
            self.status["building"] = False
            with self.lock:
                self._cache = None

    def _backfill_chips(self, dates):
        eng = self.eng
        todo = [d for d in dates if not (eng.chips.get(d) or {}).get("__twse__")]
        if not todo:
            return
        log(f"補抓上市三大法人資料：{len(todo)} 天（約 {len(todo) * 3 // 60 + 1} 分鐘）")
        fails = 0
        for k, d in enumerate(todo):
            self._set(f"補抓上市三大法人資料 {k + 1}/{len(todo)}（第一次需要幾分鐘）", k + 1, len(todo))
            try:
                m = src.fetch_t86(eng.http, d.replace("-", ""))
                if m:
                    with eng.lock:
                        eng.chips.setdefault(d, {}).update(m)
                        eng.chips[d]["__twse__"] = 1
                    write_json(eng._p("chips", f"{d}.json"), eng.chips[d])
                fails = 0
            except Exception as e:
                fails += 1
                eng.src_fail("證交所三大法人（T86）", e)
                if fails >= 3:
                    log("三大法人補抓連續失敗，先略過，下次啟動再補。")
                    break
        eng.src_ok("證交所三大法人（T86）", f"已補抓 {len(todo)} 天")

    # ------------------------------------------------------------------ 計算結果
    def api(self, force=False):
        with self.lock:
            c = self._cache
        if c and not force and time.time() - c[0] < 60:
            out = c[1]
        else:
            try:
                out = self._compute()
            except Exception as e:
                traceback.print_exc()
                out = {"error": str(e), "rows": []}
            with self.lock:
                self._cache = (time.time(), out)
        return {**out, "status": dict(self.status)}

    def _costs(self):
        disc = float(self.eng.cfg.get("fee_discount", 0.28))
        return FEE_RATE * disc, disc

    def _compute(self):
        eng = self.eng
        fee, disc = self._costs()
        chase = float(eng.cfg.get("chase_gap_pct", 3.0)) / 100
        with self.lock:
            recs = [self.lists[d] for d in sorted(self.lists)]
        live = eng.is_live()
        today = today_iso()
        with eng.lock:
            codes = {p["code"] for r in recs for p in r["picks"]}
            bars_of = {c: eng.merged_bars(c) for c in codes}
            tx = list(eng.index_hist.get("TAIEX") or [])
            ox = list(eng.index_hist.get("TPEX") or [])
        tpos = {b[0]: k for k, b in enumerate(tx)}
        opos = {b[0]: k for k, b in enumerate(ox)}
        pos_of = {c: {b[0]: k for k, b in enumerate(b_)} for c, b_ in bars_of.items() if b_}
        pool = self._pool([r["date"] for r in recs], fee, live, today)

        def net(entry, exit_, tax):
            return (exit_ * (1 - fee - tax) - entry * (1 + fee)) / (entry * (1 + fee))

        def idx_ret(series, spos, d, h):
            i = spos.get(d)
            if i is None or i + h >= len(series) or not series[i + 1][1]:
                return None
            return r2((series[i + h][4] / series[i + 1][1] - 1) * 100)

        def hold_path(bars, j0, j_end, last, entry, tax, inv_a=None):
            """持有期間每天的組合報酬（給累積曲線用）；inv_a 有值時盤中跌破就停損。回傳 (path, 停損 (j, 價))。"""
            pth, prev, stop = [], entry * (1 + fee), None
            for j in range(j0, min(j_end, last) + 1):
                fj = _f(bars[j])
                if inv_a and bars[j][3] * fj < inv_a:
                    px = inv_a if j == j0 and bars[j][1] * fj >= inv_a else min(bars[j][1] * fj, inv_a)
                    pth.append((bars[j][0], px * (1 - fee - tax) / prev - 1))
                    stop = (j, px)
                    break
                cj = bars[j][4] * fj
                v = cj * ((1 - fee - tax) if j == j_end else 1)
                pth.append((bars[j][0], v / prev - 1))
                prev = cj
            return pth, stop

        rows = []
        paths = {(s_, h): [] for s_ in ("A", "B", "C") for h in HOLDS}
        for rec in recs:
            d = rec["date"]
            pv = pool.get(d) or {}
            row = {"date": d, "reconstructed": bool(rec.get("reconstructed")), "picks": [], "h": {},
                   "mk": {h: idx_ret(tx, tpos, d, h) for h in HOLDS},
                   "otc": {h: idx_ret(ox, opos, d, h) for h in HOLDS},
                   "pool": {h: pv.get(h) for h in HOLDS},
                   "ppath": pv.get("path"),
                   **self._state(d, tx, tpos, ox, opos)}
            pick_paths = []
            for p in rec["picks"]:
                bars = bars_of.get(p["code"])
                pos = pos_of.get(p["code"], {})
                i0 = pos.get(d)
                etf = p.get("type") == "etf"
                q = {"rank": p["rank"], "code": p["code"], "name": p["name"], "score": p.get("score"),
                     "tags": p.get("tags", []), "keys": p.get("keys"), "status": "nodata"}
                row["picks"].append(q)
                if bars is None or i0 is None:
                    continue
                last = len(bars) - 1
                last_done = last - 1 if (live and bars[-1][0] == today) else last
                if i0 + 1 > last:
                    q["status"] = "wait"
                    continue
                f0 = _f(bars[i0])
                e1 = i0 + 1
                entry = bars[e1][1] * _f(bars[e1])
                close0 = bars[i0][4] * f0
                if not entry or not close0:
                    continue
                q["status"] = "ok"
                q["gap"] = r2((entry / close0 - 1) * 100)
                q["held"] = last - i0
                inv = p.get("invalid")
                inv_a = inv * f0 if inv else None
                skip_b = q["gap"] / 100 >= chase or bool(inv_a and entry < inv_a)
                q["skipB"] = skip_b
                c1 = bars[e1][4] * _f(bars[e1])
                skip_c = bool(inv_a and c1 < inv_a)  # 隔天收盤已跌破失效價就不買
                q["skipC"] = skip_c
                for h in HOLDS:
                    tax = TAX_ETF if etf else (TAX_DAYTRADE if h == 1 else TAX_STOCK)
                    tax_c = TAX_ETF if etf else TAX_STOCK
                    j_end = i0 + h
                    done = j_end <= last_done
                    q[f"r{h}"] = r2(net(entry, bars[j_end][4] * _f(bars[j_end]), tax) * 100) if done else None
                    pa, _ = hold_path(bars, e1, j_end, last, entry, tax)
                    paths[("A", h)].append(pa)
                    if skip_b:
                        q[f"b{h}"] = None
                    else:
                        pb, stop = hold_path(bars, e1, j_end, last, entry, tax, inv_a)
                        paths[("B", h)].append(pb)
                        if stop:
                            q[f"b{h}"] = r2(net(entry, stop[1], tax) * 100)
                            q[f"stop{h}"] = bars[stop[0]][0]
                        else:
                            q[f"b{h}"] = q[f"r{h}"]
                    # 策略 C：隔天收盤前才買，之後同樣持有 h 天（當沖不適用）
                    if h > 1 and not skip_c and c1:
                        jc = e1 + h
                        q[f"c{h}"] = r2(net(c1, bars[jc][4] * _f(bars[jc]), tax_c) * 100) if jc <= last_done else None
                        if e1 + 1 <= last:
                            pc, _ = hold_path(bars, e1 + 1, jc, last, c1, tax_c)
                            paths[("C", h)].append(pc)
                    else:
                        q[f"c{h}"] = None
                # 未扣成本的累積走勢（目前報酬）
                q["cur"] = r2(net(entry, bars[last][4] * _f(bars[last]), TAX_ETF if etf else TAX_STOCK) * 100)
                if i0 + PATH_DAYS <= last_done:
                    cs = [bars[j][4] * _f(bars[j]) for j in range(e1, i0 + PATH_DAYS + 1)]
                    dd = [entry / close0 - 1, cs[0] / entry - 1] + [cs[k] / cs[k - 1] - 1 for k in range(1, len(cs))]
                    cc = [x / entry - 1 for x in cs]
                    pick_paths.append((dd, cc))
            if pick_paths:
                n = len(pick_paths)
                row["path"] = {"d": [r2(sum(pp[0][k] for pp in pick_paths) / n * 100, 3) for k in range(PATH_DAYS + 1)],
                               "c": [r2(sum(pp[1][k] for pp in pick_paths) / n * 100, 3) for k in range(PATH_DAYS)],
                               "n": n}
            for h in HOLDS:
                done = [x[f"r{h}"] for x in row["picks"] if x.get(f"r{h}") is not None]
                cur = [x["cur"] for x in row["picks"] if x.get("cur") is not None]
                held = max([x.get("held", 0) for x in row["picks"]] or [0])
                if done and len(done) == len([x for x in row["picks"] if x["status"] == "ok"]):
                    row["h"][h] = {"state": "done", "avg": r2(sum(done) / len(done)),
                                   "up": sum(1 for v in done if v > 0), "n": len(done)}
                elif cur:
                    row["h"][h] = {"state": "track", "avg": r2(sum(cur) / len(cur)),
                                   "up": sum(1 for v in cur if v > 0), "n": len(cur), "day": min(held, h)}
                else:
                    row["h"][h] = {"state": "wait"}
            rows.append(row)

        summary, curves, strategies = {}, {}, {}
        for h in HOLDS:
            allp = [x for r in rows for x in r["picks"] if x.get(f"r{h}") is not None]
            done_rows = [r for r in rows if r["h"][h].get("state") == "done"]

            def avg_of(key):
                vals = [r[key][h] for r in done_rows if r[key].get(h) is not None]
                return r2(sum(vals) / len(vals)) if vals else None

            def stat(vals):
                return {"n": len(vals), "avg": r2(sum(vals) / len(vals)) if vals else None,
                        "win": r2(sum(1 for v in vals if v > 0) / len(vals) * 100, 1) if vals else None}
            summary[h] = {**stat([x[f"r{h}"] for x in allp]), "days": len(done_rows),
                          "mkt": avg_of("mk"), "otc": avg_of("otc"), "pool": avg_of("pool")}
            bp = [x for x in allp if not x.get("skipB")]
            cp = [x for r in rows for x in r["picks"] if x.get(f"c{h}") is not None]
            strategies[h] = {
                "A": stat([x[f"r{h}"] for x in allp]),
                "B": {**stat([x[f"b{h}"] for x in bp]), "skipped": len(allp) - len(bp),
                      "stops": sum(1 for x in bp if x.get(f"stop{h}"))},
                "C": ({**stat([x[f"c{h}"] for x in cp]),
                       "skipped": sum(1 for x in allp if x.get("skipC"))} if h > 1 else None),
            }
            curves[h] = self._curves({k: paths[(k, h)] for k in ("A", "B", "C")}, tx, ox)
            for k in ("A", "B", "C"):
                if strategies[h][k] is not None:
                    strategies[h][k]["total"] = curves[h][k][-1] if curves[h][k] else None
        rows.reverse()
        return {"rows": rows, "summary": summary, "curves": curves, "strategies": strategies,
                "holds": list(HOLDS), "path_days": PATH_DAYS,
                "settings": {"fee_discount": disc, "fee_rate": FEE_RATE, "tax": TAX_STOCK, "tax_dt": TAX_DAYTRADE,
                             "chase_gap": eng.cfg.get("chase_gap_pct", 3.0),
                             "min_value": eng.cfg.get("top_min_avg_value"), "min_price": eng.cfg.get("top_min_price")},
                "range": [rows[-1]["date"], rows[0]["date"]] if rows else None,
                "generated": now_tw().strftime("%Y-%m-%d %H:%M")}

    def _state(self, d, tx, tpos, ox, opos):
        """名單當天的大盤格局，以及中小型股（櫃買）相對加權指數近 20 日的強弱。"""
        i = tpos.get(d)
        regime = self.eng._regime_from([b[4] for b in tx[:i + 1]]) if i is not None else None
        j = opos.get(d)
        rel = None
        if i is not None and j is not None and i >= 20 and j >= 20 and tx[i - 20][4] and ox[j - 20][4]:
            rel = (ox[j][4] / ox[j - 20][4] - 1) - (tx[i][4] / tx[i - 20][4] - 1)
        return {"regime": regime, "style": None if rel is None else ("small" if rel > 0 else "large"),
                "rel": r2(rel * 100) if rel is not None else None}

    def _pool(self, dates, fee, live, today):
        """同條件個股基準：當天成交值與股價都符合前十名門檻的所有股票，隔天開盤等權買進的平均報酬（扣成本），
        以及每天的報酬拆解（未扣成本）。"""
        eng = self.eng
        key = (tuple(dates[-1:]), len(dates), len(eng.hist), round(fee, 7), live,
               max((b[-1][0] for b in eng.hist.values() if b), default=""))
        c = getattr(self, "_pool_cache", None)
        if c and c[0] == key and time.time() - c[1] < 600:
            return c[2]
        thr_v, thr_p = eng.cfg["top_min_avg_value"], eng.cfg["top_min_price"]
        with eng.lock:
            items = [(code, eng.merged_bars(code)) for code in eng.hist
                     if (eng.universe.get(code) or {}).get("type") == "stock"]
        dset = set(dates)
        acc = {d: {h: [0.0, 0] for h in HOLDS} for d in dates}
        pacc = {d: [[0.0] * (PATH_DAYS + 1), [0.0] * PATH_DAYS, 0] for d in dates}
        for code, bars in items:
            n = len(bars or [])
            if n < 25:
                continue
            ps = [0.0]
            for b in bars:
                ps.append(ps[-1] + b[4] * b[5])
            last_done = n - 1 - (1 if live and bars[-1][0] == today else 0)
            for i0 in range(20, last_done):
                b = bars[i0]
                if b[0] not in dset or b[4] < thr_p or (ps[i0 + 1] - ps[i0 - 19]) / 20 < thr_v:
                    continue
                entry = bars[i0 + 1][1] * _f(bars[i0 + 1])
                if not entry:
                    continue
                for h in HOLDS:
                    j = i0 + h
                    if j > last_done:
                        continue
                    tax = TAX_DAYTRADE if h == 1 else TAX_STOCK
                    ex = bars[j][4] * _f(bars[j])
                    r = (ex * (1 - fee - tax) - entry * (1 + fee)) / (entry * (1 + fee))
                    a = acc[b[0]][h]
                    a[0] += r
                    a[1] += 1
                if i0 + PATH_DAYS <= last_done:
                    close0 = b[4] * _f(b)
                    cs = [bars[j][4] * _f(bars[j]) for j in range(i0 + 1, i0 + PATH_DAYS + 1)]
                    pa = pacc[b[0]]
                    pa[0][0] += entry / close0 - 1
                    pa[0][1] += cs[0] / entry - 1
                    for k in range(1, PATH_DAYS):
                        pa[0][k + 1] += cs[k] / cs[k - 1] - 1
                    for k in range(PATH_DAYS):
                        pa[1][k] += cs[k] / entry - 1
                    pa[2] += 1
        out = {}
        for d, v in acc.items():
            out[d] = {h: (r2(a[0] / a[1] * 100) if a[1] else None) for h, a in v.items()}
            pa = pacc[d]
            if pa[2]:
                out[d]["path"] = {"d": [r2(x / pa[2] * 100, 3) for x in pa[0]],
                                  "c": [r2(x / pa[2] * 100, 3) for x in pa[1]], "n": pa[2]}
        self._pool_cache = (key, time.time(), out)
        return out

    @staticmethod
    def _curves(paths, tx, ox=None):
        """把重疊持有的部位合成每日等權組合，算累積報酬（%），並和加權、櫃買指數比較。"""
        def daily(pl):
            acc = {}
            for pth in pl:
                for d, r in pth:
                    acc.setdefault(d, []).append(r)
            return {d: sum(v) / len(v) for d, v in acc.items()}
        dl = {k: daily(v) for k, v in paths.items()}
        days = sorted(set().union(*[set(v) for v in dl.values()])) if dl else []
        out = {"dates": [], "mkt": [], "otc": [], **{k: [] for k in paths}}
        if not days:
            return out

        def base_of(series):
            close = {b[0]: b[4] for b in series}
            ds = [b[0] for b in series]
            k = bisect.bisect_left(ds, days[0])
            return close, (close.get(ds[k - 1]) if k > 0 else None)
        tclose, tbase = base_of(tx)
        oclose, obase = base_of(ox or [])
        eq = {k: 1.0 for k in paths}
        for d in days:
            out["dates"].append(d)
            for k in paths:
                eq[k] *= 1 + dl[k].get(d, 0.0)
                out[k].append(r2((eq[k] - 1) * 100))
            out["mkt"].append(r2((tclose[d] / tbase - 1) * 100) if tbase and d in tclose else None)
            out["otc"].append(r2((oclose[d] / obase - 1) * 100) if obase and d in oclose else None)
        return out


def reconstruct(eng, dates, progress=None):
    """用歷史日 K 重建指定日期的收盤前十名（技術面＋已有的法人資料；不含新聞與盤中內外盤）。
    每檔股票的指標只算一次，再逐日評估，速度比逐日重算快數十倍。"""
    dates = sorted(set(dates))
    cfg = eng.cfg
    with eng.lock:
        uni = dict(eng.universe)
        allbars = {c: eng.merged_bars(c) for c in eng.hist if c in uni}
        chips = dict(eng.chips)
        th = list(eng.index_hist.get("TAIEX") or [])
    chip_days = sorted(chips)
    regimes = {d: eng._regime_from([b[4] for b in th if b[0] <= d]) for d in dates}
    dpos, rets, ret_of = {}, {d: [] for d in dates}, {}
    for code, bars in allbars.items():
        if not bars or len(bars) < an.MIN_BARS:
            continue
        pos = {b[0]: k for k, b in enumerate(bars)}
        dpos[code] = pos
        for d in dates:
            i = pos.get(d)
            if i is not None and i >= 21 and bars[i - 20][4]:
                r = bars[i][4] / bars[i - 20][4] - 1
                rets[d].append(r)
                ret_of[(code, d)] = r
    for d in dates:
        rets[d].sort()
    cands = {d: [] for d in dates}
    codes = list(dpos)
    for k, code in enumerate(codes):
        if progress and k % 40 == 0:
            progress(k, len(codes))
        u = uni.get(code) or {}
        if u.get("type") == "etf" and not cfg["include_etf_in_top"]:
            continue
        need = [(d, dpos[code][d]) for d in dates if dpos[code].get(d, -1) >= an.MIN_BARS - 1]
        if not need:
            continue
        try:
            F = an.compute_features(allbars[code])
        except Exception:
            continue
        for d, i in need:
            if F["c"][i] < cfg["top_min_price"]:
                continue
            avgv = sum(F["c"][j] * F["v"][j] for j in range(i - 19, i + 1)) / 20
            if avgv < cfg["top_min_avg_value"]:
                continue
            rr, arr = ret_of.get((code, d)), rets[d]
            rs = bisect.bisect_left(arr, rr) / len(arr) if rr is not None and len(arr) > 50 else None
            ci = bisect.bisect_right(chip_days, d)
            ch = [{"date": x, **chips[x][code]} for x in chip_days[max(0, ci - 5):ci]
                  if isinstance(chips[x].get(code), dict)]
            ctx = {"rs_pct": rs, "market": regimes[d], "chips": ch or None, "flow": None, "news": None}
            r = an.evaluate(F, i, ctx)
            if not r or r["flags"]["dist"]:
                continue
            lst = cands[d]
            if len(lst) >= 10 and (r["score"], r["raw"], avgv) <= min((x[0], x[1], x[2]) for x in lst):
                continue
            info = {"code": code, "name": u.get("name", code), "type": u.get("type"), "score": r["score"],
                    "close": F["c"][i], "invalid": an.key_levels(F, i).get("invalid"),
                    "tags": [sg["label"] for sg in sorted(r["signals"], key=lambda x: -x["pts"]) if sg["pts"] > 0][:3],
                    "keys": [sg["key"] for sg in r["signals"] if sg["pts"] > 0]}
            lst.append((r["score"], r["raw"], avgv, info))
            if len(lst) > 10:
                lst.sort(key=lambda x: (x[0], x[1], x[2]), reverse=True)
                del lst[10:]
    out = {}
    for d in dates:
        top = sorted(cands[d], key=lambda x: (x[0], x[1], x[2]), reverse=True)[:10]
        picks = [{"rank": rank, **info} for rank, (_, _, _, info) in enumerate(top, 1)]
        out[d] = {"date": d, "reconstructed": True, "v": VERSION, "saved": now_tw().strftime("%Y-%m-%d %H:%M"), "picks": picks}
    if progress:
        progress(len(codes), len(codes))
    return out
