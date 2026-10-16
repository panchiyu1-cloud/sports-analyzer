"""One-off check of the owner's odds-api.io key (manual workflow probe-odds-api-io.yml).

What does the free key give: which two bookmakers are selected, the league slugs of the
competitions we follow, and one league's upcoming events with their odds (football and
MLB), plus whatever quota headers come back. About seven requests of the free 100 an hour.

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
# Words that pick out the competitions we follow from the league lists.
FOOTBALL_WANTED = {
    "PL": ("england", "premier"),
    "PD": ("spain", "liga"),
    "BL1": ("germany", "bundesliga"),
    "SA": ("italy", "serie-a"),
    "FL1": ("france", "ligue-1"),
    "CL": ("champions-league",),
}
QUOTA_WORDS = ("limit", "remain", "quota", "reset", "request", "used")
SAMPLE_CHARS = 6000


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


def league_slugs(leagues: Any, wanted: dict[str, tuple[str, ...]]) -> dict[str, list[str]]:
    """Slugs whose name or slug holds every word of a competition's key words."""
    out: dict[str, list[str]] = {code: [] for code in wanted}
    for item in leagues if isinstance(leagues, list) else []:
        text = f"{item.get('slug', '')} {item.get('name', '')}".lower().replace(" ", "-")
        for code, words in wanted.items():
            if all(w in text for w in words):
                out[code].append(str(item.get("slug") or item.get("name")))
    return out


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


def sample(value: Any) -> Any:
    """A readable slice of a large answer."""
    text = json.dumps(value, ensure_ascii=False)
    return value if len(text) <= SAMPLE_CHARS else text[:SAMPLE_CHARS] + " ...(cut)"


def probe_sport(sport: str, slug: str | None, books: list[str], key: str, log: list) -> dict:
    if not slug:
        return {"skipped": "no league slug found"}
    events = call("/events", key, log, sport=sport, league=slug, status="pending", limit=10)
    rows = events if isinstance(events, list) else []
    out: dict[str, Any] = {
        "league": slug,
        "events": [
            {k: e.get(k) for k in ("id", "home", "away", "date", "status") if k in e} for e in rows
        ],
        "event_keys": sorted(rows[0].keys()) if rows else [],
    }
    ids = [str(e.get("id")) for e in rows if e.get("id") is not None][:10]
    if ids and books:
        odds = call("/odds/multi", key, log, eventIds=",".join(ids), bookmakers=",".join(books))
        first = odds[0] if isinstance(odds, list) and odds else odds
        out["odds_events"] = len(odds) if isinstance(odds, list) else None
        out["odds_first"] = sample(first)
    return out


def main(out_path: Path) -> int:
    key = os.environ.get("ODDS_API_IO_KEY", "").strip()
    result: dict[str, Any] = {}
    log: list[dict[str, Any]] = []
    if not key:
        result["error"] = "ODDS_API_IO_KEY is not set (GitHub: Settings > Secrets > Actions)"
    else:
        books = selected_books(call("/bookmakers/selected", key, log))
        result["selected_bookmakers"] = books
        football = league_slugs(call("/leagues", key, log, sport="football"), FOOTBALL_WANTED)
        baseball = league_slugs(call("/leagues", key, log, sport="baseball"), {"MLB": ("mlb",)})
        result["league_slugs"] = {**football, **baseball}
        result["football"] = probe_sport(
            "football", (football.get("PL") or [None])[0], books, key, log
        )
        result["baseball"] = probe_sport(
            "baseball", (baseball.get("MLB") or [None])[0], books, key, log
        )
    result["requests"] = log
    text = json.dumps(result, ensure_ascii=False, indent=1)
    if key:
        text = text.replace(key, "***")  # never write the key, whatever echoed it
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text + "\n", encoding="utf-8")
    print(f"wrote {out_path}: {len(log)} requests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1] if len(sys.argv) > 1 else "probe/odds_api_io.json")))
