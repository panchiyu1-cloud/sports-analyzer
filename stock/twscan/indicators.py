"""技術指標（純 Python，皆為因果計算：第 i 天的值只用到第 i 天以前的資料）。"""
import math


def sma(x, n):
    out = [None] * len(x)
    s = 0.0
    for i, v in enumerate(x):
        s += v
        if i >= n:
            s -= x[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def ema(x, n):
    out = [None] * len(x)
    a = 2.0 / (n + 1)
    e = None
    for i, v in enumerate(x):
        if v is None:
            out[i] = e
            continue
        e = v if e is None else e + a * (v - e)
        out[i] = e
    return out


def macd(c, fast=12, slow=26, sig=9):
    ef, es = ema(c, fast), ema(c, slow)
    dif = [a - b for a, b in zip(ef, es)]
    dea = ema(dif, sig)
    osc = [a - b for a, b in zip(dif, dea)]
    return dif, dea, osc


def kd(h, l, c, n=9):
    """臺灣常用 KD(9,3,3)。"""
    K, D = [], []
    k = d = 50.0
    for i in range(len(c)):
        s = max(0, i - n + 1)
        lo, hi = min(l[s:i + 1]), max(h[s:i + 1])
        rsv = 50.0 if hi == lo else (c[i] - lo) / (hi - lo) * 100.0
        k = k * 2 / 3 + rsv / 3
        d = d * 2 / 3 + k / 3
        K.append(k)
        D.append(d)
    return K, D


def rsi(c, n=14):
    out = [None] * len(c)
    ag = al = 0.0
    for i in range(1, len(c)):
        ch = c[i] - c[i - 1]
        g, lo = max(ch, 0.0), max(-ch, 0.0)
        if i <= n:
            ag += g
            al += lo
            if i == n:
                ag /= n
                al /= n
                out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + lo) / n
            out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def bollinger(c, n=20, k=2.0):
    mid = sma(c, n)
    up, lo, bw = [None] * len(c), [None] * len(c), [None] * len(c)
    for i in range(n - 1, len(c)):
        m = mid[i]
        var = sum((x - m) ** 2 for x in c[i - n + 1:i + 1]) / n
        sd = math.sqrt(var)
        up[i], lo[i] = m + k * sd, m - k * sd
        bw[i] = (up[i] - lo[i]) / m if m else None
    return mid, up, lo, bw


def obv(c, v):
    out = [0.0] * len(c)
    for i in range(1, len(c)):
        out[i] = out[i - 1] + (v[i] if c[i] > c[i - 1] else -v[i] if c[i] < c[i - 1] else 0)
    return out


def dmi(h, l, c, n=14):
    """回傳 (+DI, -DI, ADX, ATR)，Wilder 平滑。"""
    L = len(c)
    pdi, mdi, adx, atr = [None] * L, [None] * L, [None] * L, [None] * L
    if L < n + 2:
        return pdi, mdi, adx, atr
    tr_s = pdm_s = mdm_s = 0.0
    dx_list = []
    adx_v = None
    for i in range(1, L):
        tr = max(h[i] - l[i], abs(h[i] - c[i - 1]), abs(l[i] - c[i - 1]))
        up, dn = h[i] - h[i - 1], l[i - 1] - l[i]
        pdm = up if (up > dn and up > 0) else 0.0
        mdm = dn if (dn > up and dn > 0) else 0.0
        if i <= n:
            tr_s += tr
            pdm_s += pdm
            mdm_s += mdm
            if i < n:
                continue
        else:
            tr_s = tr_s - tr_s / n + tr
            pdm_s = pdm_s - pdm_s / n + pdm
            mdm_s = mdm_s - mdm_s / n + mdm
        atr[i] = tr_s / n
        if tr_s <= 0:
            continue
        p, m = 100 * pdm_s / tr_s, 100 * mdm_s / tr_s
        pdi[i], mdi[i] = p, m
        dx = 0.0 if p + m == 0 else 100 * abs(p - m) / (p + m)
        if adx_v is None:
            dx_list.append(dx)
            if len(dx_list) == n:
                adx_v = sum(dx_list) / n
                adx[i] = adx_v
        else:
            adx_v = (adx_v * (n - 1) + dx) / n
            adx[i] = adx_v
    return pdi, mdi, adx, atr


def rolling_prev_max(x, n):
    """第 i 天之前 n 天（不含當天）的最大值。"""
    out = [None] * len(x)
    for i in range(n, len(x)):
        out[i] = max(x[i - n:i])
    return out


def rolling_prev_min(x, n):
    out = [None] * len(x)
    for i in range(n, len(x)):
        out[i] = min(x[i - n:i])
    return out


def pivots(h, l, k=3):
    """轉折高低點。第 i 根要等到第 i+k 根收盤後才確認，因此回傳 (i, 價格, 確認日 i+k)。"""
    highs, lows = [], []
    for i in range(k, len(h) - k):
        win_h = h[i - k:i + k + 1]
        if h[i] == max(win_h) and h[i] > max(h[i - k:i]):
            highs.append((i, h[i], i + k))
        win_l = l[i - k:i + k + 1]
        if l[i] == min(win_l) and l[i] < min(l[i - k:i]):
            lows.append((i, l[i], i + k))
    return highs, lows
