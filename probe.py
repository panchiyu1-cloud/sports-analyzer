"""Which Taiwan stock sources answer from a GitHub runner (and with which CORS headers)."""
import json
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone

from twscan import sources as src
from twscan.net import Http

ORIGIN = "https://panchiyu1-cloud.github.io"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
TPE = timezone(timedelta(hours=8))


def raw(name, url, headers=None, show=160):
    h = {"User-Agent": UA, "Origin": ORIGIN, "Accept": "*/*"}
    h.update(headers or {})
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=25) as r:
            body = r.read()
            acao = r.headers.get("Access-Control-Allow-Origin")
            print(f"[RAW ok ] {name}: HTTP {r.status} {len(body)} bytes {time.time()-t0:.1f}s ACAO={acao!r}"
                  f" | {body[:show].decode('utf-8', 'replace')!r}")
    except urllib.error.HTTPError as e:
        body = e.read()[:show]
        print(f"[RAW ERR] {name}: HTTP {e.code} {time.time()-t0:.1f}s ACAO={e.headers.get('Access-Control-Allow-Origin')!r}"
              f" server={e.headers.get('Server')!r} | {body.decode('utf-8', 'replace')!r}")
    except Exception as e:
        print(f"[RAW ERR] {name}: {type(e).__name__}: {str(e)[:200]} {time.time()-t0:.1f}s")


def call(name, fn):
    t0 = time.time()
    try:
        v = fn()
        print(f"[FN  ok ] {name} ({time.time()-t0:.1f}s): {v}")
    except Exception as e:
        print(f"[FN  ERR] {name} ({time.time()-t0:.1f}s): {type(e).__name__}: {str(e)[:300]}")
        traceback.print_exc(limit=1)


now = datetime.now(TPE)
print("runner time (Taipei):", now.isoformat())
raw("ipinfo", "https://ipinfo.io/json", show=400)

raw("TWSE openapi STOCK_DAY_ALL", "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
raw("TPEx openapi close quotes", "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
raw("TWSE www T86", f"https://www.twse.com.tw/rwd/zh/fund/T86?date={now:%Y%m%d}&selectType=ALLBUT0999&response=json")
raw("TWSE www MI_INDEX", f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?date={now:%Y%m%d}&type=ALLBUT0999&response=json")
raw("ISIN", "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", show=60)
raw("MIS index.jsp", "https://mis.twse.com.tw/stock/index.jsp", show=60)
raw("MIS getStockInfo", "https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_2330.tw%7Cotc_6488.tw&json=1&delay=0",
    headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp"}, show=300)
raw("Yahoo query1 chart", "https://query1.finance.yahoo.com/v8/finance/chart/2330.TW?range=5d&interval=1d")
raw("Yahoo query2 chart", "https://query2.finance.yahoo.com/v8/finance/chart/2330.TW?range=5d&interval=1d")
raw("Google News RSS", "https://news.google.com/rss/search?q=%E5%8F%B0%E7%A9%8D%E9%9B%BB&hl=zh-TW&gl=TW&ceid=TW:zh-Hant", show=80)
raw("cnyes", "https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit=5", show=120)
raw("jina -> MIS", "https://r.jina.ai/https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_2330.tw%7Cotc_6488.tw&json=1&delay=0",
    headers={"X-Return-Format": "text", "X-No-Cache": "true"}, show=400)
raw("jina -> TWSE openapi", "https://r.jina.ai/https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX",
    headers={"X-Return-Format": "text"}, show=200)
raw("raw.githubusercontent (CORS check)", "https://raw.githubusercontent.com/panchiyu1-cloud/sports-analyzer/main/README.md", show=40)

http = Http(intervals={"mis.twse.com.tw": 1.5})
call("fetch_twse_daily_all", lambda: (lambda r: (len(r), r[0]["date"] if r else None, [x for x in r if x["code"] == "2330"][:1]))(src.fetch_twse_daily_all(http)))
call("fetch_tpex_daily_all", lambda: (lambda r: (len(r), r[0]["date"] if r else None, [x for x in r if x["code"] == "6488"][:1]))(src.fetch_tpex_daily_all(http)))
uni = {}
call("fetch_isin(2)", lambda: (lambda r: (uni.update({c: "TWSE" for c in r}), len(r))[1])(src.fetch_isin(http, 2)))
call("fetch_isin(4)", lambda: (lambda r: (uni.update({c: "TPEx" for c in r if c not in uni}), len(r))[1])(src.fetch_isin(http, 4)))
codes = sorted(uni)[:100] or ["2330", "2317", "2610", "0050"]
chans = [src.mis_channel(c, uni.get(c, "TWSE")) for c in codes]
call("fetch_mis(100)", lambda: (lambda r: (len(r), r[:2]))(src.fetch_mis(http, chans)))
call("fetch_mis(2330,2610,6488)", lambda: src.fetch_mis(http, ["tse_2330.tw", "tse_2610.tw", "otc_6488.tw"]))
call("fetch_yahoo_history 2330", lambda: (lambda r: (len(r), r[-2:]))(src.fetch_yahoo_history(http, "2330.TW", "1mo")))
call("fetch_yahoo_history 6488.TWO", lambda: (lambda r: (len(r), r[-1:]))(src.fetch_yahoo_history(http, "6488.TWO", "1mo")))
call("fetch_yahoo_intraday 30m", lambda: (lambda r: (len(r), r[-2:]))(src.fetch_yahoo_intraday(http, "2330.TW", "30m", "5d")))
d = now
for _ in range(6):
    d -= timedelta(days=1)
    if d.weekday() < 5:
        break
call(f"fetch_t86 {d:%Y%m%d}", lambda: (lambda r: (len(r), list(r.items())[:1] if isinstance(r, dict) else r[:1]))(src.fetch_t86(http, d.strftime("%Y%m%d"))))
call("fetch_tpex_insti", lambda: (lambda r: (len(r), list(r.items())[:1] if isinstance(r, dict) else r[:1]))(src.fetch_tpex_insti(http)))
call("fetch_google_news", lambda: (lambda r: (len(r), r[:1]))(src.fetch_google_news(http, "台積電")))
call("fetch_cnyes_news", lambda: (lambda r: (len(r), r[:1]))(src.fetch_cnyes_news(http, 5)))
call("fetch_twse_month 2330", lambda: (lambda r: (len(r), r[-1:]))(src.fetch_twse_month(http, "2330", now.strftime("%Y%m"))))
print("http stats:", json.dumps(http.stats, ensure_ascii=False, default=str))
