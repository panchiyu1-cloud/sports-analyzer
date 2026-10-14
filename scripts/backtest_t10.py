"""Backtest of the owner's T-10 baseball rule on 2021-2025 MLB closing odds.

    python scripts/backtest_t10.py [data/raw/mlb_odds_2021_2025.csv.gz]

The odds come from SportsBookReview (DraftKings, FanDuel, Caesars, bet365, BetMGM,
BetRivers; opening and closing moneyline, run line and totals), as collected by
github.com/ArnavSaraogi/mlb-odds-scraper. Fetch the flattened CSV first:

    git fetch origin t10-data
    git show origin/t10-data:t10/mlb_odds_2021_2025.csv.gz > data/raw/mlb_odds_2021_2025.csv.gz

(the "T-10 backtest data" workflow builds that branch). The 76 MB source is never
committed. Writes docs/t10_backtest.html and data/t10_backtest_summary.json.

The rule is tested as the owner defined it (G = p + c - 1 at the closing line,
±6pp); the Switch zones, price guard and overlays of the prompt version are not,
because their exact rules are not written down here.
"""

from __future__ import annotations

import csv
import gzip
import json
import math
import sys
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinja2 import Environment, FileSystemLoader, select_autoescape  # noqa: E402

from sports_analyzer.analysis.t10_backtest import game_g, outcome, summarise  # noqa: E402

BOOK_ORDER = ("draftkings", "fanduel", "caesars", "bet365", "betmgm", "bet_rivers_ny")
GAME_TYPES = {"R": "例行賽", "F": "季後賽", "D": "季後賽", "L": "季後賽", "W": "季後賽"}
THRESHOLD = 0.06
GRID = (0.02, 0.04, 0.06, 0.08, 0.10)
# 運彩's measured MLB margin (2026-10 uploads): moneyline, run line and totals ~15%.
TW_MARGIN = 0.15
# Table rows with fewer games show only the count: a hit rate on a handful of games
# (BetRivers had 3 qualifying underdogs) is noise that reads like a finding.
MIN_N = 100


def load(path: Path) -> list[dict[str, Any]]:
    """One record per game: the preferred book's G at close and open, the outcome,
    and the best closing price across books for each signal's bet."""
    by_game: dict[tuple, list[dict[str, str]]] = defaultdict(list)
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row["game_type"] not in GAME_TYPES or not row["status"].startswith("Final"):
                continue
            if row["home_score"] == "" or row["away_score"] == "":
                continue
            by_game[(row["date"], row["start_utc"], row["away"], row["home"])].append(row)

    games = []
    for key, rows in by_game.items():
        rank = {b: i for i, b in enumerate(BOOK_ORDER)}
        rows.sort(key=lambda r: rank.get(r["book"], 99))
        priced = []
        for r in rows:
            close = game_g(
                r["ml_close_home"],
                r["ml_close_away"],
                r["rl_close_home_spread"],
                r["rl_close_home"],
                r["rl_close_away"],
            )
            if close:
                priced.append((r, close))
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
        mult = game_g(
            r["ml_close_home"],
            r["ml_close_away"],
            r["rl_close_home_spread"],
            r["rl_close_home"],
            r["rl_close_away"],
            method="multiplicative",
        )
        scores = (int(float(r["home_score"])), int(float(r["away_score"])))
        res = outcome(*scores, close.favourite)
        same_fav = [g for _, g in priced if g.favourite == close.favourite]
        games.append(
            {
                "date": key[0],
                "season": key[0][:4],
                "stage": GAME_TYPES[r["game_type"]],
                "book": r["book"],
                "g": close,
                "g_open": opn if opn and opn.favourite == close.favourite else None,
                "g_mult": mult,
                "res": res,
                "scores": scores,
                "best_ml_fav": max(g.ml_fav for g in same_fav),
                "best_rl_dog": max(g.rl_dog for g in same_fav),
                "per_book": {rr["book"]: gg for rr, gg in priced},
            }
        )
    return games


def fav_bets(
    games: list[dict[str, Any]], price: str = "close", g_key: str = "g"
) -> list[dict[str, float]]:
    out = []
    for x in games:
        g = x[g_key]
        p = {"close": g.ml_fav, "best": x["best_ml_fav"]}.get(price)
        if price == "tw":
            p = 1 / (g.p * (1 + TW_MARGIN))
        out.append({"hit": float(x["res"].fav_win), "prob": g.p, "price": p})
    return out


def dog_bets(
    games: list[dict[str, Any]], price: str = "close", g_key: str = "g"
) -> list[dict[str, float]]:
    out = []
    for x in games:
        g = x[g_key]
        q = 1 - g.c
        p = {"close": g.rl_dog, "best": x["best_rl_dog"]}.get(price)
        if price == "tw":
            p = 1 / (q * (1 + TW_MARGIN))
        out.append({"hit": float(x["res"].dog_cover), "prob": q, "price": p})
    return out


