"""臺股強勢掃描 —— 雲端版執行程式（在 GitHub Actions 上跑）。

和電腦版是同一套掃描程式（twscan）。這支程式不開網頁伺服器，而是每分鐘把網頁需要的資料
輸出成 JSON 檔，推到存放庫的 stock-data 分支；網頁（docs/stock/）直接讀這些檔案，
所以不用開電腦，手機隨時能看。

    python cloud.py --publish-dir pub
        依台北時間決定這一輪做什麼：平日 14:10 以前＝盤中模式，一直跑到 14:10
        （官方收盤資料約 13:50 公布，套用完才結束）；其他時間＝掃描一次、輸出、結束。
    python cloud.py --demo-live --minutes 2 --publish-dir /tmp/pub --no-push
        本機測試（模擬資料、不推送）。
"""
import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from twscan.engine import Engine, load_config  # noqa: E402
from twscan.util import log, now_tw  # noqa: E402

SESSION_END = (14, 10)    # 盤中模式結束時間（台北）
MAX_MINUTES = 340         # GitHub 一個工作最多 6 小時：留時間給存快取
ONCE_MINUTES = 55         # 非盤中：最多等這麼久讓一輪完整掃描跑完
# 小而重要、不能重算的資料：除了 Actions 快取，另外在 stock-data 分支存一份，快取被清掉也不會遺失
STATE_DIRS = ("close_lists", "daytrade", "review")
BRANCH = "stock-data"
# 推送頻率（秒）
EVERY = {"live": 60, "market": 180, "key_stocks": 300, "tf": 600, "slow": 900, "all_stocks": 3600}
MAX_KEY = 40              # 每 5 分鐘更新的重點股票數
MAX_TF = 24               # 每 10 分鐘更新 30 分／60 分 K 的股票數
RELAY_EVERY = 10          # 盤中每 10 秒把最新報價送到轉播站（收盤後每分鐘）
RELAY_CODES = 300         # 每次送出的股票數上限（名單、候選、網頁要的、成交值最大的）


