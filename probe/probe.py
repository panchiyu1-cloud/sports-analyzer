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

# ---- ESPN (keyless)
espn = "https://site.api.espn.com/apis/site/v2/sports"
nba = get("espn_nba_today", f"{espn}/basketball/nba/scoreboard?dates={us}")
get("espn_nba_tomorrow", f"{espn}/basketball/nba/scoreboard?dates={nxt}")
get("espn_epl_next", f"{espn}/soccer/eng.1/scoreboard?dates={wk1}-{wk2}")
get("espn_laliga_next", f"{espn}/soccer/esp.1/scoreboard?dates={wk1}-{wk2}")
get("espn_nations", f"{espn}/soccer/uefa.nations/scoreboard?dates={us}-{wk1}")
get("espn_ucl_next", f"{espn}/soccer/uefa.champions/scoreboard?dates={wk1}-{(now + timedelta(days=12)).strftime('%Y%m%d')}")
try:
    pre = [e for e in (nba or {}).get("events", []) if e["competitions"][0]["status"]["type"]["state"] == "pre"]
    ev = (pre or (nba or {}).get("events", []))[0]
    eid = ev["id"]
    core = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"
    get("espn_core_odds", f"{core}/events/{eid}/competitions/{eid}/odds?limit=20")
    get("espn_summary", f"{espn}/basketball/nba/summary?event={eid}")
except Exception as e:  # noqa: BLE001
    report["espn_core_odds"] = {"error": type(e).__name__}

# ---- Polymarket (keyless)
gamma = "https://gamma-api.polymarket.com"
get("poly_sports", f"{gamma}/sports")
for tag in ("nba", "epl", "soccer", "la-liga", "champions-league"):
    q = urllib.parse.urlencode({"tag_slug": tag, "closed": "false", "active": "true", "limit": 12, "order": "startDate", "ascending": "false"})
    get(f"poly_events_{tag}", f"{gamma}/events?{q}")
try:
    sports = json.loads((OUT / "poly_sports.json").read_text())
    nba_s = next(s for s in sports if s.get("sport") == "nba")
    series = str(nba_s.get("series"))
    q = urllib.parse.urlencode({"series_id": series, "closed": "false", "active": "true", "limit": 8, "order": "startDate", "ascending": "true", "tag_id": 100639})
    games = get("poly_nba_games", f"{gamma}/events?{q}")
    q2 = urllib.parse.urlencode({"series_id": series, "closed": "false", "limit": 8})
    get("poly_nba_series_events", f"{gamma}/events?{q2}")
    tok = None
    for ev in games or []:
        for m in ev.get("markets", []):
            ids = json.loads(m.get("clobTokenIds") or "[]")
            if ids:
                tok = ids[0]
                break
        if tok:
            break
    if tok:
        clob = "https://clob.polymarket.com"
        get("poly_book", f"{clob}/book?token_id={tok}")
        get("poly_midpoint", f"{clob}/midpoint?token_id={tok}")
        get("poly_history", f"{clob}/prices-history?market={tok}&interval=1d&fidelity=10")
        preflight("poly_prices_preflight", f"{clob}/prices", "POST")
except Exception as e:  # noqa: BLE001
    report["poly_nba_games"] = {"error": f"{type(e).__name__}"}

# ---- Kalshi (keyless market data)
kal = "https://api.elections.kalshi.com/trade-api/v2"
get("kalshi_nba", f"{kal}/events?series_ticker=KXNBAGAME&status=open&limit=4&with_nested_markets=true")
get("kalshi_epl", f"{kal}/events?series_ticker=KXEPLGAME&status=open&limit=4&with_nested_markets=true")

# ---- The Odds API and odds-api.io: CORS headers only (free endpoints), nothing saved
k = os.environ.get("ODDS_API_KEY")
if k:
    get("theoddsapi_sports", f"https://api.the-odds-api.com/v4/sports/?apiKey={k}", save=False, secret=True)
    get("theoddsapi_events", f"https://api.the-odds-api.com/v4/sports/basketball_nba/events?apiKey={k}", save=False, secret=True)
k2 = os.environ.get("ODDS_API_IO_KEY")
if k2:
    get("oddsapiio_sports", f"https://api.odds-api.io/v3/sports?apiKey={k2}", save=False, secret=True)

# ---- 運彩 from a runner (expected: blocked by country)
get("tw_lottery_football", "https://blob3rd.sportslottery.com.tw/apidata/Pre/34740.1-Games.zh.json", save=False)

(OUT / "_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
print(json.dumps({n: {"status": e.get("status"), "cors": (e.get("headers") or {}).get("access-control-allow-origin"), "bytes": e.get("bytes"), "error": e.get("error")} for n, e in report.items()}, indent=1))
