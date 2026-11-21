"""模擬戰頁面：手機寬度（390、360）的檢查與截圖。需要 Playwright 與 Chrome。
準備（在存放庫根目錄）：
    python3 stock/cloud.py --demo-live --minutes 2 --publish-dir /tmp/simpub --no-push     # 產生示範資料
    mkdir -p /tmp/simsite && ln -s "$PWD/docs/stock" /tmp/simsite/stock && ln -s /tmp/simpub /tmp/simsite/data
    (cd /tmp/simsite && python3 -m http.server 8772 &)
用法：python3 stock/tests/shots_sim.py [資料夾名稱，預設 data] [截圖資料夾，預設 /tmp/shots]
"""
import asyncio
import sys
from playwright.async_api import async_playwright

DATA = sys.argv[1] if len(sys.argv) > 1 else "data"
BASE = f"http://localhost:8772/stock/index.html?data=/{DATA}/"
OUT = sys.argv[2] if len(sys.argv) > 2 else "/tmp/shots"
FAILS = []


def ok(name, cond, info=None):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond or info is None else f"  → {info}"))
    if not cond:
        FAILS.append(name)


async def main():
    __import__('os').makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/google-chrome", args=["--no-sandbox"])
        for width in (390, 360):
            errs = []
            ctx = await b.new_context(viewport={"width": width, "height": 844}, device_scale_factor=2, accept_downloads=True)
            pg = await ctx.new_page()
            pg.on("console", lambda m: errs.append(f"{m.type}: {m.text}") if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
            await pg.goto(BASE + "#sim")
            await pg.wait_for_timeout(3000)
            tabs = await pg.locator(".tabs a").all_inner_texts()
            ok(f"[{width}] 選單有「模擬戰」而且是目前這一頁", "模擬戰" in tabs and await pg.locator('.tabs a.active').inner_text() == "模擬戰", tabs)
            over = await pg.evaluate("(() => { const t = document.querySelector('.tabs'); return t.scrollWidth - t.clientWidth; })()")
            print(f"   選單超出寬度 {over}px")
            acct = (await pg.locator(".sim-acct").inner_text()).replace("\n", " ")
            ok(f"[{width}] 帳戶：權益、總損益、今天、勝率", all(k in acct for k in ("帳戶權益", "總損益", "今天", "勝率", "可用現金", "最大回落")), acct[:200])
            ok(f"[{width}] 四個分頁", await pg.locator("[data-sim-tab]").all_inner_texts() and [t.split("\n")[0] for t in await pg.locator("[data-sim-tab]").all_inner_texts()] == ["觀察中", "準備中", "交易中", "歷史"], await pg.locator("[data-sim-tab]").all_inner_texts())
            sw = await pg.evaluate("document.documentElement.scrollWidth")
            ok(f"[{width}] 頁面沒有左右捲動", sw <= width, sw)
            await pg.screenshot(path=f"{OUT}/sim_{width}_top.png")
            for tab in ("watch", "ready", "open", "hist"):
                await pg.click(f'[data-sim-tab="{tab}"]')
                await pg.wait_for_timeout(900 if tab != "hist" else 1800)
                body = (await pg.locator("#simBody").inner_text()).replace("\n", " ")
                print(f"   [{tab}] {body[:150]}")
                sw = await pg.evaluate("document.documentElement.scrollWidth")
                ok(f"[{width}] {tab} 分頁沒有撐破版面", sw <= width, sw)
                await pg.locator("#simBody").screenshot(path=f"{OUT}/sim_{width}_{tab}.png")
            # 歷史：展開一筆、匯出
            rows = pg.locator("#simBody [data-sim-row]")
            n = await rows.count()
            ok(f"[{width}] 歷史有交易紀錄", n > 0, n)
            if n:
                await rows.first.click()
                await pg.wait_for_timeout(400)
                det = (await pg.locator(".sim-detail").first.inner_text()).replace("\n", " ")
                ok(f"[{width}] 展開一筆：進出場、金額、停損停利、損益、R、成本、持有時間、餘額、事件經過",
                   all(k in det for k in ("買進", "賣出", "買進金額", "停損價／目標價", "損益", "R 值", "手續費＋證交稅", "持有時間", "出場後餘額")), det[:300])
                await pg.locator(".sim-row.on").first.screenshot(path=f"{OUT}/sim_{width}_detail.png")
                async with pg.expect_download() as dl:
                    await pg.click('[data-sim-act="csv"]')
                d = await dl.value
                path = await d.path()
                txt = open(path, encoding="utf-8-sig").read()
                ok(f"[{width}] 匯出 CSV：表頭與筆數", txt.startswith("日期,代號,名稱") and len(txt.strip().splitlines()) == n + 1 and d.suggested_filename.startswith("當沖模擬戰"), (d.suggested_filename, len(txt.strip().splitlines()), n))
            await pg.click('[data-sim-tab="open"]')
            await pg.wait_for_timeout(500)
            # 下半部：績效、每天的結果、動態、規則
            low = (await pg.locator("#simRoot").inner_text()).replace("\n", " ")
            ok(f"[{width}] 績效、每天的結果、規則", all(k in low for k in ("績效", "平均每筆", "賺賠比", "每天的結果", "完整規則")), low[-300:])
            await pg.locator(".sim-rules summary").click()
            await pg.wait_for_timeout(300)
            rules = await pg.locator(".sim-rules").inner_text()
            ok(f"[{width}] 規則寫出 35 萬、1%、50%、3 檔、13:15、3%", all(k in rules for k in ("350,000", "1%", "50%", "最多 3 檔", "13:15", "3%", "不是投資建議")), rules[:200])
            await pg.screenshot(path=f"{OUT}/sim_{width}_full.png", full_page=True)
            ok(f"[{width}] 雲端版沒有暫停／重設按鈕", await pg.locator('[data-sim-act="reset"]').count() == 0)
            # 其他頁照常
            for h in ("top", "market", "review", "about"):
                await pg.goto(BASE + "#" + h)
                await pg.wait_for_timeout(1500)
            await pg.goto(BASE + "#sim")
            await pg.wait_for_timeout(1500)
            ok(f"[{width}] 切到其他頁再回來還在，分頁記得上次看的", await pg.locator('[data-sim-tab].on').inner_text() and "交易中" in await pg.locator('[data-sim-tab].on').inner_text())
            ok(f"[{width}] 沒有 JavaScript 錯誤", not errs, errs[:3])
            await ctx.close()
        await b.close()
    print("\nSIM UI:", "ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")


asyncio.run(main())