def clean(obj):
    """推播主題等同密碼：輸出到公開分支前一律拿掉，只留下「有設定」。"""
    if isinstance(obj, dict):
        return {k: (("（雲端）" if v else "") if k == "ntfy_topic" else clean(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    return obj


class Publisher:
    def __init__(self, eng, out, push, label):
        self.eng, self.out, self.push, self.label = eng, out, push, label
        self.last = {}
        self.stocks = set()       # 已輸出個股資料的代號
        self.tf = set()           # 已輸出 30 分／60 分 K 的代號
        self.pushed = 0
        self.head = None          # stock-data 分支最新一版的代號（轉播站一起送給網頁，網頁就不用問 GitHub）
        self.lock = threading.Lock()
        os.makedirs(out, exist_ok=True)
        old = self._read("meta.json") or {}
        # 分支上原本就有的個股檔繼續有效（下一輪會逐步更新）
        self.stocks = set(old.get("stocks") or []) & set(self._ls("stock"))
        self.tf = set(old.get("tf") or [])

    # ---------------------------------------------------------------- 檔案
    def _ls(self, sub):
        d = os.path.join(self.out, sub)
        return [f[:-5] for f in os.listdir(d) if f.endswith(".json")] if os.path.isdir(d) else []

    def _read(self, rel):
        try:
            with open(os.path.join(self.out, rel), encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def write(self, rel, data):
        path = os.path.join(self.out, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(clean(data), f, ensure_ascii=False, separators=(",", ":"), default=str)
        os.replace(tmp, path)

    def due(self, name, force=False):
        if force or time.time() - self.last.get(name, 0) >= EVERY[name]:
            self.last[name] = time.time()
            return True
        return False

    # ---------------------------------------------------------------- 要輸出的股票
    def key_codes(self):
        eng = self.eng
        codes = []
        try:
            for c in eng.top_payload_list():
                codes.append(c["code"])
            codes += [c["code"] for c in (eng.daytrade.api().get("cands") or [])]
            with eng.lock:
                codes += list(eng.rt_priority[:MAX_KEY])
        except Exception:
            traceback.print_exc()
        with eng.lock:
            have = set(eng.analysis)
        return [c for c in dict.fromkeys(codes) if c in have][:MAX_KEY]

    def write_stock(self, code, fetch=False):
        try:
            d = self.eng.api_stock(code, fetch=fetch)
        except Exception as e:
            log(f"[雲端] 個股 {code} 輸出失敗：{type(e).__name__}")
            return
        if isinstance(d, dict) and not d.get("error"):
            self.write(f"stock/{code}.json", d)
            self.stocks.add(code)

    def write_tf(self, code):
        ok = False
        for tf in ("30m", "60m"):
            try:
                d = self.eng.api_stock_tf(code, tf)
            except Exception as e:
                log(f"[雲端] {code} {tf} 輸出失敗：{type(e).__name__}")
                continue
            if isinstance(d, dict) and not d.get("error"):
                self.write(f"tf/{code}-{tf}.json", d)
                ok = True
        if ok:
            self.tf.add(code)

    def quotes(self):
        """全部股票的精簡報價（網頁上「我的持倉」用）：代號 -> [價, 昨收, 推估?, 時間, 開, 低, 高, 漲停, ETF?, 名稱]。"""
        eng = self.eng
        out = {}
        with eng.lock:
            for code, u in eng.universe.items():
                s = eng.snap.get(code)
                if not s or not s.get("price"):
                    continue
                out[code] = [s.get("price"), s.get("prev"), 1 if s.get("est") else 0, s.get("time"), s.get("open"),
                             s.get("low"), s.get("high"), s.get("limit_up"), 1 if u.get("type") == "etf" else 0, u.get("name")]
            dates = [s.get("date") for s in eng.snap.values() if s.get("date")]
        n = now_tw()
        return {"date": max(dates) if dates else None, "now": n.strftime("%Y-%m-%d %H:%M:%S"), "q": out}

    # ---------------------------------------------------------------- 一輪輸出
    def tick(self, force=False, final=False):
        eng = self.eng
        with self.lock:
            t0 = time.time()
            wrote = []
            try:
                if self.due("live", force):
                    self.write("status.json", eng.api_status())
                    self.write("top.json", eng.api_top())
                    self.write("daytrade.json", eng.daytrade.api())
                    self.write("quotes.json", self.quotes())
                    wrote.append("live")
                ready = bool(eng.status.get("first_scan_done")) or bool(eng.analysis)
                if ready and self.due("market", force):
                    self.write("market.json", eng.api_market())
                    wrote.append("market")
                if ready and self.due("slow", force):
                    self.write("review.json", eng.review.api())
                    self.write("daytrade_review.json", eng.daytrade.review())
                    self.write("diag.json", eng.api_diag())
                    wrote.append("slow")
                if ready and self.due("all_stocks", force and final):
                    with eng.lock:
                        codes = sorted(eng.analysis)
                    for code in codes:
                        self.write_stock(code)
                    gone = self.stocks - set(codes)
                    for code in gone:
                        try:
                            os.remove(os.path.join(self.out, "stock", f"{code}.json"))
                        except OSError:
                            pass
                    self.stocks -= gone
                    self.last["key_stocks"] = time.time()
                    wrote.append(f"all_stocks({len(codes)})")
                elif ready and self.due("key_stocks", force):
                    keys = self.key_codes()
                    for code in keys:
                        self.write_stock(code)
                    wrote.append(f"key_stocks({len(keys)})")
                if ready and self.due("tf", force) and not eng.demo:
                    keys = self.key_codes()[:MAX_TF]
                    for code in keys:
                        self.write_tf(code)
                    wrote.append(f"tf({len(keys)})")
            except Exception:
                traceback.print_exc()
            if not wrote:
                return
            n = now_tw()
            meta = self._read("meta.json") or {}
            upd = dict(meta.get("updated") or {})
            for w in wrote:
                upd[w.split("(")[0]] = n.strftime("%Y-%m-%dT%H:%M:%S+08:00")
            self.write("meta.json", {
                "v": 1, "published": n.strftime("%Y-%m-%dT%H:%M:%S+08:00"), "ts": int(time.time()),
                "phase": eng.phase(), "run": self.label, "final": final,
                "every": EVERY["live"] if eng.phase() in ("open", "preopen") else None,
                "stocks": sorted(self.stocks), "tf": sorted(self.tf), "updated": upd,
                "demo": eng.demo,
            })
            self.copy_state()
            self.commit_push(f"data: {n:%Y-%m-%d %H:%M} {' '.join(wrote)}")
            log(f"[雲端] 輸出 {' '.join(wrote)}（{time.time() - t0:.1f} 秒）")

    # ---------------------------------------------------------------- 小檔案備份（快取被清掉時用）
    def copy_state(self):
        for sub in STATE_DIRS:
            src_dir = self.eng._p(sub)
            if os.path.isdir(src_dir):
                shutil.copytree(src_dir, os.path.join(self.out, "state", sub), dirs_exist_ok=True)

    # ---------------------------------------------------------------- git
    def git(self, *args):
        return subprocess.run(["git", "-C", self.out, *args], capture_output=True, text=True, timeout=180)

    def commit_push(self, msg):
        if not self.push:
            return
        self.git("add", "-A")
        if self.git("diff", "--cached", "--quiet").returncode == 0:
            return
        has_head = self.git("rev-parse", "-q", "--verify", "HEAD").returncode == 0
        # 分支只留一個提交（每次蓋掉），資料量不會越積越多
        r = self.git("commit", "-q", *(["--amend"] if has_head else []), "-m", msg)
        if r.returncode:
            log(f"[雲端] 提交失敗：{(r.stderr or r.stdout)[-300:]}")
            return
        r = self.git("push", "-q", "-f", "origin", f"HEAD:refs/heads/{BRANCH}")
        if r.returncode:
            log(f"[雲端] 推送失敗：{r.stderr[-300:]}")
        else:
            self.pushed += 1
            self.head = self.git("rev-parse", "HEAD").stdout.strip() or None


class Relay(threading.Thread):
    """10 秒報價：GitHub 一分鐘只能更新一次，所以另外把最新報價送到使用者自己的
    Google Apps Script 轉播站（網址放在 GitHub secret STOCK_RELAY_URL），網頁每 10 秒去讀。
    轉播站也帶回網頁想優先更新的股票（持倉、正在看的股票）。網址等同密碼：不寫進任何檔案或紀錄。"""

    def __init__(self, eng, pub, url):
        super().__init__(name="relay", daemon=True)
        self.eng, self.pub, self.url = eng, pub, url
        self.halt = threading.Event()
        self.sent = self.fails = 0
        self.last = 0.0

    def codes(self):
        eng = self.eng
        out = list(eng.extra_watch)
        try:
            out += [c["code"] for c in eng.top_payload_list()]
        except Exception:
            pass
        with eng.lock:
            out += list(eng.rt_priority)
            act = sorted(((s.get("price") or 0) * (s.get("vol") or 0), c) for c, s in eng.snap.items() if s.get("price"))
        out += [c for _, c in reversed(act[-RELAY_CODES:])]
        return list(dict.fromkeys(out))[:RELAY_CODES]

    def payload(self):
        eng = self.eng
        q, dates = {}, set()
        codes = self.codes()
        with eng.lock:
            for c in codes:
                s = eng.snap.get(c)
                if s and s.get("price"):
                    q[c] = [s.get("price"), s.get("prev"), 1 if s.get("est") else 0, s.get("time"), s.get("high"), s.get("low")]
                    if s.get("date"):
                        dates.add(s["date"])
        n = now_tw()
        return {"v": 1, "ts": int(time.time()), "now": n.strftime("%Y-%m-%d %H:%M:%S"), "date": max(dates) if dates else None,
                "phase": eng.phase(), "sha": self.pub.head, "q": q}

    def send(self):
        body = json.dumps(self.payload(), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, headers={"Content-Type": "text/plain;charset=utf-8"})
        with urllib.request.urlopen(req, timeout=20) as r:   # Apps Script 回 302，urllib 會自動跟過去拿回覆
            reply = json.loads(r.read().decode("utf-8") or "{}")
        eng = self.eng
        watch = [str(c).strip().upper() for c in (reply.get("watch") or [])][:10]
        watch = [c for c in watch if c in eng.universe]
        if watch != eng.extra_watch:
            with eng.lock:
                eng.extra_watch = watch
                eng.rt_priority = list(dict.fromkeys(watch + list(eng.rt_priority)))[:eng.cfg["rt_priority_count"]]
            if watch:
                log(f"[轉播] 網頁要求優先更新：{'、'.join(watch)}")

    def run(self):
        while not self.halt.wait(1):
            live = self.eng.phase() in ("open", "preopen")
            if time.time() - self.last < (RELAY_EVERY if live else 60):
                continue
            self.last = time.time()
            try:
                self.send()
                self.sent += 1
                if self.fails:
                    log(f"[轉播] 恢復正常（之前失敗 {self.fails} 次）")
                self.fails = 0
            except Exception as e:
                self.fails += 1
                if self.fails in (1, 10) or self.fails % 60 == 0:
                    log(f"[轉播] 送出失敗（{type(e).__name__}），連續 {self.fails} 次")


def restore_state(eng, out):
    """Actions 快取不見時（例如連續放假超過 7 天），從 stock-data 分支把小檔案拿回來。"""
    n = 0
    for sub in STATE_DIRS:
        src_dir = os.path.join(out, "state", sub)
        if not os.path.isdir(src_dir):
            continue
        for root, _dirs, files in os.walk(src_dir):
            rel = os.path.relpath(root, src_dir)
            dst_dir = os.path.join(eng._p(sub), rel)
            os.makedirs(dst_dir, exist_ok=True)
            for f in files:
                dst = os.path.join(dst_dir, f)
                if not os.path.exists(dst):
                    shutil.copy2(os.path.join(root, f), dst)
                    n += 1
    if n:
        log(f"[雲端] 從 stock-data 分支補回 {n} 個存檔")


def plan(n, minutes=None):
    """這一輪的模式與結束時間。"""
    if minutes:
        return "session", n + timedelta(minutes=minutes)
    end = n.replace(hour=SESSION_END[0], minute=SESSION_END[1], second=0, microsecond=0)
    if n.weekday() < 5 and n < end:
        return "session", min(end, n + timedelta(minutes=MAX_MINUTES))
    return "once", n + timedelta(minutes=ONCE_MINUTES)


def main():
    ap = argparse.ArgumentParser(description="臺股強勢掃描（雲端版）")
    ap.add_argument("--publish-dir", required=True, help="stock-data 分支的工作目錄")
    ap.add_argument("--no-push", action="store_true", help="只寫檔，不推送")
    ap.add_argument("--minutes", type=float, help="固定跑這麼多分鐘（測試用）")
    ap.add_argument("--demo", action="store_true")
    ap.add_argument("--demo-live", action="store_true")
    args = ap.parse_args()

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    cfg = load_config()
    eng = Engine(cfg, demo=args.demo or args.demo_live)
    eng.demo_live = args.demo_live
    if not eng.demo:
        os.makedirs(eng.data_dir, exist_ok=True)
        restore_state(eng, args.publish_dir)
    eng.start()
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if topic:   # 手機推播主題來自 GitHub 的 secret，不寫進任何檔案或紀錄
        eng.daytrade.settings["ntfy_topic"] = topic
        eng.daytrade.settings["notify"] = True

    label = os.environ.get("GITHUB_RUN_ID") or "local"
    pub = Publisher(eng, args.publish_dir, push=not args.no_push, label=label)
    relay_url = os.environ.get("STOCK_RELAY_URL", "").strip()
    relay = None
    if relay_url:
        relay = Relay(eng, pub, relay_url)
        relay.start()
        log("[轉播] 已啟用：盤中每 10 秒送出報價")
    mode, deadline = plan(now_tw(), args.minutes)
    log(f"[雲端] 模式：{'盤中' if mode == 'session' else '掃描一次'}，最晚 {deadline:%H:%M} 結束")
    scans0 = eng.status.get("last_scan")
    try:
        while not stop.is_set() and now_tw() < deadline:
            pub.tick()
            n = now_tw()
            if mode == "session" and n.hour * 60 + n.minute >= 9 * 60 + 20 and eng.is_holiday():
                log("[雲端] 今天休市（09:20 指數仍是前一個交易日）：輸出後結束")
                mode = "once"      # 不交棒
                break
            if mode == "once":
                st = eng.status
                if st.get("first_scan_done") and st.get("last_scan") != scans0 and not st.get("scanning") \
                        and eng.last_sweep is not None:
                    break
            stop.wait(5)
    finally:
        log("[雲端] 結束前輸出全部資料…")
        if relay:
            relay.halt.set()
            log(f"[轉播] 本輪送出 {relay.sent} 次")
        eng.stop.set()
        pub.tick(force=True, final=True)
        log(f"[雲端] 本輪共推送 {pub.pushed} 次")
    # 一輪最多跑 MAX_MINUTES；盤中還沒結束就請 GitHub 接著開下一輪（stock.yml 的「交棒」步驟）。
    # GitHub 的排程常常晚一兩個小時才啟動，不能只靠排程把盤中接起來。
    end = now_tw().replace(hour=SESSION_END[0], minute=SESSION_END[1], second=0, microsecond=0)
    chain = mode == "session" and not args.minutes and not stop.is_set() and now_tw() < end - timedelta(minutes=3)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"chain={'true' if chain else 'false'}\n")
    if chain:
        log("[雲端] 盤中還沒結束：交棒給下一輪")


if __name__ == "__main__":
    main()
