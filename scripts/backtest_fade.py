"""反者道之動: which betting strategies do worst against the market, and does betting the
exact opposite pay? Writes data/fade_summary.json (shown on docs/fade.html).

    python scripts/backtest_fade.py [--model-records records.json]

Data (not on main; see CLAUDE.md "反者道之動"):
- data/raw/mlb_odds_2021_2025.csv.gz: MLB 2021-2025 closing and opening odds of six US
  books (`t10-data` branch), filtered with t10_backtest.trusted_books (in-play closes);
- data/raw/football_odds.csv.gz: 22 leagues 2019/20-2026/27, early and closing odds
  (`odds-history` branch);
- the system's own Dixon-Coles predictions, walk-forward on data/history (the same
  stage 1 as scripts/backtest_football.py; ~10 minutes, or reuse --model-records).

Every strategy is scored twice: its own picks, and the opposite bet on the same games
(the other side of a two-way market; in 1X2 the other two outcomes, dutched). Hit rates
are compared with the closing market's fair probability (Pinnacle where available,
power de-vig); ROI at fair prices (no margin), at the international price, at the best
price across books, and at 運彩's margin. The data is split in two halves by season so
a strategy that is only bad in one half is not mistaken for a rule.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import importlib.util
import json
import statistics
import sys
from collections import defaultdict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sports_analyzer.analysis.fade import TW_MARGIN, dutch, score, tw_prices  # noqa: E402
from sports_analyzer.analysis.t10_backtest import (  # noqa: E402
    american_to_decimal,
    game_g,
    outcome,
    trusted_books,
)
from sports_analyzer.config import DATA_DIR, MODEL  # noqa: E402
from sports_analyzer.models.market import devig  # noqa: E402

MLB_PATH = DATA_DIR / "raw" / "mlb_odds_2021_2025.csv.gz"
FOOTBALL_PATH = DATA_DIR / "raw" / "football_odds.csv.gz"
OUT_PATH = DATA_DIR / "fade_summary.json"

BOOK_ORDER = ("draftkings", "fanduel", "caesars", "bet365", "betmgm", "bet_rivers_ny")
MLB_GAME_TYPES = {"R", "F", "D", "L", "W"}
MLB_POPULAR = {
    "New York Yankees",
    "Los Angeles Dodgers",
    "Boston Red Sox",
    "Chicago Cubs",
    "New York Mets",
}
BIG_CLUBS = {
    "Man United", "Liverpool", "Arsenal", "Chelsea", "Man City", "Tottenham",
    "Real Madrid", "Barcelona", "Ath Madrid", "Bayern Munich", "Dortmund",
    "Juventus", "Inter", "Milan", "Paris SG", "Ajax", "Benfica", "Porto",
    "Celtic", "Rangers", "Galatasaray", "Fenerbahce",
}  # fmt: skip
LOWER_DIVISIONS = {"E2", "E3", "EC", "SC1", "SC2", "SC3"}
SIDES = ("H", "D", "A")
MLB_LEVELS = ("intl", "best", "tw")
FOOTBALL_LEVELS = ("intl", "best", "tw")


# ============================================================================ MLB
def _home_prob(home: Any, away: Any) -> float | None:
    h, a = american_to_decimal(home), american_to_decimal(away)
    if h is None or a is None or h <= 1 or a <= 1:
        return None
    return devig([h, a], "power")[0]


def load_mlb(path: Path) -> list[dict[str, Any]]:
    """One record per verified game: the preferred trusted book's prices at the close
    (and open), the best close across trusted books, totals, teams and score."""
    by_game: dict[tuple, list[dict[str, str]]] = defaultdict(list)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["game_type"] not in MLB_GAME_TYPES or not row["status"].startswith("Final"):
                continue
            if row["home_score"] == "" or row["away_score"] == "":
                continue
            by_game[(row["start_utc"], row["away"], row["home"])].append(row)
    rank = {b: i for i, b in enumerate(BOOK_ORDER)}
    games = []
    for (start, away, home), rows in by_game.items():
        close_p = {r["book"]: _home_prob(r["ml_close_home"], r["ml_close_away"]) for r in rows}
        close_p = {b: p for b, p in close_p.items() if p is not None}
        open_p = {r["book"]: _home_prob(r["ml_open_home"], r["ml_open_away"]) for r in rows}
        open_p = {b: p for b, p in open_p.items() if p is not None}
        anchor = statistics.median(open_p.values()) if len(open_p) >= 2 else None
        ok = trusted_books(close_p, anchor)
        if not ok:
            continue
        rows = sorted((r for r in rows if r["book"] in ok), key=lambda r: rank.get(r["book"], 99))
        priced = []
        for r in rows:
            g = game_g(
                r["ml_close_home"],
                r["ml_close_away"],
                r["rl_close_home_spread"],
                r["rl_close_home"],
                r["rl_close_away"],
            )
            if g:
                priced.append((r, g))
        if not priced:
            continue
        fav0 = priced[0][1].favourite
        rl_ok = trusted_books(
            {r["book"]: g.c for r, g in priced if g.favourite == fav0}, tol=0.05, min_books=1
        )
        priced = [(r, g) for r, g in priced if r["book"] in rl_ok]
        if not priced:
            continue
        r, close = priced[0]
        opn = game_g(
            r["ml_open_home"],
            r["ml_open_away"],
            r["rl_open_home_spread"],
            r["rl_open_home"],
            r["rl_open_away"],
        )
        if r["book"] not in trusted_books(open_p, min_books=2) or (
            opn and opn.favourite != close.favourite
        ):
            opn = None
        hs, as_ = int(float(r["home_score"])), int(float(r["away_score"]))
        same = [g for _, g in priced if g.favourite == close.favourite]
        ml = tw_prices([close.p, 1 - close.p], TW_MARGIN["mlb"], even=True)
        rl = tw_prices([close.c, 1 - close.c], TW_MARGIN["mlb"], even=True)
        tot = _mlb_totals(r, priced)
        games.append(
            {
                "start": start,
                "season": start[:4],
                "home": home,
                "away": away,
                "g": close,
                "g_open": opn,
                "res": outcome(hs, as_, close.favourite),
                "scores": (hs, as_),
                "best": {
                    "fav_ml": max(g.ml_fav for g in same),
                    "dog_ml": max(g.ml_dog for g in same),
                    "fav_rl": max(g.rl_fav for g in same),
                    "dog_rl": max(g.rl_dog for g in same),
                },
                "tot": tot,
                # 運彩's prices for the same fair probabilities (its margin, its spread)
                "tw": {
                    "fav_ml": ml[0],
                    "dog_ml": ml[1],
                    "fav_rl": rl[0],
                    "dog_rl": rl[1],
                    **(
                        dict(
                            zip(
                                ("over", "under"),
                                tw_prices(
                                    [tot["p_over"], 1 - tot["p_over"]], TW_MARGIN["mlb"], even=True
                                ),
                                strict=True,
                            )
                        )
                        if tot
                        else {}
                    ),
                },
            }
        )
    games.sort(key=lambda x: x["start"])
    return games


def _mlb_totals(r: dict[str, str], priced: list) -> dict[str, Any] | None:
    try:
        line = float(r["tot_close_line"])
    except (TypeError, ValueError):
        return None
    o, u = american_to_decimal(r["tot_close_over"]), american_to_decimal(r["tot_close_under"])
    if not o or not u or o <= 1 or u <= 1:
        return None
    bests = []
    for rr, _ in priced:
        try:
            if float(rr["tot_close_line"]) != line:
                continue
        except (TypeError, ValueError):
            continue
        bo, bu = (
            american_to_decimal(rr["tot_close_over"]),
            american_to_decimal(rr["tot_close_under"]),
        )
        if bo and bu and bo > 1 and bu > 1:
            bests.append((bo, bu))
    return {
        "line": line,
        "over": o,
        "under": u,
        "p_over": devig([o, u], "power")[0],
        "best_over": max(b[0] for b in bests) if bests else o,
        "best_under": max(b[1] for b in bests) if bests else u,
    }


MLB_OPPOSITE = {
    "fav_ml": "dog_ml",
    "dog_ml": "fav_ml",
    "fav_rl": "dog_rl",
    "dog_rl": "fav_rl",
    "over": "under",
    "under": "over",
}


def mlb_bet(x: dict[str, Any], side: str) -> dict[str, float] | None:
    """One side of one game: hit, the closing market's fair probability, and prices.
    None on a totals push (stake back: left out) or a missing total."""
    g, res = x["g"], x["res"]
    if side in ("over", "under"):
        t = x["tot"]
        if not t:
            return None
        total = sum(x["scores"])
        if total == t["line"]:
            return None
        prob = t["p_over"] if side == "over" else 1 - t["p_over"]
        return {
            "hit": float((total > t["line"]) == (side == "over")),
            "prob": prob,
            "intl": t[side],
            "best": t["best_" + side],
            "tw": x["tw"][side],
        }
    hit, prob, price = {
        "fav_ml": (res.fav_win, g.p, g.ml_fav),
        "dog_ml": (not res.fav_win, 1 - g.p, g.ml_dog),
        "fav_rl": (res.fav_cover, g.c, g.rl_fav),
        "dog_rl": (res.dog_cover, 1 - g.c, g.rl_dog),
    }[side]
    return {
        "hit": float(hit),
        "prob": prob,
        "intl": price,
        "best": x["best"][side],
        "tw": x["tw"][side],
    }


def mlb_form(games: list[dict[str, Any]]) -> dict[tuple, dict[str, Any]]:
    """Each team's state before every game, season by season."""
    hist: dict[tuple, list[tuple[str, int, int, str]]] = defaultdict(list)
    out = {}
    for x in games:
        hs, as_ = x["scores"]
        for side, team, opp in (("home", x["home"], x["away"]), ("away", x["away"], x["home"])):
            h = hist[(x["season"], team)]
            res = [y[0] for y in h]
            last = h[-1] if h else None
            out[(x["start"], side)] = {
                "win_run": _run(res, "W"),
                "loss_run": _run(res, "L"),
                "last_rf": last[1] if last else None,
                "last_margin": last[1] - last[2] if last else None,
                "series_won": bool(last and last[3] == opp and last[0] == "W"),
                "pct": res.count("W") / len(res) if len(res) >= 30 else None,
            }
        hist[(x["season"], x["home"])].append(("W" if hs > as_ else "L", hs, as_, x["away"]))
        hist[(x["season"], x["away"])].append(("W" if as_ > hs else "L", as_, hs, x["home"]))
    return out


