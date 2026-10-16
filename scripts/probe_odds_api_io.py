"""One-off check of the owner's odds-api.io key (manual workflow probe-odds-api-io.yml).

What does the free key give: which two bookmakers are selected and, for every competition
we follow (五大聯賽, 歐冠, MLB), the upcoming events and the main odds of up to ten of them,
with each price's update time and the quota headers. About 15 of the free 100 requests an
hour.

The key comes only from ODDS_API_IO_KEY and is scrubbed from everything written; errors
are recorded by type, never with the request URL (it carries the key).

    python scripts/probe_odds_api_io.py probe/odds_api_io.json
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import requests

BASE = "https://api.odds-api.io/v3"
# The competitions we follow, by their odds-api.io slugs (found by the first probe run).
LEAGUES = {
    "PL": ("football", "england-premier-league"),
    "PD": ("football", "spain-laliga"),
    "BL1": ("football", "germany-bundesliga"),
    "SA": ("football", "italy-serie-a"),
    "FL1": ("football", "france-ligue-1"),
    "CL": ("football", "international-clubs-uefa-champions-league"),
    "MLB": ("baseball", "usa-mlb-playoffs"),
}
# The markets we compare with 運彩 (moneyline / 1X2, main totals, main handicap).
MARKETS = ("ML", "Totals", "Goals Over/Under", "Spread")
QUOTA_WORDS = ("limit", "remain", "quota", "reset", "request", "used")


def call(path: str, key: str, log: list[dict[str, Any]], **params: Any) -> Any:
    """GET one endpoint; record status, size and quota headers (never the URL)."""
    entry: dict[str, Any] = {"path": path, "params": {k: v for k, v in params.items()}}
    try:
        resp = requests.get(f"{BASE}{path}", params={"apiKey": key, **params}, timeout=30)
    except requests.RequestException as exc:
        entry["error"] = type(exc).__name__
        log.append(entry)
        return None
    entry["status"] = resp.status_code
    entry["bytes"] = len(resp.content)
    entry["quota_headers"] = {
        k: v for k, v in resp.headers.items() if any(w in k.lower() for w in QUOTA_WORDS)
    }
    try:
        body = resp.json()
    except ValueError:
        body = None
        entry["text"] = resp.text[:500]
    if resp.status_code != 200:
        entry["body"] = body if body is not None else entry.get("text")
        body = None
    log.append(entry)
    return body


def selected_books(raw: Any) -> list[str]:
    """The selected bookmakers, whatever shape the endpoint answers in."""
    if isinstance(raw, list):
        return [str(b.get("name", b)) if isinstance(b, dict) else str(b) for b in raw]
    if isinstance(raw, dict):
        for value in raw.values():
            if isinstance(value, list):
                return selected_books(value)
        return [str(k) for k, v in raw.items() if v]
    return []


def compact(event: dict[str, Any]) -> dict[str, Any]:
    """One event's main markets per bookmaker: the first line of each, with its time."""
    books = {}
    for book, markets in (event.get("bookmakers") or {}).items():
        books[book] = {
            m.get("name"): {"at": m.get("updatedAt"), "odds": (m.get("odds") or [None])[0]}
            for m in markets
            if m.get("name") in MARKETS
        }
    return {
        "id": event.get("id"),
        "home": event.get("home"),
        "away": event.get("away"),
        "date": event.get("date"),
        "books": books,
    }


def probe_league(sport: str, slug: str, books: list[str], key: str, log: list) -> dict:
    """Upcoming events of one league and the main odds of the first ten."""
    events = call("/events", key, log, sport=sport, league=slug, status="pending", limit=20)
    rows = events if isinstance(events, list) else []
    ids = [str(e.get("id")) for e in rows if e.get("id") is not None][:10]
    out: dict[str, Any] = {"pending": len(rows), "asked": len(ids)}
    if ids and books:
        odds = call("/odds/multi", key, log, eventIds=",".join(ids), bookmakers=",".join(books))
        found = odds if isinstance(odds, list) else []
        out["with_odds"] = len(found)
        out["events"] = [compact(e) for e in found]
    return out


def check_the_odds_api() -> dict[str, Any]:
    """Is the main key (ODDS_API_KEY, The Odds API) still the one the pipeline needs? The
    sports list is free of quota. Only the-odds-api.com ever sees that key."""
    key = os.environ.get("THE_ODDS_API_KEY", "").strip()
    if not key:
        return {"status": "not set"}
    try:
        resp = requests.get(
            "https://api.the-odds-api.com/v4/sports", params={"apiKey": key}, timeout=30
        )
    except requests.RequestException as exc:
        return {"error": type(exc).__name__}
    return {
        "status": resp.status_code,
        "remaining": resp.headers.get("x-requests-remaining"),
        "used": resp.headers.get("x-requests-used"),
    }


def main(out_path: Path) -> int:
    key = os.environ.get("ODDS_API_IO_KEY", "").strip()
    result: dict[str, Any] = {
        "key_source": os.environ.get("KEY_SOURCE", ""),
        "the_odds_api": check_the_odds_api(),
    }
    log: list[dict[str, Any]] = []
    if not key:
        result["error"] = "ODDS_API_IO_KEY is not set (GitHub: Settings > Secrets > Actions)"
    else:
        books = selected_books(call("/bookmakers/selected", key, log))
        result["selected_bookmakers"] = books
        result["leagues"] = {
            code: {"slug": slug, **probe_league(sport, slug, books, key, log)}
            for code, (sport, slug) in LEAGUES.items()
        }
    result["requests"] = log
    text = json.dumps(result, ensure_ascii=False, indent=1)
    for secret in (key, os.environ.get("THE_ODDS_API_KEY", "").strip()):
        if secret:
            text = text.replace(secret, "***")  # never write a key, whatever echoed it
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {out_path}: {len(log)} requests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "probe/odds_api_io.json")))
