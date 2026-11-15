"""One-off probe (temporary branch): which public odds sources answer a browser on
panchiyu1-cloud.github.io (CORS headers), and what their payloads look like. Keys are
never printed: only status, CORS / quota headers and trimmed bodies of keyless APIs."""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path

ORIGIN = "https://panchiyu1-cloud.github.io"
OUT = Path("probe/out")
OUT.mkdir(parents=True, exist_ok=True)
KEEP = ("access-control-allow-origin", "access-control-expose-headers", "content-type",
        "x-requests-remaining", "x-requests-used", "cache-control", "x-ratelimit-remaining")
report: dict[str, dict] = {}


def get(name: str, url: str, save: bool = True, secret: bool = False, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"Origin": ORIGIN, "User-Agent": "Mozilla/5.0 probe", **(headers or {})})
    entry: dict = {"url": "(hidden: carries a key)" if secret else url}
    body = b""
    try:
        with urllib.request.urlopen(req, timeout=25, context=ssl.create_default_context()) as r:
            entry["status"] = r.status
            entry["headers"] = {k.lower(): v for k, v in r.headers.items() if k.lower() in KEEP}
            body = r.read()
    except urllib.error.HTTPError as e:
        entry["status"] = e.code
        entry["headers"] = {k.lower(): v for k, v in e.headers.items() if k.lower() in KEEP}
        body = e.read()[:300] if not secret else b""
    except Exception as e:  # noqa: BLE001
        entry["error"] = type(e).__name__
    entry["bytes"] = len(body)
    report[name] = entry
    data = None
    try:
        data = json.loads(body) if body else None
    except Exception:  # noqa: BLE001
        entry["not_json"] = body[:200].decode("utf-8", "replace") if not secret else True
    if save and data is not None and not secret:
        (OUT / f"{name}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1)[:400_000], encoding="utf-8")
    return data


def preflight(name: str, url: str, method: str = "GET", req_headers: str = "content-type"):
    req = urllib.request.Request(url, method="OPTIONS", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": method,
        "Access-Control-Request-Headers": req_headers, "User-Agent": "Mozilla/5.0 probe"})
    entry: dict = {}
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            entry["status"] = r.status
            entry["headers"] = {k.lower(): v for k, v in r.headers.items() if k.lower().startswith("access-control")}
    except urllib.error.HTTPError as e:
        entry["status"] = e.code
        entry["headers"] = {k.lower(): v for k, v in e.headers.items() if k.lower().startswith("access-control")}
    except Exception as e:  # noqa: BLE001
        entry["error"] = type(e).__name__
    report[name] = entry


now = datetime.now(UTC)
us = (now - timedelta(hours=5)).strftime("%Y%m%d")
nxt = (now + timedelta(days=1) - timedelta(hours=5)).strftime("%Y%m%d")
wk1 = (now + timedelta(days=5)).strftime("%Y%m%d")
wk2 = (now + timedelta(days=8)).strftime("%Y%m%d")

# ---- ESPN soccer, one date at a time
espn = "https://site.api.espn.com/apis/site/v2/sports"
for slug, days in (("eng.1", (5, 6, 7)), ("esp.1", (5, 6)), ("ger.1", (5, 6)), ("ita.1", (6, 7)), ("fra.1", (6,)),
                   ("uefa.champions", (9, 10)), ("uefa.nations", (0, 1, 2))):
    for dd in days:
        day = (now + timedelta(days=dd)).strftime("%Y%m%d")
        get(f"espn_{slug}_{day}", f"{espn}/soccer/{slug}/scoreboard?dates={day}")
get("espn_eng1_default", f"{espn}/soccer/eng.1/scoreboard")
(OUT / "_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({n: {"status": e.get("status"), "cors": (e.get("headers") or {}).get("access-control-allow-origin"), "bytes": e.get("bytes"), "error": e.get("error")} for n, e in report.items()}, indent=1))
