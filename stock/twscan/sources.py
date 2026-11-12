"""各資料來源的抓取與解析。每個函式失敗時丟出例外，由 engine 記錄並略過。"""
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote

from .util import TW, num, to_iso_date

STOCK_RE = re.compile(r"^\d{4}$")              # 一般股票：4 位數字
ETF_RE = re.compile(r"^00\d{2,4}[A-Z]?$")       # ETF：00 開頭，例如 0050、00878、00631L


def code_type(code):
    if ETF_RE.match(code):
        return "etf"
    if STOCK_RE.match(code) and code[0] != "0":
        return "stock"
    return None


def _pick(d, *names):
    """依序找第一個存在的欄位；也接受大小寫/空白不同的鍵。"""
    for n in names:
        if n in d:
            return d[n]
    low = {str(k).strip().lower(): v for k, v in d.items()}
    for n in names:
        v = low.get(n.lower())
        if v is not None:
            return v
    return None


# ---------------------------------------------------------------- 全市場日行情
def fetch_twse_daily_all(http):
    """證交所 OpenAPI：上市所有證券最近一個交易日的開高低收量。"""
    rows = http.get_json("https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL")
    out = []
    for r in rows:
        code = str(_pick(r, "Code", "證券代號") or "").strip()
        t = code_type(code)
        if not t:
            continue
        close = num(_pick(r, "ClosingPrice", "收盤價"))
        chg = num(_pick(r, "Change", "漲跌價差"))
        out.append({
            "code": code, "name": str(_pick(r, "Name", "證券名稱") or "").strip(), "market": "TWSE", "type": t,
            "date": to_iso_date(_pick(r, "Date", "日期")),
            "open": num(_pick(r, "OpeningPrice", "開盤價")), "high": num(_pick(r, "HighestPrice", "最高價")),
            "low": num(_pick(r, "LowestPrice", "最低價")), "close": close,
            "prev": (close - chg) if (close is not None and chg is not None) else None,
            "volume": num(_pick(r, "TradeVolume", "成交股數")), "value": num(_pick(r, "TradeValue", "成交金額")),
        })
    return out


def fetch_tpex_daily_all(http):
    """櫃買中心 OpenAPI：上櫃所有證券最近一個交易日行情（欄位值都是字串，可能有空白）。"""
    rows = http.get_json("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes")
    out = []
    for r in rows:
        code = str(_pick(r, "SecuritiesCompanyCode", "Code", "代號") or "").strip()
        t = code_type(code)
        if not t:
            continue
        close = num(_pick(r, "Close", "收盤"))
        chg = num(_pick(r, "Change", "漲跌"))
        out.append({
            "code": code, "name": str(_pick(r, "CompanyName", "Name", "名稱") or "").strip(), "market": "TPEx", "type": t,
            "date": to_iso_date(_pick(r, "Date", "日期")),
            "open": num(_pick(r, "Open", "開盤")), "high": num(_pick(r, "High", "最高")),
            "low": num(_pick(r, "Low", "最低")), "close": close,
            "prev": (close - chg) if (close is not None and chg is not None) else None,
            "volume": num(_pick(r, "TradingShares", "TradeVolume", "成交股數")),
            "value": num(_pick(r, "TransactionAmount", "TradeValue", "成交金額")),
        })
    return out


# ---------------------------------------------------------------- 證券代號與產業別
def fetch_isin(http, mode):
    """證交所 ISIN 頁：mode=2 上市、mode=4 上櫃。回傳 {code: {name, industry, type}}。"""
    html = http.get_text(f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}",
                         encoding=("cp950", "big5hkscs", "utf-8"), timeout=40)
    out = {}
    section = ""
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S | re.I):
        tds = [re.sub(r"<[^>]+>", "", td).strip() for td in re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S | re.I)]
        if len(tds) == 1:
            section = tds[0].strip()
            continue
        if len(tds) < 5:
            continue
        first = tds[0].replace("　", " ").strip()
        parts = first.split(" ", 1)
        if len(parts) != 2:
            continue
        code, name = parts[0].strip(), parts[1].strip()
        t = code_type(code)
        if not t:
            continue
        if t == "stock" and "股票" not in section and section:
            continue
        industry = tds[4].strip() if len(tds) > 4 else ""
        out[code] = {"name": name, "industry": industry or ("ETF" if t == "etf" else ""), "type": t}
    if not out:
        raise ValueError("ISIN 頁面解析不到資料")
    return out