def clv(bets: list[dict[str, float]], closing_probs: list[float]) -> float | None:
    """Average EV of each bet judged by the closing line: closing fair prob x price taken - 1."""
    if not bets:
        return None
    return round(
        sum(cp * b["price"] - 1 for b, cp in zip(bets, closing_probs, strict=True)) / len(bets), 4
    )


def deciles(games: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(games, key=lambda x: x["g"].g)
    n = len(ordered)
    out = []
    for k in range(10):
        chunk = ordered[k * n // 10 : (k + 1) * n // 10]
        m = len(chunk)
        p = sum(x["g"].p for x in chunk) / m
        q = sum(1 - x["g"].c for x in chunk) / m
        win = sum(x["res"].fav_win for x in chunk) / m
        dog = sum(x["res"].dog_cover for x in chunk) / m
        se_p = math.sqrt(sum(x["g"].p * (1 - x["g"].p) for x in chunk)) / m
        se_q = math.sqrt(sum((1 - x["g"].c) * x["g"].c for x in chunk)) / m
        out.append(
            {
                "n": m,
                "g_lo": round(100 * chunk[0]["g"].g, 1),
                "g_hi": round(100 * chunk[-1]["g"].g, 1),
                "g_mean": round(100 * sum(x["g"].g for x in chunk) / m, 1),
                "fav_gap_pp": round(100 * (win - p), 2),
                "fav_se_pp": round(100 * se_p, 2),
                "dog_gap_pp": round(100 * (dog - q), 2),
                "dog_se_pp": round(100 * se_q, 2),
                "p": round(p, 3),
                "win": round(win, 3),
            }
        )
    return out


def analyse(games: list[dict[str, Any]]) -> dict[str, Any]:
    fav = [x for x in games if x["g"].g >= THRESHOLD]
    dog = [x for x in games if x["g"].g <= -THRESHOLD]
    s: dict[str, Any] = {
        "generated": datetime.now(UTC).strftime("%Y-%m-%d"),
        "games": len(games),
        "seasons": sorted({x["season"] for x in games}),
        "first": min(x["date"] for x in games),
        "last": max(x["date"] for x in games),
        "books": dict(sorted(_count(x["book"] for x in games).items(), key=lambda kv: -kv[1])),
        "threshold": THRESHOLD,
        "tw_margin": TW_MARGIN,
        "min_n": MIN_N,
    }
    s["main"] = {
        "fav": {
            **summarise(fav_bets(fav)),
            "roi_best": summarise(fav_bets(fav, "best"))["roi"],
            "roi_tw": summarise(fav_bets(fav, "tw"))["roi"],
        },
        "dog": {
            **summarise(dog_bets(dog)),
            "roi_best": summarise(dog_bets(dog, "best"))["roi"],
            "roi_tw": summarise(dog_bets(dog, "tw"))["roi"],
        },
    }
    mult_fav = [x for x in games if x["g_mult"] and x["g_mult"].g >= THRESHOLD]
    mult_dog = [x for x in games if x["g_mult"] and x["g_mult"].g <= -THRESHOLD]
    s["multiplicative"] = {
        "fav": summarise(fav_bets(mult_fav, g_key="g_mult")),
        "dog": summarise(dog_bets(mult_dog, g_key="g_mult")),
    }
    s["baseline"] = {"fav": summarise(fav_bets(games)), "dog": summarise(dog_bets(games))}
    s["grid"] = [
        {
            "thr_pp": round(100 * t, 0),
            "fav": summarise(fav_bets([x for x in games if x["g"].g >= t])),
            "dog": summarise(dog_bets([x for x in games if x["g"].g <= -t])),
        }
        for t in GRID
    ]
    s["seasons_table"] = [
        {
            "season": season,
            "fav": summarise(fav_bets([x for x in fav if x["season"] == season])),
            "dog": summarise(dog_bets([x for x in dog if x["season"] == season])),
        }
        for season in s["seasons"]
    ]
    s["stages"] = [
        {
            "stage": stage,
            "fav": summarise(fav_bets([x for x in fav if x["stage"] == stage])),
            "dog": summarise(dog_bets([x for x in dog if x["stage"] == stage])),
        }
        for stage in ("例行賽", "季後賽")
    ]
    by_book = []
    for book in BOOK_ORDER:
        bf, bd = [], []
        for x in games:
            g = x["per_book"].get(book)
            if g is None:
                continue
            # A book may see the other team as favourite: settle from its own view.
            res = outcome(*x["scores"], g.favourite)
            if g.g >= THRESHOLD:
                bf.append({"hit": float(res.fav_win), "prob": g.p, "price": g.ml_fav})
            elif g.g <= -THRESHOLD:
                bd.append({"hit": float(res.dog_cover), "prob": 1 - g.c, "price": g.rl_dog})
        by_book.append({"book": book, "fav": summarise(bf), "dog": summarise(bd)})
    s["books_table"] = by_book

    # Betting the opening price when the OPENING G fires, judged by the close (CLV).
    of = [x for x in games if x["g_open"] and x["g_open"].g >= THRESHOLD]
    od = [x for x in games if x["g_open"] and x["g_open"].g <= -THRESHOLD]
    of_bets = [
        {"hit": float(x["res"].fav_win), "prob": x["g_open"].p, "price": x["g_open"].ml_fav}
        for x in of
    ]
    od_bets = [
        {"hit": float(x["res"].dog_cover), "prob": 1 - x["g_open"].c, "price": x["g_open"].rl_dog}
        for x in od
    ]
    stable_f = [x for x in fav if x["g_open"] and x["g_open"].g >= THRESHOLD]
    stable_d = [x for x in dog if x["g_open"] and x["g_open"].g <= -THRESHOLD]
    s["variants"] = [
        {
            "name": "開盤時 G 達標就用開盤價下注",
            "fav": {**summarise(of_bets), "clv": clv(of_bets, [x["g"].p for x in of])},
            "dog": {**summarise(od_bets), "clv": clv(od_bets, [1 - x["g"].c for x in od])},
        },
        {
            "name": "開盤和收盤 G 都達標（穩定）才下注，用收盤價",
            "fav": {
                **summarise(fav_bets(stable_f)),
                "clv": clv(fav_bets(stable_f), [x["g"].p for x in stable_f]),
            },
            "dog": {
                **summarise(dog_bets(stable_d)),
                "clv": clv(dog_bets(stable_d), [1 - x["g"].c for x in stable_d]),
            },
        },
    ]
    s["deciles"] = deciles(games)
    s["chart_fav"] = chart(s["deciles"], "fav")
    s["chart_dog"] = chart(s["deciles"], "dog")
    s["verdict"], s["passed"] = verdict(s)
    return s


def chart(deciles_: list[dict[str, Any]], key: str) -> dict[str, Any]:
    """Dot-and-whisker geometry (SVG units) for one side: gap to the market by G decile."""
    width, height, left, right, top, bottom = 340, 190, 34, 8, 10, 34
    span = max(6.0, max(abs(d[f"{key}_gap_pp"]) + 2 * d[f"{key}_se_pp"] for d in deciles_))
    span = math.ceil(span / 2) * 2
    plot_w, plot_h = width - left - right, height - top - bottom

    def y(v: float) -> float:
        return round(top + plot_h * (span - v) / (2 * span), 1)

    step = plot_w / len(deciles_)
    points = []
    for i, d in enumerate(deciles_):
        gap, se = d[f"{key}_gap_pp"], d[f"{key}_se_pp"]
        points.append(
            {
                "x": round(left + step * (i + 0.5), 1),
                "y": y(gap),
                "y_lo": y(gap - 2 * se),
                "y_hi": y(gap + 2 * se),
                "label": f"{d['g_mean']:+.0f}",
                "tip": f"G {d['g_lo']:+.1f} ~ {d['g_hi']:+.1f}pp，{d['n']} 場：比市場 {gap:+.1f}pp（±{2 * se:.1f}）",
            }
        )
    ticks = [{"v": v, "y": y(v)} for v in range(-span, span + 1, max(2, span // 2))]
    return {
        "w": width,
        "h": height,
        "left": left,
        "right": width - right,
        "top": top,
        "bottom": height - bottom,
        "zero": y(0),
        "points": points,
        "ticks": ticks,
    }


def verdict(s: dict[str, Any]) -> tuple[str, bool]:
    """Pass only if a signal beats the closing market's own probability by more than two
    standard errors under both de-vig methods AND makes money at the book's closing price."""
    passed = []
    for side, label in (("fav", "強隊 ML"), ("dog", "弱隊 +1.5")):
        m, mm = s["main"][side], s["multiplicative"][side]
        if (m["z"] or 0) > 2 and (mm["z"] or 0) > 2 and (m["roi"] or 0) > 0:
            passed.append(label)
    if passed:
        return f"通過：{'、'.join(passed)} 在收盤價下仍有正報酬，且勝率顯著高於市場機率。", True
    return (
        "未通過：G ≥ +6pp 的強隊 ML 和 G ≤ −6pp 的弱隊 +1.5，實際勝率都沒有顯著高於收盤市場自己的機率，"
        "在收盤價下注都是負報酬——T-10 的 G 值目前看不出市場沒算到的資訊。",
        False,
    )


def _count(items: Any) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for i in items:
        out[i] += 1
    return dict(out)


def main(path: Path) -> dict[str, Any]:
    games = load(path)
    summary = analyse(games)
    (ROOT / "data" / "t10_backtest_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    env = Environment(
        loader=FileSystemLoader(str(ROOT / "src" / "sports_analyzer" / "render" / "templates")),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    html = env.get_template("t10_backtest.html.j2").render(s=summary)
    (ROOT / "docs" / "t10_backtest.html").write_text(html, encoding="utf-8")
    m = summary["main"]
    print(f"{summary['games']} games {summary['first']}..{summary['last']}")
    print("FAV ML  (G>=+6pp):", m["fav"])
    print("DOG +1.5 (G<=-6pp):", m["dog"])
    print(summary["verdict"])
    return summary


if __name__ == "__main__":
    main(
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else ROOT / "data" / "raw" / "mlb_odds_2021_2025.csv.gz"
    )
