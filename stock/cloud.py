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
    mode, deadline = plan(now_tw(), args.minutes)
    log(f"[雲端] 模式：{'盤中' if mode == 'session' else '掃描一次'}，最晚 {deadline:%H:%M} 結束")
    scans0 = eng.status.get("last_scan")
    try:
        while not stop.is_set() and now_tw() < deadline:
            pub.tick()
            if mode == "once":
                st = eng.status
                if st.get("first_scan_done") and st.get("last_scan") != scans0 and not st.get("scanning") \
                        and eng.last_sweep is not None:
                    break
            stop.wait(5)
    finally:
        log("[雲端] 結束前輸出全部資料…")
        eng.stop.set()
        pub.tick(force=True, final=True)
        log(f"[雲端] 本輪共推送 {pub.pushed} 次")


if __name__ == "__main__":
    main()
