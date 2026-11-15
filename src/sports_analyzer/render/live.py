"""docs/live/data.json: what the live page (docs/live/, hand-written JavaScript) starts from.

The live page runs in the owner's browser and reads prices itself (ESPN's scoreboard,
運彩's feed where the browser may, The Odds API with the owner's own key). It cannot run
the pipeline, so every render hands it the pipeline's view of the matches still to play:

- the international consensus (fair probabilities, when they were bought),
- the last 運彩 prices the phone uploaded, with 最少串關數 (`legs`),
- the lines the pipeline translated (`derived`: the fair probability of a 運彩 line the
  consensus does not quote directly),
- the match-winner tape (for the curve), the thresholds and the name tables the page
  needs to recognise the same match in another source.

Nothing here is a secret and nothing comes from odds-api.io (its data may not be
redistributed): the consensus is The Odds API's, the 運彩 prices are the owner's uploads.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..config import FOOTBALL_COMPETITIONS, MLB_ODDS_KEY, NBA_ODDS_KEY
from ..fetch.taiwan import TW_ALIASES

if TYPE_CHECKING:
    from .site import Site

LIVE_DIR = "live"
DATA_FILE = "data.json"
# ESPN's scoreboard path of each competition (site.api.espn.com/apis/site/v2/sports/<path>/
# scoreboard): open to browsers, no key, with one bookmaker's prices on each event.
ESPN_PATHS = {
    "PL": "soccer/eng.1",
    "PD": "soccer/esp.1",
    "BL1": "soccer/ger.1",
    "SA": "soccer/ita.1",
    "FL1": "soccer/fra.1",
    "CL": "soccer/uefa.champions",
    "MLB": "baseball/mlb",
    "NBA": "basketball/nba",
}
# 運彩's feed id of each sport (blob3rd.../apidata/Pre/<id>-Games.zh.json). Basketball's
# is not known yet: the page looks for it and remembers it.
TW_FEED_IDS = {"football": "34740.1", "mlb": "34731.1", "nba": None}
MARKETS = (
    ("h2h", ("home", "draw", "away")),
    ("totals", ("over", "under")),
    ("spread", ("home", "away")),
)
# Tape points kept per match and source: enough for the curve, small enough to load fast.
MAX_POINTS = 40


def leagues() -> list[dict[str, Any]]:
    out = [
        {
            "code": c.code,
            "name": c.name,
            "sport": "football",
            "espn": ESPN_PATHS.get(c.code),
            "odds_key": c.odds_key,
        }
        for c in FOOTBALL_COMPETITIONS
    ]
    out.append(
        {
            "code": "MLB",
            "name": "MLB",
            "sport": "mlb",
            "espn": ESPN_PATHS["MLB"],
            "odds_key": MLB_ODDS_KEY,
        }
    )
    out.append(
        {
            "code": "NBA",
            "name": "NBA",
            "sport": "nba",
            "espn": ESPN_PATHS["NBA"],
            "odds_key": NBA_ODDS_KEY,
        }
    )
    return out


def fair_block(view: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The consensus without the bookmaker detail: probability per side, line, age."""
    if not view:
        return None
    out: dict[str, Any] = {"books": view.get("books")}
    found = False
    for market, sides in MARKETS:
        block = view.get(market)
        if not isinstance(block, Mapping):
            out[market] = None
            continue
        probs = {
            s: round(float(block[s]["fair"]), 4)
            for s in sides
            if isinstance(block.get(s), Mapping) and block[s].get("fair") is not None
        }
        if len(probs) < 2:
            out[market] = None
            continue
        found = True
        out[market] = {
            **probs,
            "line": block.get("line"),
            "at": block.get("odds_at"),
            "stale": bool(block.get("stale")),
        }
    return out if found else None