def _run(results: list[str], want: str) -> int:
    n = 0
    for r in reversed(results):
        if r != want:
            break
        n += 1
    return n


def mlb_strategies(games: list[dict[str, Any]]) -> list[tuple[str, str, str, Callable]]:
    """(key, group, name, pick) - pick(game) returns the side bet or None."""
    form = mlb_form(games)

    def f(x: dict[str, Any], side: str) -> dict[str, Any]:
        return form[(x["start"], side)]

    def team(x: dict[str, Any], side: str) -> str:
        return "fav_ml" if x["g"].favourite == side else "dog_ml"

    def one(x: dict[str, Any], home: bool, away: bool) -> str | None:
        return None if home == away else team(x, "home" if home else "away")

    def home_p(x: dict[str, Any]) -> float:
        return x["g"].p if x["g"].favourite == "home" else 1 - x["g"].p

    def steam(x: dict[str, Any], sign: int) -> str | None:
        o = x["g_open"]
        if not o:
            return None
        move = x["g"].p - o.p
        if sign > 0 and move >= 0.04:
            return "fav_ml"
        if sign < 0 and move <= -0.04:
            return "dog_ml"
        return None

    def streak(x: dict[str, Any], key: str, n: int) -> str | None:
        return one(x, f(x, "home")[key] >= n, f(x, "away")[key] >= n)

    return [
        ("t10_g6", "mine", "T-10：G≥+6 押強隊不讓分（你的原始規則）",
         lambda x: "fav_ml" if x["g"].g >= 0.06 else None),
        ("t10_gm6", "mine", "T-10：G≤−6 押弱隊 +1.5（你的原始規則）",
         lambda x: "dog_rl" if x["g"].g <= -0.06 else None),
        ("mlb_steam_fav", "mine", "熱門被國際盤買進 4pp↑ 還去追（大單）", lambda x: steam(x, 1)),
        ("mlb_steam_dog", "mine", "冷門被國際盤買進 4pp↑ 還去追（大單）", lambda x: steam(x, -1)),
        ("mlb_fav", "public", "每場都押熱門不讓分", lambda x: "fav_ml"),
        ("mlb_home", "public", "每場都押主隊不讓分", lambda x: team(x, "home")),
        ("home_fav55", "public", "押主場熱門（勝率≥55%）",
         lambda x: "fav_ml" if x["g"].favourite == "home" and home_p(x) >= 0.55 else None),
        ("mlb_bigfav", "public", "押大熱門（勝率≥65%）不讓分",
         lambda x: "fav_ml" if x["g"].p >= 0.65 else None),
        ("mlb_closefav", "public", "勢均力敵（勝率<55%）押熱門",
         lambda x: "fav_ml" if x["g"].p < 0.55 else None),
        ("mlb_away_bigfav", "public", "押客場大熱門（勝率≥60%）",
         lambda x: "fav_ml" if x["g"].favourite == "away" and x["g"].p >= 0.60 else None),
        ("mlb_home_dog", "public", "押主場冷門",
         lambda x: "dog_ml" if x["g"].favourite == "away" else None),
        ("mlb_fav_rl", "public", "每場都押熱門 −1.5", lambda x: "fav_rl"),
        ("mlb_bigfav_rl", "public", "大熱門（≥65%）押 −1.5",
         lambda x: "fav_rl" if x["g"].p >= 0.65 else None),
        ("mlb_over", "public", "每場都押大分", lambda x: "over"),
        ("mlb_high_over", "public", "高總分線（≥9.5）押大",
         lambda x: "over" if x["tot"] and x["tot"]["line"] >= 9.5 else None),
        ("mlb_low_under", "public", "低總分線（≤7）押小",
         lambda x: "under" if x["tot"] and x["tot"]["line"] <= 7 else None),
        ("mlb_popular", "public", "押人氣球隊（洋基、道奇、紅襪、小熊、大都會）",
         lambda x: one(x, x["home"] in MLB_POPULAR, x["away"] in MLB_POPULAR)),
        ("mlb_popular_away", "public", "人氣球隊當客隊",
         lambda x: team(x, "away")
         if x["away"] in MLB_POPULAR and x["home"] not in MLB_POPULAR else None),
        ("mlb_win3", "public", "押三連勝的球隊", lambda x: streak(x, "win_run", 3)),
        ("mlb_loss3", "public", "押三連敗的球隊", lambda x: streak(x, "loss_run", 3)),
        ("mlb_win5", "public", "押五連勝的球隊", lambda x: streak(x, "win_run", 5)),
        ("mlb_loss5", "public", "押五連敗的球隊", lambda x: streak(x, "loss_run", 5)),
        ("mlb_runs10", "public", "押上一場得 10 分↑的球隊",
         lambda x: one(x, (f(x, "home")["last_rf"] or 0) >= 10, (f(x, "away")["last_rf"] or 0) >= 10)),
        ("mlb_shutout", "public", "押上一場被完封的球隊",
         lambda x: one(x, f(x, "home")["last_rf"] == 0, f(x, "away")["last_rf"] == 0)),
        ("mlb_bigwin", "public", "押上一場贏 5 分↑的球隊",
         lambda x: one(
             x, (f(x, "home")["last_margin"] or 0) >= 5, (f(x, "away")["last_margin"] or 0) >= 5
         )),
        ("mlb_series", "public", "系列賽：押昨天贏的那隊再贏",
         lambda x: one(x, f(x, "home")["series_won"], f(x, "away")["series_won"])),
        ("mlb_600", "public", "押勝率 .600↑ 的強隊",
         lambda x: one(x, (f(x, "home")["pct"] or 0) >= 0.6, (f(x, "away")["pct"] or 0) >= 0.6)),
        ("mlb_playoff_fav", "public", "季後賽押熱門",
         lambda x: "fav_ml" if x["start"][5:7] in ("10", "11") else None),
        ("mlb_april_fav", "public", "開季（三、四月）押熱門",
         lambda x: "fav_ml" if x["start"][5:7] in ("03", "04") else None),
        ("mlb_drift_fav", "public", "熱門被拋售（開→收 −4pp↓）還押熱門",
         lambda x: "fav_ml" if x["g_open"] and x["g"].p - x["g_open"].p <= -0.04 else None),
    ]  # fmt: skip


