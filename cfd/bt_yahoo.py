#!/usr/bin/env python3
"""Yahoo 的 K 線下載（給差價交易模擬戰回測用）。

用 Python 內建的 urllib、和 stock/twscan/net.py 一樣的瀏覽器標頭與間隔：那一套從 GitHub 的機器抓得到
（2026-11-22 用 Node 的 fetch 抓，全部被 Yahoo 回 429）。只下載、原樣存檔；解析在 bt_fetch.js。

用法：python3 cfd/bt_yahoo.py <輸出資料夾> <jobs.json>
jobs.json：[[代號, 週期, 範圍, 要不要盤前盤後(0/1)], ...]；第 k 個存成 <輸出資料夾>/<k>.json，結果寫在 status.json。
"""
import gzip
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import quote

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept": "application/json, text/plain, text/html, application/xml, */*",
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6",
    "Accept-Encoding": "gzip, deflate",
}
GAP = 0.3          # 兩次請求之間至少隔幾秒
HOSTS = ("https://query1.finance.yahoo.com", "https://query2.finance.yahoo.com")
if os.environ.get("BT_YAHOO_BASE"):     # 只給離線測試用：改連本機的假伺服器
    HOSTS = (os.environ["BT_YAHOO_BASE"],) * 2


def body(resp):
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    if enc == "deflate":
        try:
            return zlib.decompress(raw)
        except zlib.error:
            return zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw


def main():
    out, jobs = sys.argv[1], json.load(open(sys.argv[2], encoding="utf-8"))
    os.makedirs(out, exist_ok=True)
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    status, last, refused = {}, 0.0, 0
    for k, (sym, iv, rng, prepost) in enumerate(jobs):
        err = None
        if refused >= 6 and not any(v == "ok" for v in status.values()):
            status[str(k)] = "http429"      # 一開始連續被拒絕：這台機器被擋了，不要每一個都等
            continue
        for attempt in range(3):
            host = HOSTS[(k + attempt) % 2]
            url = (f"{host}/v8/finance/chart/{quote(sym, safe='.')}"
                   f"?range={rng}&interval={iv}&includePrePost={'true' if prepost else 'false'}")
            wait = last + GAP - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            last = time.monotonic()
            try:
                with opener.open(urllib.request.Request(url, headers=HEADERS), timeout=25) as resp:
                    raw = body(resp)
                json.loads(raw)             # 不是 JSON 就當失敗
                with open(os.path.join(out, f"{k}.json"), "wb") as f:
                    f.write(raw)
                err = None
                break
            except urllib.error.HTTPError as e:      # 只記錯誤的種類
                err = f"http{e.code}"
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2.0 * (attempt + 1) + (6 if e.code == 429 else 0))
                    continue
                break
            except Exception as e:
                err = type(e).__name__
                time.sleep(1.0)
        if err == "http429":
            refused += 1
        status[str(k)] = err or "ok"
    with open(os.path.join(out, "status.json"), "w", encoding="utf-8") as f:
        json.dump(status, f)
    good = sum(1 for v in status.values() if v == "ok")
    kinds = {}
    for v in status.values():
        if v != "ok":
            kinds[v] = kinds.get(v, 0) + 1
    print(f"  Yahoo: {good}/{len(jobs)} ok {kinds or ''}")


if __name__ == "__main__":
    main()
