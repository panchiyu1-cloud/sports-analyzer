"""即時報價的「最近成交價」判斷。

證交所基本市況報導（MIS）每 5 秒揭示一次。z（當盤成交價）只有在揭示那一刻剛好有成交時才有數字，
其他時候是 "-"（實測台積電這種熱門股也大多是 "-"）。如果 "-" 就沿用很久以前看到的價格，或拿買一、
賣一的平均當價格，畫面上的價格就會和券商軟體不一樣。

這裡用三個線索決定要顯示的價格：
1. 有 z：就是最近成交價（精確）。
2. 沒有 z，但累積成交量和上次看到成交價時一樣：中間沒有新的成交，沿用那次的成交價（精確）。
3. 沒有 z，累積成交量變多了：有新的成交但不知道價格 → 以上次的成交價為基準，限制在目前的
   買一～賣一、以及今天的最高～最低之間（推估，畫面上會標「估」）。
另外處理：漲停／跌停鎖住、今天還沒成交、開盤前與收盤前 5 分鐘的集合競價試撮（試撮價不是成交價）。
"""

OPEN_TIME = "09:00:00"
CLOSE_CALL_START, CLOSE_CALL_END = "13:25:00", "13:30:00"


def in_trial(t):
    """開盤前（08:30–09:00）與收盤前（13:25–13:30）是集合競價的試撮時段。"""
    return bool(t) and (t < OPEN_TIME or CLOSE_CALL_START <= t < CLOSE_CALL_END)


def _best(q):
    b = q.get("bids") or []
    a = q.get("asks") or []
    return (b[0] if b and b[0] else None), (a[0] if a and a[0] else None)


def estimate(q, ref):
    """沒有揭示成交價、但確定有新成交時，推估最近成交價。"""
    b, a = _best(q)
    hi, lo = q.get("high"), q.get("low")
    lu, ld = q.get("limit_up"), q.get("limit_down")
    if lu and b and b >= lu and not a and hi and hi >= lu:
        return lu  # 漲停鎖住：只剩漲停價的委買
    if ld and a and a <= ld and not b and lo and lo <= ld:
        return ld  # 跌停鎖住
    p = ref
    if p is None:
        p = b or a or q.get("open") or q.get("prev")
    if p is None:
        return None
    if b and a and b <= a:
        p = min(max(p, b), a)
    if hi:
        p = min(p, hi)
    if lo:
        p = max(p, lo)
    return p


def resolve(q, old):
    """依這次的 MIS 報價 q 和上一次的快照 old，決定要顯示的價格。

    回傳 dict：price、est（是否為推估）、lt／lt_t／lt_v（今天最後一次確認的成交價、時間、當時累積量）、
    no_trade（今天尚未成交）、trial（試撮價）。回傳 None 代表這筆報價不更新價格（開盤前試撮）。
    """
    d, t = q.get("date"), q.get("time") or ""
    same = bool(d) and old.get("date") == d
    lt, lt_t, lt_v = (old.get("lt"), old.get("lt_t"), old.get("lt_v")) if same else (None, None, None)
    v, z = q.get("vol"), q.get("price")
    keep = {"lt": lt, "lt_t": lt_t, "lt_v": lt_v}

    if in_trial(t):
        trial = z if z is not None else q.get("trial")
        if t < OPEN_TIME:
            return None  # 開盤前：今天還沒有成交，保留昨天的收盤資料
        # 收盤前試撮：價格維持最後一筆連續交易的成交價，等 13:30 揭示收盤價
        if same and old.get("price") is not None:
            return {**keep, "price": old["price"], "est": bool(old.get("est")), "trial": trial,
                    "no_trade": bool(old.get("no_trade"))}
        if not v:
            return {**keep, "price": q.get("prev"), "est": False, "no_trade": True, "trial": trial}
        if lt is not None:
            return {**keep, "price": lt, "est": False, "trial": trial}
        return {**keep, "price": estimate(q, None), "est": True, "trial": trial}

    if z is not None:
        return {"price": z, "est": False, "lt": z, "lt_t": t, "lt_v": v}
    if not v:
        return {**keep, "price": q.get("prev"), "est": False, "no_trade": True}
    if lt is not None and lt_v is not None and v <= lt_v:
        return {**keep, "price": lt, "est": False}  # 之後沒有新的成交
    p = estimate(q, lt)
    if p is None:
        return None
    return {**keep, "price": p, "est": True}