def run_mlb(games: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    for key, group, name, pick in mlb_strategies(games):
        mine, fade = [], []
        for x in games:
            side = pick(x)
            if side is None:
                continue
            b, o = mlb_bet(x, side), mlb_bet(x, MLB_OPPOSITE[side])
            if b and o:
                mine.append(b)
                fade.append(o)
        out[key] = {
            "sport": "MLB",
            "group": group,
            "name": name,
            "self": score(mine, MLB_LEVELS),
            "fade": score(fade, MLB_LEVELS),
        }
    return out


# ======================================================================= football
def _f(row: dict[str, str], key: str) -> float | None:
    try:
        v = float(row[key])
    except (KeyError, TypeError, ValueError):
        return None
    return v if v > 1 else None


def _three(row: dict[str, str], keys: tuple[str, str, str]) -> list[float] | None:
    vals = [_f(row, k) for k in keys]
    return vals if all(vals) else None  # type: ignore[return-value]


def load_football(path: Path) -> list[dict[str, Any]]:
    out = []
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            try:
                hg, ag = int(row["FTHG"]), int(row["FTAG"])
                day = datetime.strptime(row["Date"], "%d/%m/%Y")
            except (KeyError, ValueError):
                continue
            avg = _three(row, ("AvgCH", "AvgCD", "AvgCA"))
            best = _three(row, ("MaxCH", "MaxCD", "MaxCA"))
            sharp = _three(row, ("PSCH", "PSCD", "PSCA")) or avg
            early = _three(row, ("PSH", "PSD", "PSA")) or _three(row, ("AvgH", "AvgD", "AvgA"))
            if not (sharp and avg and best):
                continue
            rec: dict[str, Any] = {
                "season": row["Season"],
                "div": row["Div"],
                "day": day,
                "home": row["HomeTeam"],
                "away": row["AwayTeam"],
                "hg": hg,
                "ag": ag,
                "res": "H" if hg > ag else ("D" if hg == ag else "A"),
                "p": dict(zip(SIDES, devig(sharp, "power"), strict=True)),
                "p_early": dict(zip(SIDES, devig(early, "power"), strict=True)) if early else None,
                "avg": dict(zip(SIDES, avg, strict=True)),
                "best": dict(zip(SIDES, best, strict=True)),
            }
            rec["tw"] = dict(
                zip(SIDES, tw_prices([rec["p"][s] for s in SIDES], TW_MARGIN["1x2"]), strict=True)
            )
            ou_sharp = [_f(row, "PC>2.5"), _f(row, "PC<2.5")]
            ou_avg = [_f(row, "AvgC>2.5"), _f(row, "AvgC<2.5")]
            ou_best = [_f(row, "MaxC>2.5"), _f(row, "MaxC<2.5")]
            if not all(ou_sharp):
                ou_sharp = ou_avg
            if all(ou_sharp) and all(ou_avg) and all(ou_best):
                po = devig(ou_sharp, "power")[0]
                tw_o, tw_u = tw_prices([po, 1 - po], TW_MARGIN["totals"])
                rec["ou"] = {
                    "p": {"O": po, "U": 1 - po},
                    "avg": {"O": ou_avg[0], "U": ou_avg[1]},
                    "best": {"O": ou_best[0], "U": ou_best[1]},
                    "tw": {"O": tw_o, "U": tw_u},
                }
            out.append(rec)
    out.sort(key=lambda r: r["day"])
    return out


def single(r: dict[str, Any], side: str) -> dict[str, float]:
    """Back one 1X2 outcome."""
    return {
        "hit": float(r["res"] == side),
        "prob": r["p"][side],
        "intl": r["avg"][side],
        "best": r["best"][side],
        "tw": r["tw"][side],
    }


def against(r: dict[str, Any], side: str) -> dict[str, float]:
    """The exact opposite: the other two outcomes, dutched."""
    others = [s for s in SIDES if s != side]
    return {
        "hit": float(r["res"] != side),
        "prob": 1 - r["p"][side],
        "intl": dutch(r["avg"][s] for s in others),
        "best": dutch(r["best"][s] for s in others),
        "tw": dutch(r["tw"][s] for s in others),
    }


def total(r: dict[str, Any], side: str) -> dict[str, float] | None:
    ou = r.get("ou")
    if not ou:
        return None
    return {
        "hit": float((r["hg"] + r["ag"] > 2.5) == (side == "O")),
        "prob": ou["p"][side],
        "intl": ou["avg"][side],
        "best": ou["best"][side],
        "tw": ou["tw"][side],
    }


def football_form(matches: list[dict[str, Any]]) -> list[dict[str, dict[str, Any]]]:
    """Both teams' state before every match, within their league-season."""
    hist: dict[tuple, list[tuple[str, int, int]]] = defaultdict(list)
    points: dict[tuple, int] = defaultdict(int)
    teams: dict[tuple, set[str]] = defaultdict(set)
    for r in matches:
        teams[(r["div"], r["season"])].update((r["home"], r["away"]))
    seasons = sorted({r["season"] for r in matches})
    previous = {s: seasons[i - 1] if i else None for i, s in enumerate(seasons)}
    out = []
    for r in matches:
        key = (r["div"], r["season"])
        state = {}
        table = sorted(teams[key], key=lambda t: -points[key + (t,)])
        for side, team in (("H", r["home"]), ("A", r["away"])):
            h = hist[key + (team,)]
            res = [x[0] for x in h]
            last = h[-1] if h else None
            prev = previous[r["season"]]
            prev_teams = teams.get((r["div"], prev), set()) if prev else set()
            state[side] = {
                "played": len(h),
                "win_run": _run(res, "W"),
                "loss_run": _run(res, "L"),
                "unbeaten": next((i for i, x in enumerate(reversed(res)) if x == "L"), len(res)),
                "winless": next((i for i, x in enumerate(reversed(res)) if x == "W"), len(res)),
                "last_margin": last[1] - last[2] if last else None,
                "last_gf": last[1] if last else None,
                "last_total": last[1] + last[2] if last else None,
                "rank": table.index(team) + 1 if len(h) >= 5 else None,
                "size": len(teams[key]),
                "promoted": bool(prev_teams) and team not in prev_teams,
            }
        out.append(state)
        h_res = "W" if r["hg"] > r["ag"] else ("D" if r["hg"] == r["ag"] else "L")
        a_res = {"W": "L", "L": "W", "D": "D"}[h_res]
        hist[key + (r["home"],)].append((h_res, r["hg"], r["ag"]))
        hist[key + (r["away"],)].append((a_res, r["ag"], r["hg"]))
        points[key + (r["home"],)] += {"W": 3, "D": 1, "L": 0}[h_res]
        points[key + (r["away"],)] += {"W": 3, "D": 1, "L": 0}[a_res]
    return out


def football_strategies(
    matches: list[dict[str, Any]],
) -> tuple[list[tuple[str, str, str, Callable]], list[tuple[str, str, str, Callable]]]:
    """(1X2 strategies, over/under strategies): (key, group, name, pick(i, match))."""
    form = football_form(matches)

    def f(i: int, side: str) -> dict[str, Any]:
        return form[i][side]

    def one(home: bool, away: bool) -> str | None:
        return None if home == away else ("H" if home else "A")

    def fav(r: dict[str, Any]) -> str:
        return min(("H", "A"), key=lambda s: r["avg"][s])

    def longshot(r: dict[str, Any], odds: float) -> str | None:
        sides = [s for s in SIDES if r["avg"][s] >= odds]
        return max(sides, key=lambda s: r["avg"][s]) if sides else None

    def move(r: dict[str, Any], thr: float) -> str | None:
        if not r["p_early"]:
            return None
        moves = {s: r["p"][s] - r["p_early"][s] for s in SIDES}
        s = max(moves, key=moves.get)  # type: ignore[arg-type]
        return s if moves[s] >= thr else None

    def drift(r: dict[str, Any]) -> str | None:
        if not r["p_early"]:
            return None
        moves = {s: r["p"][s] - r["p_early"][s] for s in SIDES}
        s = min(moves, key=moves.get)  # type: ignore[arg-type]
        return s if moves[s] <= -0.04 else None

    def last_place(i: int, side: str) -> bool:
        x = f(i, side)
        return x["rank"] is not None and x["rank"] == x["size"]

    one_x_two = [
        ("fb_home", "public", "每場都押主隊", lambda i, r: "H"),
        ("fb_draw", "public", "每場都押和局", lambda i, r: "D"),
        ("fb_away", "public", "每場都押客隊", lambda i, r: "A"),
        ("fb_fav", "public", "每場都押熱門", lambda i, r: fav(r)),
        ("longshot5", "public", "押冷門（賠率≥5）", lambda i, r: longshot(r, 5.0)),
        ("longshot8", "public", "押大冷門（賠率≥8）", lambda i, r: longshot(r, 8.0)),
        ("fb_lower_longshot", "public", "低級別聯賽押冷門（賠率≥5）",
         lambda i, r: longshot(r, 5.0) if r["div"] in LOWER_DIVISIONS else None),
        ("fb_short_fav", "public", "押超級熱門（賠率≤1.30）",
         lambda i, r: next((s for s in ("H", "A") if r["avg"][s] <= 1.30), None)),
        ("home_dog", "public", "押主場冷門", lambda i, r: "H" if r["avg"]["H"] > r["avg"]["A"] else None),
        ("fb_away_fav", "public", "押客場熱門", lambda i, r: "A" if r["avg"]["A"] < r["avg"]["H"] else None),
        ("fb_close_draw", "public", "勢均力敵（主客差<5pp）押和局",
         lambda i, r: "D" if abs(r["p"]["H"] - r["p"]["A"]) < 0.05 else None),
        ("fb_steam4", "mine", "追盤口移動（早盤→收盤 +4pp↑）", lambda i, r: move(r, 0.04)),
        ("fb_steam8", "mine", "追大單（早盤→收盤 +8pp↑）", lambda i, r: move(r, 0.08)),
        ("fb_drift", "public", "押被市場拋售的一邊（−4pp↓）", lambda i, r: drift(r)),
        ("fb_bigclub", "public", "押豪門（曼聯、皇馬、拜仁…）",
         lambda i, r: one(r["home"] in BIG_CLUBS, r["away"] in BIG_CLUBS)),
        ("fb_bigclub_away", "public", "押豪門客場",
         lambda i, r: "A" if r["away"] in BIG_CLUBS and r["home"] not in BIG_CLUBS else None),
        ("fb_win3", "public", "押三連勝的球隊",
         lambda i, r: one(f(i, "H")["win_run"] >= 3, f(i, "A")["win_run"] >= 3)),
        ("fb_win5", "public", "押五連勝的球隊",
         lambda i, r: one(f(i, "H")["win_run"] >= 5, f(i, "A")["win_run"] >= 5)),
        ("fb_loss3", "public", "押三連敗的球隊",
         lambda i, r: one(f(i, "H")["loss_run"] >= 3, f(i, "A")["loss_run"] >= 3)),
        ("fb_loss5", "public", "押五連敗的球隊",
         lambda i, r: one(f(i, "H")["loss_run"] >= 5, f(i, "A")["loss_run"] >= 5)),
        ("fb_unbeaten8", "public", "押 8 場不敗的球隊",
         lambda i, r: one(f(i, "H")["unbeaten"] >= 8, f(i, "A")["unbeaten"] >= 8)),
        ("fb_winless5", "public", "押 5 場沒贏的球隊",
         lambda i, r: one(f(i, "H")["winless"] >= 5, f(i, "A")["winless"] >= 5)),
        ("fb_bigwin", "public", "押上一場大勝（贏3球↑）的球隊",
         lambda i, r: one((f(i, "H")["last_margin"] or 0) >= 3, (f(i, "A")["last_margin"] or 0) >= 3)),
        ("fb_bigloss", "public", "押上一場大敗（輸3球↑）的球隊",
         lambda i, r: one((f(i, "H")["last_margin"] or 0) <= -3, (f(i, "A")["last_margin"] or 0) <= -3)),
        ("fb_scored4", "public", "押上一場進4球↑的球隊",
         lambda i, r: one((f(i, "H")["last_gf"] or 0) >= 4, (f(i, "A")["last_gf"] or 0) >= 4)),
        ("fb_promoted", "public", "押升班馬", lambda i, r: one(f(i, "H")["promoted"], f(i, "A")["promoted"])),
        ("fb_early_fav", "public", "開季前 5 輪押熱門",
         lambda i, r: fav(r) if f(i, "H")["played"] < 5 and f(i, "A")["played"] < 5 else None),
        ("fb_leader", "public", "押榜首球隊",
         lambda i, r: one(f(i, "H")["rank"] == 1, f(i, "A")["rank"] == 1)),
        ("fb_bottom", "public", "押墊底球隊", lambda i, r: one(last_place(i, "H"), last_place(i, "A"))),
    ]  # fmt: skip

    def totals_after(i: int, high: bool) -> bool:
        h, a = f(i, "H")["last_total"], f(i, "A")["last_total"]
        if h is None or a is None:
            return False
        return (h >= 4 and a >= 4) if high else (h <= 1 and a <= 1)

    over_under = [
        ("fb_over", "public", "每場都押大 2.5", lambda i, r: "O"),
        ("fb_under", "public", "每場都押小 2.5", lambda i, r: "U"),
        ("fb_over_after4", "public", "兩隊上一場都 4 球↑：押大 2.5",
         lambda i, r: "O" if totals_after(i, True) else None),
        ("fb_under_after1", "public", "兩隊上一場都 1 球↓：押小 2.5",
         lambda i, r: "U" if totals_after(i, False) else None),
    ]  # fmt: skip
    return one_x_two, over_under


def run_football(matches: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    out = {}
    one_x_two, over_under = football_strategies(matches)
    for key, group, name, pick in one_x_two:
        mine, fade = [], []
        for i, r in enumerate(matches):
            side = pick(i, r)
            if side:
                mine.append(single(r, side))
                fade.append(against(r, side))
        out[key] = _row("足球", group, name, mine, fade)
    for key, group, name, pick in over_under:
        mine, fade = [], []
        for i, r in enumerate(matches):
            side = pick(i, r)
            if not side:
                continue
            b, o = total(r, side), total(r, "U" if side == "O" else "O")
            if b and o:
                mine.append(b)
                fade.append(o)
        out[key] = _row("足球", group, name, mine, fade)
    return out


def _row(sport: str, group: str, name: str, mine: list, fade: list) -> dict:
    return {
        "sport": sport,
        "group": group,
        "name": name,
        "self": score(mine, FOOTBALL_LEVELS),
        "fade": score(fade, FOOTBALL_LEVELS),
    }


# ======================================================== the system's own model
def model_records(path: Path | None) -> list[dict[str, Any]]:
    """Walk-forward Dixon-Coles predictions (scripts/backtest_football.stage1_league)."""
    if path and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    spec = importlib.util.spec_from_file_location("bt", ROOT / "scripts" / "backtest_football.py")
    bt = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["bt"] = bt
    spec.loader.exec_module(bt)  # type: ignore[union-attr]
    by_league: dict[str, list[str]] = defaultdict(list)
    for p in sorted((DATA_DIR / "history").glob("*.csv")):
        by_league[p.name.split("_")[0].upper()].append(str(p))
    records: list[dict[str, Any]] = []
    for code, paths in sorted(by_league.items()):
        _, recs = bt.stage1_league((code, paths, MODEL.dc_xi, 120, 7))
        print(f"model {code}: {len(recs)} predictions", flush=True)
        records.extend(recs)
    if path:
        path.write_text(json.dumps(records), encoding="utf-8")
    return records


def run_model(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """模型分歧 (model above the market by a threshold, at the pre-match price as the
    football backtest bets it) and its opposite."""
    out = {}
    recs = [r for r in records if r["data_ok"]]
    for thr in (0.04, 0.10):
        mine, fade = [], []
        for r in recs:
            edges = [r["model"][i] - r["market"][i] for i in range(3)]
            i = max(range(3), key=lambda j: edges[j])
            if edges[i] < thr or r["model"][i] < MODEL.min_model_prob:
                continue
            close = r.get("close") or r["market"]
            others = [j for j in range(3) if j != i]
            tw = tw_prices(r["market"], TW_MARGIN["1x2"])
            mine.append(
                {
                    "hit": float(r["outcome"] == i),
                    "prob": close[i],
                    "intl": r["prices"][i],
                    "best": r["max_prices"][i],
                    "tw": tw[i],
                }
            )
            fade.append(
                {
                    "hit": float(r["outcome"] != i),
                    "prob": 1 - close[i],
                    "intl": dutch(r["prices"][j] for j in others),
                    "best": dutch(r["max_prices"][j] for j in others),
                    "tw": dutch(tw[j] for j in others),
                }
            )
        key = f"model{int(thr * 100)}"
        name = f"系統的模型分歧（模型比市場高 {int(thr * 100)}pp↑）"
        out[key] = _row("足球", "mine", name, mine, fade)
        dates = sorted(r["date"] for r in recs)
        out[key]["span"] = f"{dates[0][:7]} – {dates[-1][:7]}" if dates else ""
    return out


# ============================================================================ main
def halves(sport_all: dict, early: dict, late: dict) -> dict[str, dict[str, Any]]:
    """Attach the two halves' gaps, and a verdict on whether the strategy is bad in both."""
    for key, row in sport_all.items():
        row["early"] = early[key]["self"]
        row["late"] = late[key]["self"]
        row["fade_late"] = late[key]["fade"]
        gaps = [h.get("gap_pp") for h in (row["early"], row["late"])]
        z = row["self"].get("z")
        if z is not None and z <= -1.8 and all(g is not None and g < 0 for g in gaps):
            row["verdict"] = "consistent"
        elif z is not None and z <= -1.8:
            row["verdict"] = "unstable"
        elif z is not None and z >= 1.8:
            row["verdict"] = "better"
        else:
            row["verdict"] = "noise"
    return sport_all


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-records", type=Path, default=None)
    args = parser.parse_args(argv)

    games = load_mlb(MLB_PATH)
    print(f"MLB: {len(games)} games", flush=True)
    mlb = halves(
        run_mlb(games),
        run_mlb([x for x in games if x["season"] <= "2023"]),
        run_mlb([x for x in games if x["season"] >= "2024"]),
    )
    matches = load_football(FOOTBALL_PATH)
    print(f"football: {len(matches)} matches", flush=True)
    football = halves(
        run_football(matches),
        run_football([r for r in matches if r["season"] <= "2223"]),
        run_football([r for r in matches if r["season"] >= "2324"]),
    )
    model = run_model(model_records(args.model_records))
    for row in model.values():
        z = row["self"].get("z")
        row["verdict"] = "short" if z is None or z > -1.8 else "consistent"

    strategies = {**mlb, **football, **model}
    summary = {
        "generated": datetime.now(UTC).date().isoformat(),
        "data": {
            "mlb_games": len(games),
            "mlb_seasons": f"{games[0]['season']}–{games[-1]['season']}",
            "football_matches": len(matches),
            "football_seasons": "2019/20–2026/27",
            "football_leagues": len({r["div"] for r in matches}),
            "halves": {
                "mlb": ["2021–2023", "2024–2025"],
                "football": ["2019/20–2022/23", "2023/24–2026/27"],
            },
        },
        "margins": TW_MARGIN,
        "strategies": strategies,
    }
    OUT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    worst = sorted(strategies.items(), key=lambda kv: kv[1]["self"].get("z") or 0)
    for _key, row in worst[:12]:
        s, f = row["self"], row["fade"]
        print(
            f"{row['sport']:4s} {row['name'][:28]:30s} gap {s['gap_pp']:+5.2f}pp z {s['z']:+5.2f} "
            f"{row['verdict']:10s} fade: fair {f['roi_fair']:+5.1f} intl {f['roi_intl']:+5.1f} "
            f"best {f['roi_best']:+5.1f} 運彩 {f['roi_tw']:+6.1f}"
        )
    print("wrote", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
