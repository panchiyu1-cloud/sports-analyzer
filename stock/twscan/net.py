"""HTTP 抓取：只用 Python 標準函式庫。各網站分別限速，避免被交易所暫時封鎖 IP。"""
import gzip
import http.cookiejar
import json
import ssl
import threading
import time
import urllib.error
import urllib.request
import zlib
from urllib.parse import urlparse

from .util import log, now_tw

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# 每個網站兩次請求之間至少間隔幾秒（證交所太頻繁會被暫停連線）
DEFAULT_INTERVALS = {
    "www.twse.com.tw": 2.6,
    "mis.twse.com.tw": 1.5,
    "openapi.twse.com.tw": 0.6,
    "isin.twse.com.tw": 1.0,
    "www.tpex.org.tw": 1.2,
    "query1.finance.yahoo.com": 0.25,
    "query2.finance.yahoo.com": 0.25,
    "news.google.com": 1.0,
    "api.cnyes.com": 1.0,
}

# 這些是公開行情資料網站；若電腦缺少憑證鏈（常見於 macOS 的 python.org 版本）
# 才會在驗證失敗後退回不驗證憑證的連線，只用於讀取公開資料。
PUBLIC_DATA_HOSTS = (
    "twse.com.tw", "tpex.org.tw", "finance.yahoo.com", "news.google.com", "cnyes.com",
)


class FetchError(Exception):
    pass


def _strict_off_context():
    ctx = ssl.create_default_context()
    # Python 3.13 起預設開啟 VERIFY_X509_STRICT，證交所憑證缺少 Subject Key Identifier 會被拒絕。
    if hasattr(ssl, "VERIFY_X509_STRICT"):
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
    try:
        import certifi  # 有安裝就用，沒有也沒關係
        ctx.load_verify_locations(certifi.where())
    except Exception:
        pass
    return ctx


class Http:
    def __init__(self, intervals=None, insecure_fallback=True):
        self.intervals = dict(DEFAULT_INTERVALS)
        if intervals:
            self.intervals.update(intervals)
        self.insecure_fallback = insecure_fallback
        self.cj = http.cookiejar.CookieJar()
        self._ctx = _strict_off_context()
        self._insecure = ssl._create_unverified_context()
        self._insecure_hosts = set()
        self._host_locks = {}
        self._last = {}
        self._glock = threading.Lock()
        self.stats = {}  # host -> {ok, fail, last_ok, last_error}

    # ------------------------------------------------------------------
    def _throttle(self, host):
        with self._glock:
            lock = self._host_locks.setdefault(host, threading.Lock())
        interval = self.intervals.get(host, 0.3)
        with lock:
            wait = self._last.get(host, 0) + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last[host] = time.monotonic()

    def _record(self, host, ok, err=None):
        with self._glock:
            s = self.stats.setdefault(host, {"ok": 0, "fail": 0, "last_ok": None, "last_error": None})
            if ok:
                s["ok"] += 1
                s["last_ok"] = now_tw().strftime("%m/%d %H:%M:%S")
            else:
                s["fail"] += 1
                s["last_error"] = f"{now_tw():%m/%d %H:%M:%S} {err}"[:300]

    def _open(self, req, host, timeout):
        ctx = self._insecure if host in self._insecure_hosts else self._ctx
        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            urllib.request.HTTPSHandler(context=ctx),
        )
        return opener.open(req, timeout=timeout)

    def get(self, url, timeout=25, headers=None, retries=2):
        host = urlparse(url).hostname or ""
        h = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, text/html, application/xml, */*",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.6",
            "Accept-Encoding": "gzip, deflate",
        }
        if headers:
            h.update(headers)
        last_err = None
        for attempt in range(retries + 1):
            self._throttle(host)
            req = urllib.request.Request(url, headers=h)
            try:
                with self._open(req, host, timeout) as resp:
                    raw = resp.read()
                    enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip" or raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)
                elif enc == "deflate":
                    try:
                        raw = zlib.decompress(raw)
                    except zlib.error:
                        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                self._record(host, True)
                return raw
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code in (429, 500, 502, 503, 504):
                    time.sleep(2.0 * (attempt + 1) + (8 if e.code == 429 else 0))
                    continue
                break
            except (ssl.SSLError, urllib.error.URLError) as e:
                reason = getattr(e, "reason", e)
                last_err = f"{type(e).__name__}: {reason}"
                is_ssl = isinstance(e, ssl.SSLError) or isinstance(reason, ssl.SSLError) or "CERTIFICATE" in str(reason).upper()
                if (is_ssl and self.insecure_fallback and host not in self._insecure_hosts
                        and any(host.endswith(d) for d in PUBLIC_DATA_HOSTS)):
                    log(f"[注意] {host} 憑證驗證失敗，改以不驗證憑證方式讀取公開資料。")
                    self._insecure_hosts.add(host)
                    continue
                time.sleep(1.5 * (attempt + 1))
            except (TimeoutError, OSError) as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(1.5 * (attempt + 1))
        self._record(host, False, last_err)
        raise FetchError(f"{host}: {last_err}")

    def get_text(self, url, encoding="utf-8", **kw):
        raw = self.get(url, **kw)
        for enc in ([encoding] if isinstance(encoding, str) else list(encoding)):
            try:
                return raw.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return raw.decode("utf-8", errors="replace")

    def get_json(self, url, **kw):
        raw = self.get(url, **kw)
        txt = raw.decode("utf-8-sig", errors="replace").strip()
        try:
            return json.loads(txt)
        except ValueError as e:
            host = urlparse(url).hostname or ""
            snippet = txt[:80].replace("\n", " ")
            self._record(host, False, f"非 JSON 回應：{snippet}")
            raise FetchError(f"{host}: 非 JSON 回應（可能被暫時限制）: {snippet}") from e