# ---------------------------------------------------------------- 即時行情（基本市況報導）
def mis_channel(code, market):
    return f"{'tse' if market == 'TWSE' else 'otc'}_{code}.tw"


def _split_levels(s):
    vals = [num(x) for x in str(s or "").split("_") if x.strip()]
    return [v for v in vals if v is not None]


def fetch_mis(http, channels):
    """證交所基本市況報導 API，一次最多約 100 檔。回傳 list[dict]。"""
    if not channels:
        return []
    if not getattr(http, "_mis_warm", False):
        try:  # 先開首頁取得 session cookie（部分情況下沒有 cookie 會回傳空資料）
            http.get("https://mis.twse.com.tw/stock/index.jsp", timeout=15, retries=0)
        except Exception:
            pass
        http._mis_warm = True
    ts = int(time.time() * 1000)
    url = ("https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch="
           + "%7C".join(channels) + f"&json=1&delay=0&_={ts}")
    j = http.get_json(url, headers={"Referer": "https://mis.twse.com.tw/stock/index.jsp"}, timeout=20)
    out = []
    for m in j.get("msgArray", []) or []:
        code = str(m.get("c") or "").strip()
        if not code:
            continue
        asks, askv = _split_levels(m.get("a")), _split_levels(m.get("f"))
        bids, bidv = _split_levels(m.get("b")), _split_levels(m.get("g"))
        d = str(m.get("d") or "")
        out.append({
            "code": code, "name": (m.get("n") or "").strip(), "ex": m.get("ex"),
            "price": num(m.get("z")), "prev": num(m.get("y")), "open": num(m.get("o")),
            "high": num(m.get("h")), "low": num(m.get("l")), "vol": num(m.get("v")),  # 累積成交量（張）
            "tick_vol": num(m.get("tv")), "limit_up": num(m.get("u")), "limit_down": num(m.get("w")),
            "asks": asks[:5], "ask_vols": askv[:5], "bids": bids[:5], "bid_vols": bidv[:5],
            "date": to_iso_date(d) if d else None, "time": m.get("t"),
            # z（當盤成交價）只有揭示那一刻剛好有成交才有值，大多數時候是 "-"；pz 是集合競價的試撮／開盤價
            "trial": num(m.get("pz")), "raw_z": m.get("z"),
        })
    return out


# ---------------------------------------------------------------- 歷史日 K
def fetch_yahoo_history(http, symbol, rng="1y"):
    """Yahoo Finance 日 K（未還原權息）。symbol 例：2330.TW、6488.TWO、^TWII。"""
    sym = quote(symbol, safe=".")
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{sym}?range={rng}&interval=1d&includePrePost=false"
        try:
            j = http.get_json(url, timeout=20, retries=1)
            break
        except Exception as e:  # 換另一台主機再試
            last = e
            j = None
    if j is None:
        raise last
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        err = (j.get("chart") or {}).get("error")
        raise ValueError(f"Yahoo 無資料 {symbol}: {err}")
    res = res[0]
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    adj = (((res.get("indicators") or {}).get("adjclose") or [{}])[0] or {}).get("adjclose") or []
    off = (res.get("meta") or {}).get("gmtoffset", 28800)
    bars = {}
    for i, t in enumerate(ts):
        try:
            o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c) or c <= 0:
            continue
        d = datetime.fromtimestamp(t + off, tz=timezone.utc).date().isoformat()
        a = adj[i] if i < len(adj) and adj[i] else c
        # 第 7 欄：還原權息收盤價（Yahoo adjclose），用來計算含股利的真實報酬
        bars[d] = [d, round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v or 0), round(a, 4)]
    return [bars[k] for k in sorted(bars)]