def tw_block(tw: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The last uploaded 運彩 prices of a card: the three direct markets, the translated
    lines with their fair probability, and 最少串關數 by market."""
    if not tw:
        return None
    out: dict[str, Any] = {
        "at": tw.get("captured_at"),
        "id": tw.get("id"),
        "no": tw.get("game_no"),
        "legs": dict(tw.get("legs") or {}),
    }
    for market, sides in MARKETS:
        block = tw.get(market)
        if not isinstance(block, Mapping):
            out[market] = None
            continue
        prices = {
            s: block[s]["price"]
            for s in sides
            if isinstance(block.get(s), Mapping) and block[s].get("price")
        }
        out[market] = {**prices, "line": block.get("line")} if prices else None
    out["derived"] = [
        {
            "market": d.get("market"),
            "label": d.get("selection"),
            "key": d.get("selection_key"),
            "line": d.get("line"),
            "price": d.get("tw_price"),
            "p": d.get("fair_prob"),
        }
        for d in tw.get("derived") or []
        if d.get("tw_price") and d.get("fair_prob") is not None
    ]
    return out


def tape_points(rows: list[Mapping[str, str]]) -> dict[str, list[list[Any]]]:
    """A match's match-winner tape as {"intl": [[time, {side: prob}], ...], "tw": [...]}
    (運彩's entry is its own de-vigged probability), oldest first, the newest MAX_POINTS."""
    series: dict[str, dict[str, dict[str, float]]] = {"intl": {}, "tw": {}}
    for r in rows:
        source = r.get("source")
        if r.get("market") != "h2h" or source not in series:
            continue
        try:
            prob = round(float(r.get("prob") or ""), 4)
        except ValueError:
            continue
        series[source].setdefault(str(r.get("observed_at")), {})[str(r.get("side"))] = prob
    return {
        source: [[t, sides] for t, sides in sorted(points.items())][-MAX_POINTS:]
        for source, points in series.items()
    }


def live_data(site: Site) -> dict[str, Any]:
    """The JSON the live page loads: matches not started, with everything known about
    their prices."""
    # Imported here: site.py imports this module to write the file.
    from ..moves import value_threshold
    from . import site as pages

    cfg = site.cfg
    by_match: dict[tuple[str, str], list[Mapping[str, str]]] = {}
    for row in site.tape:
        by_match.setdefault((str(row.get("sport")), str(row.get("match_id"))), []).append(row)

    matches = []
    for card in pages.upcoming(pages.all_cards(site), site.now):
        sport = str(card.get("sport") or ("mlb" if card.get("game_pk") else "football"))
        match_id = str(card.get("match_id") or card.get("game_pk") or card.get("game_id") or "")
        if not match_id:
            continue
        code = str(card.get("competition") or sport.upper())
        tw = card.get("tw") or {}
        matches.append(
            {
                "key": f"{sport}:{match_id}",
                "sport": sport,
                "id": match_id,
                "league": code,
                "league_name": pages.league(site, sport, card.get("competition"), match_id) or code,
                "kickoff": card.get("kickoff_utc"),
                "home": card.get("home"),
                "away": card.get("away"),
                "home_key": card.get("home_key"),
                "away_key": card.get("away_key"),
                "home_zh": tw.get("home_zh") or "",
                "away_zh": tw.get("away_zh") or "",
                "fair": fair_block(card.get("market")),
                "tw": tw_block(tw),
                "tape": tape_points(by_match.get((sport, match_id), [])),
                "page": f"{_page_of(sport, bool(tw))}#{pages.anchor(sport, match_id)}",
            }
        )

    budget = site.budget or {}
    return {
        "generated_at": site.now.astimezone(UTC).isoformat(timespec="seconds"),
        "cfg": {
            "threshold": {s: value_threshold(s, cfg) for s in ("football", "mlb", "nba")},
            "derived_threshold": cfg.derived_value_threshold,
            "gap_minutes": cfg.max_price_gap_minutes,
            "pregame_hours": cfg.pregame_window_hours,
            "nba_margin_sigma": cfg.nba_margin_sigma,
            "nba_total_sigma": cfg.nba_total_sigma,
        },
        # The pipeline's own count of The Odds API credits (the page shows it next to
        # what the owner's key reports when he buys prices himself).
        "budget": {k: budget.get(k) for k in ("remaining", "used", "period_start") if k in budget},
        "leagues": leagues(),
        "tw_feeds": TW_FEED_IDS,
        "matches": matches,
        # 運彩's Chinese team names -> our team keys, to recognise a match in 運彩's feed.
        "zh": dict(sorted(TW_ALIASES.items())),
    }


def _page_of(sport: str, on_tw: bool) -> str:
    """The existing page that analyses a match in full."""
    if sport == "nba":
        return "nba.html"
    if sport == "mlb":
        return "t10.html"
    return "value.html" if on_tw else "model.html"


def write_live(site: Site, docs: Path) -> Path:
    """Write docs/live/data.json (the page itself, index.html + engine.js + app.js, is
    hand-written and lives next to it)."""
    folder = docs / LIVE_DIR
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / DATA_FILE
    text = json.dumps(live_data(site), ensure_ascii=False, separators=(",", ":"))
    path.write_text(text + "\n", encoding="utf-8")
    return path


def parse_generated(value: Any) -> datetime | None:
    """`generated_at` back to a time (tests and the page's own check use it)."""
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None
