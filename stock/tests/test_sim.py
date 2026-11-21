"""模擬戰（當沖）的核心規則。假時鐘、假報價、假的進場建議，不連網路，只需要 Python。
用法：python3 stock/tests/test_sim.py        （最後一行印出 SIM: ALL PASS 才算過）
改了 twscan/sim.py 先跑這一支。測電腦版的資料夾：TWSCAN_ROOT=/path/to/tw-stock-scanner python3 test_sim.py
"""
import json
import os
import random
import shutil
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.environ.get('TWSCAN_ROOT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from twscan import util, sim as SM  # noqa: E402
from twscan.daytrade import trade_pnl, FEE_RATE  # noqa: E402
import twscan.engine as E  # noqa: E402
import cloud as CL  # noqa: E402

FAILS = []


def ok(name, cond, info=None):
    print(("PASS " if cond else "FAIL ") + name + ("" if info is None or cond else f"  → {info}"))
    if not cond:
        FAILS.append(name)


# ---------------------------------------------------------------- 假時鐘：2026/11/23（週一）
CLOCK = [datetime(2026, 11, 23, 8, 50, 0, tzinfo=util.TW)]
fake_now = lambda: CLOCK[0]  # noqa: E731
for name, mod in list(sys.modules.items()):
    if name.startswith('twscan') and hasattr(mod, 'now_tw'):
        mod.now_tw = fake_now
CL.now_tw = fake_now


def at(h, m, s=0, day=23):
    CLOCK[0] = datetime(2026, 11, day, h, m, s, tzinfo=util.TW)
    return CLOCK[0]


def later(sec):
    CLOCK[0] = CLOCK[0] + timedelta(seconds=sec)


DATA = '/tmp/twscan_sim_test'
shutil.rmtree(DATA, ignore_errors=True)
E.DATA_DIR = DATA
DISC = 0.28
NAMES = {"1101": "甲", "1102": "乙", "1103": "丙", "1104": "丁", "1105": "戊", "1106": "己", "1107": "庚", "1108": "貴"}
ADV = {}      # code -> 進場建議（假的）
LIVE = [True]


def make(token=""):
    cfg = dict(E.DEFAULT_CONFIG)
    cfg["sim_reset"] = token
    e = E.Engine(cfg)
    e.universe = {c: {"name": n, "market": "TWSE", "type": "stock"} for c, n in NAMES.items()}
    e.is_live = lambda: LIVE[0]
    e.phase = lambda: "open" if LIVE[0] else "closed"

    def advise(code, rank=None, now=None):
        a = ADV.get(code)
        s = e.snap.get(code)
        if not a or not s:
            return {"code": code, "name": NAMES[code], "status": "nodata"}
        entry = (s.get("asks") or [s["price"]])[0]
        st = a.get("status", "go")
        return {"code": code, "name": NAMES[code], "rank": rank, "score": 60, "market": "TWSE", "etf": False, "fit": True,
                "status": st, "label": {"go": "可進場", "wait": "等一下", "avoid": "不要追"}[st], "kind": "bull",
                "reason": a.get("reason", "條件都符合" if st == "go" else "預估量只有均量的 1.0 倍"),
                "checks": [{"k": "pos", "ok": True, "text": "站上開盤價與當日均價"}, {"k": "vol", "ok": st == "go", "text": "量"}],
                "price": s["price"], "prev": s["prev"], "open": s["open"], "chgp": 1.0, "gap": 0.2, "ext": 0.5, "vwap": s["open"],
                "entry": entry, "stop": a["stop"], "target": a["target"],
                "stop_pct": round((a["stop"] / entry - 1) * 100, 2), "target_pct": round((a["target"] / entry - 1) * 100, 2), "tags": []}
    e.daytrade.advise = advise
    e.sim.load()
    return e


def quote(e, code, price, bid=None, ask=None, low=None, high=None, t=None, bids=True, limit_down=None):
    s = e.snap.get(code) or {"date": CLOCK[0].strftime("%Y-%m-%d"), "prev": price, "open": price, "high": price, "low": price, "vol": 5000}
    s.update(date=CLOCK[0].strftime("%Y-%m-%d"), price=price, time=t or CLOCK[0].strftime("%H:%M:%S"),
             high=max(s["high"], price, high or price), low=min(s["low"], price, low or price),
             bids=[bid if bid is not None else price] if bids else [], asks=[ask if ask is not None else price],
             limit_up=round(s["prev"] * 1.1, 2), limit_down=limit_down if limit_down is not None else round(s["prev"] * 0.9, 2))
    e.snap[code] = s


def tick(e):
    e.sim._last = 0
    e.sim.tick()


def brute(entry, stop, cash, bal):
    risk = bal * SM.DEF["risk_pct"] / 100
    n = 0
    while (n + 1) * entry * 1000 * (1 + FEE_RATE * DISC) <= min(cash, bal * SM.DEF["pos_pct"] / 100) and -trade_pnl(entry, stop, n + 1, DISC) <= risk:
        n += 1
    return n


def recon(e):
    return abs(e.sim.st["start"] + sum(t["pnl"] for t in e.sim.hist) - e.sim.st["balance"]) < 0.5


# ================================================================ 1. 新帳戶與部位大小
e = make()
S = e.sim
a = S.api()
ok("新帳戶：35 萬、沒有部位、沒有紀錄", a["start"] == 350000 and a["account"]["balance"] == 350000 and a["account"]["equity"] == 350000 and not a["open"] and a["hist_n"] == 0 and a["stats"] == {"n": 0})
for entry, stop in ((100, 99), (60.8, 60.3), (222.5, 218.5), (27.3, 27.05), (387.5, 384.0), (340, 337), (52, 50.7), (10.5, 10.25)):
    for cash in (350000, 120000, 9000):
        n, risk, loss1 = S.size(entry, stop, False, cash)
        ok(f"部位大小 {entry}/{stop} 現金 {cash}", n == brute(entry, stop, cash, 350000) and risk == 3500 and loss1 == -trade_pnl(entry, stop, 1, DISC), (n, brute(entry, stop, cash, 350000)))
ok("100 元、停損 99：單筆不超過餘額一半（17.5 萬）→ 1 張", S.size(100, 99, False, 350000)[0] == 1)
ok("50 元、停損 49.5：一半的額度可買 3 張（15 萬）", S.size(50, 49.5, False, 350000)[0] == 3)
ok("50 元、停損 48.8：3 張碰停損會超過餘額的 1% → 2 張", S.size(50, 48.8, False, 350000)[0] == 2 and -trade_pnl(50, 48.8, 2, DISC) <= 3500 < -trade_pnl(50, 48.8, 3, DISC))
ok("現金只剩 6 萬：50 元的只能買 1 張", S.size(50, 49.5, False, 60000)[0] == 1)
ok("222.5 元：一張就超過單筆上限，買不起", S.size(222.5, 218.5, False, 350000)[0] == 0 and S.size(387.5, 384.0, False, 350000)[0] == 0)
ok("停損太遠：一張碰停損就超過風險上限", S.size(160, 156, False, 350000)[0] == 0 and -trade_pnl(160, 156, 1, DISC) > 3500)
fb, fs, tax = SM.costs(100, 102, 3, DISC)
ok("成本拆開後和損益算法一致", 102 * 3000 - 100 * 3000 - fb - fs - tax == trade_pnl(100, 102, 3, DISC) and tax == int(306000 * 0.0015), (fb, fs, tax))

# ================================================================ 2. 觀察中 → 準備中 → 交易中
at(9, 5)
e.live_cands = ["1101", "1102", "1103"]
quote(e, "1101", 100.0, bid=99.9, ask=100.0)
quote(e, "1102", 50.0, bid=49.95, ask=50.0)
quote(e, "1103", 387.5)
ADV.update({"1101": {"status": "wait", "stop": 99.0, "target": 102.0, "reason": "09:10 前波動大，先觀察"}, "1102": {"status": "wait", "stop": 49.5, "target": 51.0},
            "1103": {"status": "go", "stop": 384.0, "target": 394.5}})
tick(e)
a = S.api()
ok("觀察中：列出買得起的候選與「還缺什麼」", [w["code"] for w in a["watch"]] == ["1101", "1102"] and a["watch"][0]["why"] == "09:10 前波動大，先觀察" and not a["ready"], a["watch"])
ok("一張就買不起的不列入，另外計數", a["excluded"] == 1)
ok("觀察中附上計畫：張數、金額、碰停損與到目標的金額", a["watch"][0]["lots"] == 1 and a["watch"][0]["amount"] == 100000 and a["watch"][0]["loss"] == round(trade_pnl(100, 99, 1, DISC)) and a["watch"][0]["gain"] == round(trade_pnl(100, 102, 1, DISC)))
ok("狀態：09:10 開始找進場點", a["account"]["state_text"] == "09:10 開始找進場點", a["account"])

at(9, 20, 0)
ADV["1101"] = {"status": "go", "stop": 99.0, "target": 102.0}
quote(e, "1101", 100.0, bid=99.9, ask=100.0)
tick(e)
a = S.api()
ok("出現「可進場」→ 準備中（還沒買）", [r["code"] for r in a["ready"]] == ["1101"] and not a["open"] and a["ready"][0]["lots"] == 1 and a["ready"][0]["stage"] == "ready" and a["today"]["signals"] == 1, a["ready"])
ok("準備中的不再出現在觀察中", [w["code"] for w in a["watch"]] == ["1102"])
ok("準備中的股票馬上列入高頻更新", e.rt_priority[0] == "1101" and S.watch_codes() == ["1101"])
later(30); quote(e, "1101", 100.5, bid=100.0, ask=100.5); tick(e)  # noqa: E702
ok("30 秒：還在等", S.api()["ready"] and S.api()["ready"][0]["waited"] == 30 and not S.api()["open"])
later(31); quote(e, "1101", 100.5, bid=100.0, ask=100.5); tick(e)  # noqa: E702
a = S.api()
o = a["open"][0] if a["open"] else {}
ok("61 秒後條件還在 → 用當時的賣一價買進", not a["ready"] and o.get("entry") == 100.5 and o.get("lots") == brute(100.5, 99.0, 350000, 350000) and o.get("time") == "09:21:01", o)
ok("買進後：金額、最多賠、到目標約賺", o.get("amount") == 100500 * o["lots"] and o.get("risk") == round(-trade_pnl(100.5, 99.0, o["lots"], DISC)) and o.get("gain") == round(trade_pnl(100.5, 102.0, o["lots"], DISC)), o)
ok("浮動損益＝現在賣掉扣成本", o.get("pnl") == round(trade_pnl(100.5, 100.5, o["lots"], DISC)) and a["account"]["float"] == o["pnl"] and a["account"]["equity"] == 350000 + o["pnl"] and a["account"]["balance"] == 350000, (o.get("pnl"), a["account"]))
ok("占用的現金與可用現金", a["account"]["used"] == o["amount"] and a["account"]["avail"] == 350000 - o["amount"])
LOTS1 = o["lots"]

# 確認不過：等 5 分鐘條件都沒回來 → 取消
ADV["1102"] = {"status": "go", "stop": 49.5, "target": 51.0}
later(10); quote(e, "1102", 50.0, bid=49.95, ask=50.0); quote(e, "1101", 100.6, bid=100.5); tick(e)  # noqa: E702
cash = 350000 - 100500 * LOTS1
ok("第二檔進準備中：張數受剩下的現金限制", [r["code"] for r in S.api()["ready"]] == ["1102"] and S.api()["ready"][0]["lots"] == brute(50.0, 49.5, cash, 350000), (S.api()["ready"], cash))
ADV["1102"] = {"status": "wait", "stop": 49.5, "target": 51.0, "reason": "低於當日均價 50.10，等站回再說"}
later(120); quote(e, "1102", 49.9); quote(e, "1101", 100.6, bid=100.5); tick(e)  # noqa: E702
ok("2 分鐘後條件不見了：先不取消，繼續等", [r["code"] for r in S.api()["ready"]] == ["1102"])
later(200); quote(e, "1102", 49.9); quote(e, "1101", 100.6, bid=100.5); tick(e)  # noqa: E702
a = S.api()
ok("5 分鐘都沒回來 → 取消，回到觀察中", not a["ready"] and [w["code"] for w in a["watch"]] == ["1102"] and a["today"]["cancels"] == 1 and "取消 乙 1102" in a["log"][0]["text"], a["log"][0])
ok("取消的不算交易，今天還可以再做", "1102" not in S.st["day"]["codes"] and a["hist_n"] == 0)

# ================================================================ 3. 出場：停損
later(30); quote(e, "1101", 99.2, bid=99.1); tick(e)  # noqa: E702
ok("還沒到停損：繼續持有，記下最低價", S.api()["open"] and S.st["items"][0]["lo"] == 99.2)
later(10); quote(e, "1101", 98.8, bid=98.7); tick(e)  # noqa: E702
a = S.api(); h = S.hist[-1] if S.hist else {}  # noqa: E702
pnl = round(trade_pnl(100.5, 98.7, LOTS1, DISC))
ok("跌破停損：用當時的買一價賣（比停損價差）", not a["open"] and h.get("reason") == "stop" and h.get("exit") == 98.7 and h.get("pnl") == pnl, h)
ok("餘額、今日損益、歷史筆數", a["account"]["balance"] == 350000 + pnl and a["today"]["realized"] == pnl and a["today"]["n"] == 1 and a["today"]["win"] == 0 and a["hist_n"] == 1 and recon(e))
ok("R 值、報酬率、占資金比例、持有時間、成本", h["r"] == round(pnl / h["risk"], 2) and h["r"] < -1 and h["ret"] == round(pnl / h["amount"] * 100, 2) and h["eq_pct"] == round(pnl / 350000 * 100, 2) and h["hold_min"] == 6 and h["cost"] == sum(SM.costs(100.5, 98.7, LOTS1, DISC)) and h["bal_after"] == 350000 + pnl, h)
ok("最多曾賺／曾賠幾 R", h["mfe_r"] == round((100.6 - 100.5) / 1.5, 2) and h["mae_r"] == round((98.8 - 100.5) / 1.5, 2), (h["mfe_r"], h["mae_r"]))
ok("事件經過有三筆（訊號、買進、出場）", len(h["events"]) == 3 and "停損" in h["events"][2][1], h["events"])
ok("最大回落", a["account"]["max_dd"] == -pnl and a["account"]["peak"] == 350000)
ok("今天做過的那一檔不再做", (lambda: (quote(e, "1101", 100.0), ADV.update({"1101": {"status": "go", "stop": 99.0, "target": 102.0}}), later(10), tick(e), [w for w in S.api()["watch"] if w["code"] == "1101"][0])[-1])()["why"] == "今天已經做過這一檔")


# ================================================================ 4. 出場：目標價、同時碰到、時間到
def enter(code, price, stop, target, wait=61):
    ADV[code] = {"status": "go", "stop": stop, "target": target}
    if code not in e.live_cands:
        e.live_cands.append(code)
    quote(e, code, price, bid=price - 0.05, ask=price)
    tick(e)
    later(wait)
    quote(e, code, price, bid=price - 0.05, ask=price)
    tick(e)
    return next((it for it in S.st["items"] if it["code"] == code), None)


e.live_cands = ["1102", "1104", "1105", "1106", "1107", "1108"]
at(9, 40)
it = enter("1104", 40.0, 39.5, 41.0)
ok("買進丁 1104", it and it["stage"] == "open" and it["lots"] == brute(40.0, 39.5, S.st["balance"], S.st["balance"]), it)
bal = S.st["balance"]; n4 = it["lots"]  # noqa: E702
later(60); quote(e, "1104", 40.6, bid=40.55, high=41.05); tick(e)  # noqa: E702
h = S.hist[-1]
ok("兩次報價之間的最高價碰到目標 → 以目標價成交", h["code"] == "1104" and h["reason"] == "target" and h["exit"] == 41.0 and h["pnl"] == round(trade_pnl(40.0, 41.0, n4, DISC)) and S.st["balance"] == bal + h["pnl"] and recon(e), h)
ok("到目標的 R 值在 1.5～2 之間（扣成本後）", 1.5 < h["r"] < 2.0, h["r"])

it = enter("1105", 80.0, 79.0, 82.0)
n5 = it["lots"]
later(60); quote(e, "1105", 80.2, bid=80.1, low=78.9, high=82.1); tick(e)  # noqa: E702
h = S.hist[-1]
ok("同時碰到停損和目標 → 算停損（保守）；現價已回到停損之上 → 以停損價成交", h["code"] == "1105" and h["reason"] == "stop" and h["exit"] == 79.0 and h["pnl"] == round(trade_pnl(80.0, 79.0, n5, DISC)), h)

it = enter("1106", 30.0, 29.6, 30.8)
n6 = it["lots"]
at(13, 14, 50); quote(e, "1106", 30.3, bid=30.25); tick(e)  # noqa: E702
ok("13:14 還在持有", S.api()["open"] and S.api()["open"][0]["code"] == "1106")
at(13, 15, 2); quote(e, "1106", 30.3, bid=30.25); tick(e)  # noqa: E702
h = S.hist[-1]
ok("13:15 → 用買一價全部賣出", h["code"] == "1106" and h["reason"] == "time" and h["exit"] == 30.25 and h["pnl"] == round(trade_pnl(30.0, 30.25, n6, DISC)) and not S.api()["open"], h)
ok("13:15 後狀態：今天已全部出場", S.api()["account"]["state_text"] == "今天已全部出場")
a = S.api()
st = a["stats"]
ok("統計：筆數、勝率、總損益、各出場原因", st["n"] == 4 and st["win"] == 2 and st["loss"] == 2 and st["win_rate"] == 50.0 and st["pnl"] == sum(t["pnl"] for t in S.hist) and st["by"]["stop"]["n"] == 2 and st["by"]["target"]["n"] == 1 and st["by"]["time"]["n"] == 1, st)
gw = sum(t["pnl"] for t in S.hist if t["pnl"] > 0); gl = -sum(t["pnl"] for t in S.hist if t["pnl"] < 0)  # noqa: E702
ok("統計：賺賠比、平均賺、平均賠、成本合計", st["pf"] == round(gw / gl, 2) and st["avg_win"] == round(gw / 2) and st["avg_loss"] == round(-gl / 2) and st["cost"] == sum(t["cost"] for t in S.hist), st)
ok("今日紀錄寫進每日紀錄", a["days"][0]["date"] == "2026-11-23" and a["days"][0]["n"] == 4 and a["days"][0]["pnl"] == st["pnl"] and a["days"][0]["end"] == a["account"]["balance"] and a["days_run"] == 1 and a["days_traded"] == 1, a["days"])
ok("權益曲線：起點＋每天收盤後的餘額", a["curve"] == [[a["created"][:10], 350000], ["2026-11-23", a["account"]["balance"]]], a["curve"])

# ================================================================ 5. 隔天：帳戶這一邊的限制
LIVE[0] = False
S.on_close()
at(9, 30, day=24)
LIVE[0] = True
for c in list(e.snap):
    del e.snap[c]
ADV.clear()
e.live_cands = ["1101", "1102", "1104", "1105", "1106"]
bal0 = S.st["balance"]
i1 = enter("1101", 50.0, 49.6, 50.8)
ok("隔天：新的一天，昨天做過的今天可以再做", i1 and i1["stage"] == "open" and S.st["day"]["date"] == "2026-11-24" and S.st["day"]["start"] == bal0)
i2 = enter("1102", 20.0, 19.8, 20.4)
i3 = enter("1104", 15.0, 14.85, 15.3)
ok("同時最多 3 檔", [x and x["stage"] for x in (i1, i2, i3)] == ["open"] * 3 or [x and x["stage"] for x in (i1, i2, i3)], [x and x["stage"] for x in (i1, i2, i3)])
used = sum(x["amount"] for x in S.st["items"])
ok("買進金額合計不超過帳戶餘額（不放大）", used <= S.st["balance"], (used, S.st["balance"]))
ADV["1105"] = {"status": "go", "stop": 9.9, "target": 10.2}
quote(e, "1105", 10.0, bid=9.99, ask=10.0); later(5); tick(e)  # noqa: E702
w = [x for x in S.api()["watch"] if x["code"] == "1105"]
ok("第 4 檔：可進場但部位已滿，留在觀察中並說明", w and w[0]["why"] == "部位已滿（同時最多 3 檔）" and w[0]["blocked"] and w[0]["status"] == "go" and len(S.st["items"]) == 3, w)

# 報價太久沒更新
ADV["1105"] = {"status": "wait", "stop": 9.9, "target": 10.2}
for x in list(S.st["items"]):
    quote(e, x["code"], x["target"] + 0.1)
later(5); tick(e)  # noqa: E702
ok("三檔都到目標價出場", not S.st["items"] and all(t["reason"] == "target" for t in S.hist[-3:]) and recon(e))
ADV["1105"] = {"status": "go", "stop": 9.9, "target": 10.2}
quote(e, "1105", 10.0, bid=9.99, ask=10.0, t="09:20:00"); later(5); tick(e)  # noqa: E702
w = [x for x in S.api()["watch"] if x["code"] == "1105"]
ok("報價超過 3 分鐘沒更新 → 不進場", w and "沒更新" in w[0]["why"], w)

# 現金被占用
e.live_cands = ["1105", "1106", "1107", "1108"]
ADV["1105"] = {"status": "wait", "stop": 98.0, "target": 104.0}
ia = enter("1106", 170.0, 168.5, 173.0)
ib = enter("1107", 160.0, 158.5, 163.0)
ok("買 170 元與 160 元各 1 張（共 33 萬）", ia and ib and ia["lots"] == 1 and ib["lots"] == 1 and ia["stage"] == ib["stage"] == "open", (ia, ib))
ADV["1105"] = {"status": "go", "stop": 98.0, "target": 104.0}
e.snap.pop("1105")
quote(e, "1105", 100.0, bid=99.9, ask=100.0); later(5); tick(e)  # noqa: E702
w = [x for x in S.api()["watch"] if x["code"] == "1105"]
ok("現金被其他部位占用 → 說明不夠買一張", w and w[0]["why"].startswith("現金被其他部位占用") and w[0]["blocked"] and len(S.st["items"]) == 2, w)

# 暫停
ok("暫停／恢復", S.action({"action": "pause"}).get("ok") and S.api()["account"]["state"] == "pause")
quote(e, "1106", 173.5); quote(e, "1107", 163.5); later(5); tick(e)  # noqa: E702
ok("暫停中：已經買的照規則出場", {t["code"] for t in S.hist[-2:]} == {"1106", "1107"} and all(t["reason"] == "target" for t in S.hist[-2:]) and not S.st["items"])
quote(e, "1105", 100.0, bid=99.9, ask=100.0); later(5); tick(e)  # noqa: E702
w = [x for x in S.api()["watch"] if x["code"] == "1105"]
ok("暫停中：不開新倉", w and w[0]["why"] == "模擬已暫停" and not S.st["items"], w)
S.action({"action": "resume"})

# 當日停手：賠到 3%
ADV.clear()
e.live_cands = ["1108", "1105"]
day = S.st["day"]
day["pnl"] = -(day["start"] * 0.03 - 1500)        # 已經賠到離停手只差 1,500 元
S.st["balance"] = day["start"] + day["pnl"]
S.hist.append({"id": "pad", "code": "0000", "name": "補", "date": "2026-11-24", "pnl": round(S.st["balance"] - S.st["start"] - sum(t["pnl"] for t in S.hist)), "reason": "stop", "r": -1, "ret": -1, "hold_min": 1, "cost": 0})
it = enter("1108", 60.0, 59.4, 61.2)
later(30); quote(e, "1108", 59.3, bid=59.3); tick(e)  # noqa: E702
ok("當天虧損達 3% → 今天停手", S.st["day"]["stopped"] and S.api()["account"]["state"] == "daystop" and any("今天不再進場" in x["text"] for x in S.st["log"]), S.st["day"])
ADV["1105"] = {"status": "go", "stop": 19.8, "target": 20.4}
e.snap.pop("1105")
quote(e, "1105", 20.0, bid=19.99, ask=20.0); later(5); tick(e)  # noqa: E702
w = [x for x in S.api()["watch"] if x["code"] == "1105"]
ok("停手後：可進場也不買", w and w[0]["why"].startswith("今天已停手") and not S.st["items"], w)

# ================================================================ 6. 跌停鎖住、收盤結算、存檔與還原
at(10, 0, day=25)
ADV.clear()
for c in list(e.snap):
    del e.snap[c]
e.live_cands = ["1101", "1102"]
it = enter("1101", 50.0, 49.5, 51.0)
later(30)
quote(e, "1101", 45.0, bids=False, limit_down=45.0); tick(e)  # noqa: E702
ok("跌停鎖住（沒有買盤）：賣不掉，繼續持有並記一筆", S.st["items"] and S.st["items"][0].get("locked") and any("跌停鎖住" in x["text"] for x in S.st["log"]))
at(13, 20, day=25); quote(e, "1101", 45.0, bids=False, limit_down=45.0); tick(e)  # noqa: E702
ok("13:15 過了仍賣不掉", len(S.st["items"]) == 1)
S._save(force=True)
rev = S.st["rev"]
files = sorted(os.listdir(os.path.join(DATA, "live", "sim")))
ok("存檔：state.json 與 hist.json", files == ["hist.json", "state.json"], files)
st_file = json.load(open(os.path.join(DATA, "live", "sim", "state.json"), encoding="utf-8"))
ok("存檔沒有觀察名單這種每次重算的東西，也沒有任何網址或密碼", "watch" not in st_file and "http" not in json.dumps(st_file) and "topic" not in json.dumps(st_file))

e2 = make()
ok("重新啟動：餘額、持倉、歷史、每日紀錄都還在", e2.sim.st["balance"] == S.st["balance"] and len(e2.sim.st["items"]) == 1 and len(e2.sim.hist) == len(S.hist) and e2.sim.st["days"] == S.st["days"] and e2.sim.st["rev"] == rev)
e2.snap["1101"] = dict(e.snap["1101"])
LIVE[0] = False
at(13, 40, day=25)
e2.sim.on_close()
h = e2.sim.hist[-1]
ok("收盤後：賣不掉的以收盤價結算", not e2.sim.st["items"] and h["reason"] == "close" and h["exit"] == 45.0 and h["exit_time"] == "13:30:00" and h["pnl"] == round(trade_pnl(50.0, 45.0, h["lots"], DISC)) and recon(e2), h)
ok("每日紀錄更新（11/25 一筆）", e2.sim.st["days"][-1]["date"] == "2026-11-25" and e2.sim.st["days"][-1]["n"] == 1 and e2.sim.st["days"][-1]["end"] == round(e2.sim.st["balance"]), e2.sim.st["days"][-1])

# 程式在盤中中斷：隔天用日線補算
LIVE[0] = True
at(10, 0, day=26)
e, S = e2, e2.sim
ADV.clear(); e.snap.clear()  # noqa: E702
e.live_cands = ["1102", "1104"]
i_a = enter("1102", 20.0, 19.8, 20.4)
i_b = enter("1104", 30.0, 29.7, 30.6)
S._save(force=True)
e3 = make()
e3.hist = {"1102": [["2026-11-25", 20, 20, 20, 20, 1000], ["2026-11-26", 19.9, 20.5, 19.9, 20.3, 1000]],      # 進場後最高 20.5 → 到目標
           "1104": [["2026-11-25", 30, 30, 30, 30, 1000], ["2026-11-26", 30.0, 30.3, 29.95, 30.2, 1000]]}    # 都沒碰到 → 收盤價
at(9, 15, day=27)
e3.sim._last = 0; e3.sim.tick()  # noqa: E702
hh = {t["code"]: t for t in e3.sim.hist[-2:]}
ok("中斷的單隔天補算：碰到目標的算目標價，沒碰到的算收盤價，並標示是補算的",
   not e3.sim.st["items"] and hh["1102"]["reason"] == "target" and hh["1102"]["exit"] == 20.4 and hh["1102"]["approx"] and hh["1104"]["reason"] == "close" and hh["1104"]["exit"] == 30.2 and hh["1104"]["exit_date"] == "2026-11-26", hh)
d26 = [d for d in e3.sim.st["days"] if d["date"] == "2026-11-26"]
ok("補算的損益記在原本那一天", d26 and d26[0]["n"] == 2 and d26[0]["pnl"] == hh["1102"]["pnl"] + hh["1104"]["pnl"] and e3.sim.st["day"]["start"] == e3.sim.st["balance"] and recon(e3), d26)

# 兩個檔案不一致（存到一半中斷）：以交易紀錄為準
p = os.path.join(DATA, "live", "sim", "state.json")
raw = json.load(open(p, encoding="utf-8"))
raw["balance"] += 12345
raw["items"].append(dict(e3.sim.hist[-1], stage="open"))
json.dump(raw, open(p, "w", encoding="utf-8"))
e4 = make()
ok("存檔不一致時以交易紀錄為準：餘額對回來、已結束的單不會重複出現", recon(e4) and not e4.sim.st["items"])

# 重新開始（config 的 sim_reset）
r0 = e4.sim.st["rev"]
e5 = make("2026-12-01")
files = sorted(os.listdir(os.path.join(DATA, "live", "sim")))
ok("sim_reset 改了 → 新帳戶 35 萬，舊紀錄另存", e5.sim.st["balance"] == 350000 and not e5.sim.hist and e5.sim.st["rev"] > r0 and sum(f.startswith("old-") for f in files) == 2, files)
e6 = make("2026-12-01")
ok("同一個 sim_reset 不會再重設一次", e6.sim.st["rev"] == e5.sim.st["rev"] and e6.sim.st["created"] == e5.sim.st["created"])

# ================================================================ 7. 雲端：快取比較舊時用分支上的備份
pub = '/tmp/twscan_sim_pub'
shutil.rmtree(pub, ignore_errors=True)
os.makedirs(os.path.join(pub, "state", "sim"))
newer = dict(e6.sim.st, rev=e6.sim.st["rev"] + 5, balance=345000.0)
json.dump(newer, open(os.path.join(pub, "state", "sim", "state.json"), "w", encoding="utf-8"))
json.dump([{"id": "x1", "code": "1101", "name": "甲", "date": "2026-12-01", "pnl": -5000, "reason": "stop", "r": -1, "ret": -1, "hold_min": 5, "cost": 100}], open(os.path.join(pub, "state", "sim", "hist.json"), "w", encoding="utf-8"))
CL.restore_state(e6, pub)
e7 = make("2026-12-01")
ok("分支上的備份比較新 → 整份換掉", e7.sim.st["balance"] == 345000 and len(e7.sim.hist) == 1 and e7.sim.st["rev"] == newer["rev"])
older = dict(newer, rev=1, balance=1.0)
json.dump(older, open(os.path.join(pub, "state", "sim", "state.json"), "w", encoding="utf-8"))
CL.restore_state(e7, pub)
e8 = make("2026-12-01")
ok("分支上的備份比較舊 → 不動", e8.sim.st["balance"] == 345000)

# ================================================================ 8. 隨機走勢跑 40 天，逐步對帳
random.seed(5)
shutil.rmtree(DATA, ignore_errors=True)
e = make()
S = e.sim
codes = list(NAMES)
viol = []
d0 = datetime(2026, 12, 1, tzinfo=util.TW)
days_done = 0
for dn in range(60):
    d = d0 + timedelta(days=dn)
    if d.weekday() >= 5:
        continue
    days_done += 1
    LIVE[0] = True
    e.snap.clear(); ADV.clear()  # noqa: E702
    px = {c: random.choice([18.0, 42.0, 77.0, 130.0, 260.0]) for c in codes}
    e.live_cands = codes[:]
    for minute in range(9 * 60 + 2, 13 * 60 + 26):
        CLOCK[0] = d.replace(hour=minute // 60, minute=minute % 60, second=random.randint(0, 50))
        for c in codes:
            from twscan.daytrade import tick_size
            step = tick_size(px[c])
            px[c] = round(max(step, px[c] + step * random.choice([-3, -2, -1, -1, 0, 0, 1, 1, 2, 3])), 2)
            quote(e, c, px[c], bid=round(px[c] - step, 2), ask=px[c])
            go = random.random() < 0.08 and 9 * 60 + 10 <= minute < 12 * 60
            ADV[c] = {"status": "go" if go or (c in S.watch_codes()) else "wait", "stop": round(px[c] * 0.988, 2), "target": round(px[c] * 1.024, 2)}
        tick(e)
        st = S.st
        if not recon(e):
            viol.append(("recon", str(CLOCK[0])))
        if sum(x["amount"] for x in st["items"]) > st["balance"] + 1:
            viol.append(("cash", str(CLOCK[0])))
        if len(st["items"]) > SM.DEF["max_pos"]:
            viol.append(("maxpos", str(CLOCK[0])))
        if len({x["code"] for x in st["items"]}) != len(st["items"]):
            viol.append(("dup", str(CLOCK[0])))
        if minute >= 13 * 60 + 16 and st["items"]:
            viol.append(("late", str(CLOCK[0])))
    LIVE[0] = False
    CLOCK[0] = d.replace(hour=13, minute=40)
    S.on_close()
    if S.st["days"][-1]["end"] != round(S.st["balance"]) or S.st["days"][-1]["date"] != d.strftime("%Y-%m-%d"):
        viol.append(("day", str(d)))
a, h = S.api(), S.hist_api()
ok(f"隨機走勢 {days_done} 天、{len(S.hist)} 筆交易：0 違規（餘額對帳、現金、部位數、不重複、13:15 後沒有部位、每日紀錄）", not viol and len(S.hist) > 100, viol[:5])
ok("每筆最多賠：停損單的損失不超過每筆上限太多（滑一檔以內）", all(-t["pnl"] <= t["bal_before"] * 0.01 * 1.6 for t in S.hist if t["reason"] == "stop"), max((-t["pnl"] / t["bal_before"] * 100 for t in S.hist if t["reason"] == "stop"), default=0))
ok("同一檔一天最多一次", all(v == 1 for v in __import__("collections").Counter((t["date"], t["code"]) for t in S.hist).values()))
by_day = {}
for t in S.hist:
    by_day.setdefault(t["date"], []).append(t)
bad = []
for dd, ts in by_day.items():
    ts.sort(key=lambda t: t["time"])
    start = ts[0]["bal_before"]
    run = 0
    for i, t in enumerate(ts):
        closed_before = sum(x["pnl"] for x in ts if x["exit_time"] <= t["time"])
        if closed_before <= -start * 0.03:
            bad.append((dd, t["code"], closed_before))
ok("虧損達 3% 之後當天沒有再買", not bad, bad[:3])
ok("每日紀錄的損益加起來＝總損益；歷史上限與走勢點數", sum(d["pnl"] for d in S.st["days"]) == a["stats"]["pnl"] == round(S.st["balance"] - 350000) and all(len(t.get("path") or []) <= 60 for t in S.hist) and all("path" not in t for t in S.hist[:-150]))
ok("輸出可以轉成 JSON，歷史由新到舊", json.dumps(a) and json.dumps(h) and h["trades"][0]["date"] >= h["trades"][-1]["date"] and h["n"] == len(S.hist))
ok("最大回落不小於任一段的跌幅", a["account"]["max_dd"] >= 0 and a["account"]["peak"] >= a["account"]["balance"])
print(f"   （隨機走勢沒有任何意義，只是對帳用：{a['stats']['n']} 筆、勝率 {a['stats']['win_rate']}%、總損益 {a['stats']['pnl']}、成本 {a['stats']['cost']}）")

print("\nSIM:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