def bucket_label(date, minutes, size):
    """把當天的時間（從 00:00 起算的分鐘數）對應到 K 棒的起始時間標籤。臺股 09:00–13:30，最後一根從 13:00 開始。"""
    idx = min(max(minutes - 540, 0) // size, 240 // size)
    m = 540 + idx * size
    return f"{date} {m // 60:02d}:{m % 60:02d}"


def fetch_yahoo_intraday(http, symbol, interval="30m", rng="1mo"):
    """Yahoo Finance 分時 K 線（30m 最多 60 天、60m 最多約 2 年）。回傳 [["YYYY-MM-DD HH:MM", 開, 高, 低, 收, 量(股)], ...]。
    Yahoo 的最後一筆常是「目前報價」而不是整根 K 棒，13:30 的收盤集合競價也會自成一筆，這裡一律併回所屬的 K 棒。"""
    size = int(interval.rstrip("m"))
    sym = quote(symbol, safe=".")
    last = None
    for host in ("query1.finance.yahoo.com", "query2.finance.yahoo.com"):
        url = f"https://{host}/v8/finance/chart/{sym}?range={rng}&interval={interval}&includePrePost=false"
        try:
            j = http.get_json(url, timeout=10, retries=1)
            break
        except Exception as e:
            last = e
            j = None
    if j is None:
        raise last
    res = (j.get("chart") or {}).get("result") or []
    if not res:
        raise ValueError(f"Yahoo 無分時資料 {symbol}: {(j.get('chart') or {}).get('error')}")
    res = res[0]
    ts = res.get("timestamp") or []
    q = ((res.get("indicators") or {}).get("quote") or [{}])[0]
    off = (res.get("meta") or {}).get("gmtoffset", 28800)
    bars, order = {}, []
    for i, t in enumerate(ts):
        try:
            o, h, l, c, v = q["open"][i], q["high"][i], q["low"][i], q["close"][i], q["volume"][i]
        except (KeyError, IndexError):
            continue
        if None in (o, h, l, c) or c <= 0:
            continue
        dt = datetime.fromtimestamp(t + off, tz=timezone.utc)
        key = bucket_label(dt.date().isoformat(), dt.hour * 60 + dt.minute, size)
        b = bars.get(key)
        if b is None:
            bars[key] = [key, round(o, 2), round(h, 2), round(l, 2), round(c, 2), int(v or 0)]
            order.append(key)
        else:
            b[2], b[3], b[4], b[5] = max(b[2], round(h, 2)), min(b[3], round(l, 2)), round(c, 2), b[5] + int(v or 0)
    return [bars[k] for k in sorted(order)]


def fetch_twse_month(http, code, yyyymm):
    """證交所個股月成交資訊（備援用，較慢）。yyyymm 例：'202611'。"""
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/STOCK_DAY?date={yyyymm}01&stockNo={code}&response=json"
    j = http.get_json(url)
    if j.get("stat") != "OK":
        return []
    f = j.get("fields") or []

    def idx(key):
        for i, name in enumerate(f):
            if key in name:
                return i
        return None
    i_d, i_v, i_o, i_h, i_l, i_c = idx("日期"), idx("成交股數"), idx("開盤"), idx("最高"), idx("最低"), idx("收盤")
    out = []
    for row in j.get("data") or []:
        try:
            d = to_iso_date(row[i_d])
            o, h, l, c, v = num(row[i_o]), num(row[i_h]), num(row[i_l]), num(row[i_c]), num(row[i_v])
        except (TypeError, IndexError):
            continue
        if d and None not in (o, h, l, c):
            out.append([d, o, h, l, c, int(v or 0)])
    return out


# ---------------------------------------------------------------- 三大法人
def fetch_t86(http, ymd):
    """證交所三大法人買賣超日報（上市）。ymd 例：'20261106'。回傳 {code: {f, t, d, total}}（單位：股）。"""
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?date={ymd}&selectType=ALLBUT0999&response=json"
    j = http.get_json(url)
    if j.get("stat") != "OK":
        return None  # 非交易日或尚未公布
    f = j.get("fields") or []

    def find(pred):
        for i, name in enumerate(f):
            if pred(name):
                return i
        return None
    i_code = find(lambda n: "代號" in n)
    i_f = find(lambda n: "外陸資買賣超" in n or ("外資" in n and "買賣超" in n and "自營商" not in n))
    i_t = find(lambda n: n.startswith("投信買賣超"))
    i_d = find(lambda n: n == "自營商買賣超股數") or find(lambda n: n.startswith("自營商買賣超"))
    i_tot = find(lambda n: "三大法人買賣超" in n)
    out = {}
    for row in j.get("data") or []:
        try:
            code = str(row[i_code]).strip()
        except (TypeError, IndexError):
            continue
        g = lambda i: (num(row[i]) if i is not None and i < len(row) else None) or 0.0
        out[code] = {"f": g(i_f), "t": g(i_t), "d": g(i_d), "total": g(i_tot)}
    return out


def fetch_tpex_insti(http):
    """櫃買中心三大法人（OpenAPI 只給最近一日）。欄位名稱以關鍵字比對，格式變動時盡量容錯。"""
    rows = http.get_json("https://www.tpex.org.tw/openapi/v1/tpex_3insti_daily_trading")
    if not rows:
        return None, None
    keys = list(rows[0].keys())

    def find(*must, exclude=()):
        for k in keys:
            kl = k.lower()
            if all(m.lower() in kl for m in must) and not any(x.lower() in kl for x in exclude):
                return k
        return None
    k_code = find("code") or find("代號")
    k_date = find("date") or find("日期")
    fk = [k for k in keys if ("foreign" in k.lower() and ("difference" in k.lower() or "net" in k.lower())
                              and not k.lower().startswith("foreign dealer"))]
    fk.sort(key=lambda k: (("excluded" not in k.lower() and "不含" not in k), "total" in k.lower(), len(k)))
    k_f = fk[0] if fk else find("外資", "買賣超", exclude=("自營商",))
    k_t = find("investmenttrust", "difference") or find("trust", "net") or find("投信", "買賣超")
    k_d = find("dealers", "difference", exclude=("foreign", "hedg", "proprietary")) or find("自營商", "買賣超", exclude=("外資", "避險", "自行"))
    k_tot = find("total", "difference") or find("total", "net") or find("三大法人", "合計") or find("合計")
    if not k_code:
        raise ValueError(f"TPEx 法人欄位無法辨識：{keys[:8]}")
    date = to_iso_date(rows[0].get(k_date)) if k_date else None
    out = {}
    for r in rows:
        code = str(r.get(k_code) or "").strip()
        if not code_type(code):
            continue
        g = lambda k: (num(r.get(k)) if k else None) or 0.0
        f, t, d = g(k_f), g(k_t), g(k_d)
        tot = g(k_tot) if k_tot else f + t + d
        out[code] = {"f": f, "t": t, "d": d, "total": tot}
    return date, out


# ---------------------------------------------------------------- 新聞
def fetch_google_news(http, query, days=7):
    q = quote(f"{query} when:{days}d")
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
    xml = http.get_text(url, timeout=20)
    root = ET.fromstring(xml)
    items = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3].strip()
        pub = it.findtext("pubDate")
        try:
            ts = parsedate_to_datetime(pub).astimezone(TW).timestamp() if pub else None
        except (TypeError, ValueError):
            ts = None
        items.append({"title": title, "link": (it.findtext("link") or "").strip(), "source": src, "ts": ts})
    return items


def fetch_cnyes_news(http, limit=50):
    """鉅亨網台股新聞列表（非官方介面，失敗就略過）。"""
    j = http.get_json(f"https://api.cnyes.com/media/api/v1/newslist/category/tw_stock?limit={limit}", timeout=20)
    data = ((j.get("items") or {}).get("data")) or j.get("data") or []
    out = []
    for n in data:
        nid = n.get("newsId") or n.get("id")
        title = (n.get("title") or "").strip()
        if not title:
            continue
        ts = n.get("publishAt") or n.get("publishedAt")
        try:
            ts = float(ts)
        except (TypeError, ValueError):
            ts = None
        out.append({"title": title, "link": f"https://news.cnyes.com/news/id/{nid}" if nid else "",
                    "source": "鉅亨網", "ts": ts})
    return out
