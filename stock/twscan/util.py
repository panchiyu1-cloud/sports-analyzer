"""共用工具：台北時間、數字解析、民國日期、JSON 讀寫。"""
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))  # 臺灣沒有日光節約時間，固定 UTC+8

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")


def now_tw():
    return datetime.now(TW)


def today_iso():
    return now_tw().date().isoformat()


def market_phase(now=None):
    """回傳 'open'（09:00–13:30）、'preopen'（08:30–09:00 試撮）、'post'（13:30–14:30）或 'closed'。"""
    now = now or now_tw()
    if now.weekday() >= 5:
        return "closed"
    hm = now.hour * 60 + now.minute
    if 8 * 60 + 30 <= hm < 9 * 60:
        return "preopen"
    if 9 * 60 <= hm <= 13 * 60 + 31:
        return "open"
    if 13 * 60 + 31 < hm <= 14 * 60 + 40:
        return "post"
    return "closed"


_TAG = re.compile(r"<[^>]+>")
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")
_EMPTY = {"", "-", "--", "---", "----", "X", "N/A", "NA", "null", "None"}


def num(s):
    """把交易所各種字串（含千分位、HTML 標記、'--'、'X0.00'）轉成 float；無法解析回傳 None。"""
    if s is None:
        return None
    if isinstance(s, bool):
        return None
    if isinstance(s, (int, float)):
        if isinstance(s, float) and (math.isnan(s) or math.isinf(s)):
            return None
        return float(s)
    t = _TAG.sub("", str(s)).replace(",", "").replace("　", "").replace(" ", "").strip()
    if t in _EMPTY:
        return None
    if t.startswith("X"):
        t = t[1:]
    try:
        return float(t)
    except ValueError:
        m = _NUM.search(t)
        return float(m.group()) if m else None


def sign_from_html(s):
    """證交所 MI_INDEX 的漲跌欄位是 <p style=color:red>+</p> 這種格式。"""
    t = _TAG.sub("", str(s or "")).strip()
    if "-" in t:
        return -1
    return 1


def to_iso_date(s):
    """支援 '115/11/06'、'1151106'、'2026/11/06'、'20261106'、'2026-11-06'。"""
    if s is None:
        return None
    t = str(s).strip()
    m = re.match(r"^(\d{4})[/-]?(\d{1,2})[/-]?(\d{1,2})$", t)
    if m and int(m.group(1)) > 1911:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{y:04d}-{mo:02d}-{d:02d}"
    m = re.match(r"^(\d{2,3})/(\d{1,2})/(\d{1,2})$", t) or re.match(r"^(\d{2,3})(\d{2})(\d{2})$", t)
    if m:
        y = int(m.group(1)) + 1911
        return f"{y:04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def iso_to_roc(iso):
    y, m, d = iso.split("-")
    return f"{int(y) - 1911}/{m}/{d}"


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass


def r2(x, nd=2):
    if x is None:
        return None
    try:
        if math.isnan(x) or math.isinf(x):
            return None
    except TypeError:
        return None
    return round(x, nd)


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def log(msg):
    line = f"[{now_tw():%H:%M:%S}] {msg}"
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"), flush=True)
