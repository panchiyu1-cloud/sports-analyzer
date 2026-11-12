"""新聞情緒：以財經詞庫比對標題，依發布時間加權（越新權重越高）。"""
import math
import time

POS = [
    "創新高", "新高", "大漲", "漲停", "攻頂", "飆漲", "噴出", "強攻", "勁揚", "走揚", "上揚", "大單", "看好", "調升", "上修",
    "升評", "買進評等", "目標價上看", "調高目標價", "獲利成長", "營收成長", "營收創", "年增", "月增", "轉盈", "倍增",
    "接單", "訂單", "擴產", "擴廠", "得標", "拿下", "打入", "合作", "庫藏股", "高配息", "配息創", "買超", "加碼",
    "利多", "受惠", "需求強", "漲價", "調漲", "報喜", "優於預期", "超乎預期", "超預期", "樂觀", "回溫", "復甦", "突破",
    "翻紅", "反彈", "吸金", "搶進", "長紅", "填息", "旺季", "新產品", "量產", "認證", "獲利創", "EPS創",
]
NEG = [
    "大跌", "重挫", "跌停", "崩跌", "暴跌", "下挫", "走低", "翻黑", "收黑", "破底", "新低", "看壞", "調降", "下修", "降評",
    "賣出評等", "減碼", "虧損", "轉虧", "衰退", "年減", "月減", "獲利下滑", "營收下滑", "砍單", "減產", "停工", "跳票",
    "違約", "下市", "利空", "賣超", "倒貨", "套牢", "疑慮", "衝擊", "不如預期", "低於預期", "保守", "疲弱", "下滑",
    "失守", "跌破", "示警", "風暴", "火災", "爆炸", "解禁", "貼息", "淡季", "裁員", "訴訟", "罰款",
]
# 交易風險類字眼（處置、全額交割等），另外提示並扣分
RISK = ["處置股", "處置", "全額交割", "注意股", "警示", "掏空", "起訴", "搜索", "下市", "停止交易", "掛牌異常"]
NEGATORS = ("不", "未", "沒", "無", "非")


def _hits(title, words):
    n = 0
    for w in words:
        start = 0
        while True:
            k = title.find(w, start)
            if k < 0:
                break
            if k > 0 and title[k - 1] in NEGATORS:
                n -= 1
            else:
                n += 1
            start = k + len(w)
    return n


def title_score(title):
    p, q = _hits(title, POS), _hits(title, NEG)
    return max(-2, min(2, p - q))


def relevant(title, code, name):
    if code and code in title:
        return True
    if not name:
        return False
    base = name.replace("-KY", "").replace("*", "").strip()
    return bool(base) and (name in title or base in title)


def summarize(items, code=None, name=None, now=None):
    """回傳 {score: -1~1, n, risk, items}，items 已依時間排序並附上個別分數。"""
    now = now or time.time()
    out, seen = [], set()
    for it in items:
        t = it.get("title") or ""
        if not t or t in seen:
            continue
        if code is not None and not relevant(t, code, name):
            continue
        seen.add(t)
        ts = it.get("ts") or now
        age = max(0.0, (now - ts) / 86400)
        if age > 7.5:
            continue
        s = title_score(t)
        out.append({**it, "s": s, "age": round(age, 2)})
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    total = sum(x["s"] * math.exp(-x["age"] / 3) for x in out)
    risk = next((w for w in RISK if any(w in x["title"] for x in out[:10])), None)
    return {"score": round(math.tanh(total / 2.5), 3), "n": len(out), "risk": risk, "items": out[:12]}
