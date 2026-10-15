"""組合篩選: do several strategies agreeing on a bet pick winners the market misses?

    python scripts/backtest_combos.py

Builds on scripts/backtest_fade.py (same data under data/raw/, the same 65 strategies
plus T-10 at +/-3pp, the same 運彩 price model) and writes data/combo_summary.json.

A filter is one bet type (MLB: favourite/underdog moneyline or run line, over/under;
football: one 1X2 outcome, two outcomes dutched, over/under 2.5) plus one to three
conditions; a condition is "strategy k fired and pointed at X". Every filter with at
least MIN_TRAIN bets in the early seasons is scored there; fixed rules pick a few and
only those are looked up in the late seasons (MLB 2021-23 / 2024-25, football
2019/20-2022/23 / 2023/24-). Also scored: whether early winners stay winners (deciles of
the early z), a vote count of conditions that backed a bet and beat the market early, a
ridge logistic regression of all conditions on top of the market's probability, and the
owner's own combinations written down before looking.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from statistics import NormalDist
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

import backtest_fade as bf  # noqa: E402
from sports_analyzer.config import DATA_DIR  # noqa: E402

OUT_PATH = DATA_DIR / "combo_summary.json"
LEVELS = ("intl", "best", "tw")
NC = 6 + 3 * len(LEVELS)  # n hit prob var ret_fair ret_fair^2 | ret ret^2 1/price per level
MIN_TRAIN = 200  # early-season bets a filter needs before it is scored at all
RULE_N = 10  # filters each selection rule takes to the late seasons
VOTE_Z = 1.0  # a single condition "beat the market early" at this z
EV_MIN = 0.03  # the system's own bar for a 運彩 value bet (mlb_value_threshold)

MLB_BETS = {"fav_ml": "熱門不讓分", "dog_ml": "冷門不讓分", "fav_rl": "熱門 −1.5",
            "dog_rl": "冷門 +1.5", "over": "大分", "under": "小分"}  # fmt: skip
FB_BETS = {"SH": "主勝", "SD": "和局", "SA": "客勝", "NH": "和局＋客勝", "ND": "主勝＋客勝",
           "NA": "主勝＋和局", "TO": "大 2.5", "TU": "小 2.5"}  # fmt: skip
MLB_DIR = {"fav_ml": "fav", "fav_rl": "fav", "dog_ml": "dog", "dog_rl": "dog",
           "over": "over", "under": "under"}  # fmt: skip
MLB_OPP_DIR = {"fav": "dog", "dog": "fav", "over": "under", "under": "over"}
FB_SET = {"SH": {"H"}, "SD": {"D"}, "SA": {"A"}, "NH": {"D", "A"}, "ND": {"H", "A"},
          "NA": {"H", "D"}, "TO": {"O"}, "TU": {"U"}}  # fmt: skip
FB_OPP = {"SH": "NH", "SD": "ND", "SA": "NA", "NH": "SH", "ND": "SD", "NA": "SA",
          "TO": "TU", "TU": "TO"}  # fmt: skip
REL_ZH = {"pick": "跟", "fade": "反", "ctx": "條件"}

# The owner's combinations (T-10 with the 反者道之動 fades), fixed before any search.
OWNER_COMBOS = {
    "mlb": [
        ("G≥+6 而且主場熱門≥55% → 押客隊不讓分", "dog_ml", [("t10_g6", "fav_ml"), ("home_fav55", "fav_ml")]),
        ("G≥+6 而且主場熱門≥55% → 押客隊 +1.5", "dog_rl", [("t10_g6", "fav_ml"), ("home_fav55", "fav_ml")]),
        ("G≥+6 而且熱門被買進 4pp↑ → 押冷門不讓分", "dog_ml", [("t10_g6", "fav_ml"), ("mlb_steam_fav", "fav_ml")]),
        ("G≥+6 而且熱門三連勝 → 押冷門不讓分", "dog_ml", [("t10_g6", "fav_ml"), ("mlb_win3", "fav_ml")]),
    ],
    "football": [
        ("主隊是冷門而且賠率≥8 → 押和局＋客勝", "NH", [("home_dog", "SH"), ("longshot8", "SH")]),
        ("主隊是冷門而且三連勝 → 押和局＋客勝", "NH", [("home_dog", "SH"), ("fb_win3", "SH")]),
        ("主隊是冷門而且上一場大勝 → 押和局＋客勝", "NH", [("home_dog", "SH"), ("fb_bigwin", "SH")]),
        ("三連勝而且上一場大勝的主隊 → 押和局＋客勝", "NH", [("fb_win3", "SH"), ("fb_bigwin", "SH")]),
        ("三連勝而且上一場大勝的客隊 → 押主勝＋和局", "NA", [("fb_win3", "SA"), ("fb_bigwin", "SA")]),
    ],
}  # fmt: skip


# ======================================================================== scoring
def stat_row(bet: dict[str, float]) -> list[float]:
    """One bet as the sums fade.score needs."""
    hit, prob = bet["hit"], bet["prob"]
    ret = hit / prob - 1
    vals = [1.0, hit, prob, prob * (1 - prob), ret, ret * ret]
    for level in LEVELS:
        r = hit * bet[level] - 1
        vals += [r, r * r, 1 / bet[level]]
    return vals


def sums_score(sums: np.ndarray) -> dict[str, np.ndarray]:
    """fade.score from summed stat rows (one filter per row), as fractions."""
    s = np.atleast_2d(np.asarray(sums, dtype=float))
    n = s[:, 0]
    with np.errstate(divide="ignore", invalid="ignore"):
        out = {
            "n": n,
            "hit": s[:, 1] / n,
            "fair": s[:, 2] / n,
            "gap": (s[:, 1] - s[:, 2]) / n,
            "z": (s[:, 1] - s[:, 2]) / np.sqrt(s[:, 3]),
        }
        for k, level in enumerate(("fair", *LEVELS)):
            r, r2 = s[:, 3 + 3 * k + (k == 0)], s[:, 4 + 3 * k + (k == 0)]
            roi = r / n
            sd = np.sqrt(np.maximum(r2 - n * roi**2, 0) / (n - 1))
            out["roi_" + level] = roi
            out["se_" + level] = sd / np.sqrt(n)
            if k:
                out["need_" + level] = s[:, 5 + 3 * k] / n - out["fair"]
    return out


def as_score(d: dict[str, np.ndarray], i: int = 0) -> dict[str, Any]:
    """One filter in fade.score's units and rounding."""
    n = int(d["n"][i])
    if n == 0:
        return {"n": 0}
    z = float(d["z"][i])
    out: dict[str, Any] = {
        "n": n,
        "hit": round(float(d["hit"][i]) * 100, 1),
        "fair": round(float(d["fair"][i]) * 100, 1),
        "gap_pp": round(float(d["gap"][i]) * 100, 2),
        "z": round(z, 2) if np.isfinite(z) else None,
    }
    for level in ("fair", *LEVELS):
        out["roi_" + level] = round(float(d["roi_" + level][i]) * 100, 1)
        se = float(d["se_" + level][i])
        out["se_" + level] = round(se * 100, 1) if np.isfinite(se) else 0.0
        if level != "fair":
            out["need_" + level] = round(float(d["need_" + level][i]) * 100, 1)
    return out


def relation(sport: str, x: str, bet: str) -> str:
    """How a condition's pick X relates to the bet: 'pick' (the bet follows the
    strategy), 'fade' (the bet is against it) or 'ctx' (another market)."""
    if sport == "mlb":
        if MLB_DIR[x] == MLB_DIR[bet]:
            return "pick"
        return "fade" if MLB_OPP_DIR[MLB_DIR[x]] == MLB_DIR[bet] else "ctx"
    if FB_SET[bet] <= FB_SET[x]:
        return "pick"
    return "fade" if FB_SET[bet] <= FB_SET[FB_OPP[x]] else "ctx"


# ========================================================================= games
def mlb_games() -> tuple[list[dict[str, Any]], list[tuple]]:
    games = bf.load_mlb(bf.MLB_PATH)
    strategies = bf.mlb_strategies(games, bf.mlb_results(bf.MLB_PATH)) + [
        ("t10_g3", "mine", "T-10：G≥+3 押強隊不讓分",
         lambda x: "fav_ml" if x["g"].g >= 0.03 else None),
        ("t10_gm3", "mine", "T-10：G≤−3 押弱隊 +1.5",
         lambda x: "dog_rl" if x["g"].g <= -0.03 else None),
    ]  # fmt: skip
    rows = []
    for x in games:
        active = {k: side for k, _, _, pick in strategies if (side := pick(x))}
        bets = {b: bf.mlb_bet(x, b) for b in MLB_BETS}
        rows.append({"season": x["season"], "test": x["season"] >= "2024", "active": active,
                     "bets": bets})  # fmt: skip
    return rows, strategies


def football_games() -> tuple[list[dict[str, Any]], list[tuple]]:
    matches = bf.load_football(bf.FOOTBALL_PATH)
    one_x_two, over_under = bf.football_strategies(matches)
    rows = []
    for i, r in enumerate(matches):
        active = {k: "S" + side for k, _, _, pick in one_x_two if (side := pick(i, r))}
        active.update({k: "T" + side for k, _, _, pick in over_under if (side := pick(i, r))})
        bets: dict[str, Any] = {}
        for s in "HDA":
            bets["S" + s] = bf.single(r, s)
            bets["N" + s] = bf.against(r, s)
        for s in "OU":
            bets["T" + s] = bf.total(r, s)
        rows.append({"season": r["season"], "test": r["season"] >= "2324", "active": active,
                     "bets": bets})  # fmt: skip
    return rows, one_x_two + over_under


class Board:
    """Conditions as boolean columns, one matrix per bet type. A condition that fires in
    (nearly) every game filters nothing and is left out; two conditions on exactly the
    same games ("home underdog" = "away favourite") are one column with several names."""

    def __init__(self, sport: str, rows: list[dict[str, Any]], strategies: list[tuple]) -> None:
        self.sport = sport
        self.names = {k: n for k, _, n, _ in strategies}
        self.groups = {k: g for k, g, _, _ in strategies}
        self.bets = list(MLB_BETS if sport == "mlb" else FB_BETS)
        games: dict[tuple[str, str], list[int]] = {}
        for gi, r in enumerate(rows):
            for k, x in r["active"].items():
                games.setdefault((k, x), []).append(gi)
        fires: Counter[str] = Counter()
        for (k, _), g in games.items():
            fires[k] += len(g)
        always = {k for k, n in fires.items() if n >= 0.95 * len(rows)}
        keep = sorted(
            (c for c, g in games.items() if 30 <= len(g) <= 0.95 * len(rows)),
            key=lambda c: (c[0] in always, c),
        )
        self.conds: list[tuple[str, str]] = []
        self.aliases: dict[tuple[str, str], list[tuple[str, str]]] = {}
        seen: dict[tuple[int, ...], tuple[str, str]] = {}
        col: dict[tuple[str, str], int] = {}
        for c in keep:
            rep = seen.setdefault(tuple(games[c]), c)
            if rep == c:
                col[c] = len(self.conds)
                self.conds.append(c)
                self.aliases[c] = [c]
            else:
                col[c] = col[rep]
                self.aliases[rep].append(c)
        self.col = col
        self.data = {b: self._matrix(rows, b) for b in self.bets}

    def _matrix(self, rows: list[dict[str, Any]], bet: str) -> dict[str, Any]:
        sel = [r for r in rows if r["bets"].get(bet)]
        c = np.zeros((len(sel), len(self.conds)), dtype=bool)
        for i, r in enumerate(sel):
            for k, x in r["active"].items():
                j = self.col.get((k, x))
                if j is not None:
                    c[i, j] = True
        stats = np.array([stat_row(r["bets"][bet]) for r in sel]).reshape(len(sel), NC)
        test = np.array([r["test"] for r in sel], dtype=bool)
        return {
            "C": c,
            "stats": stats,
            "test": test,
            "season": np.array([r["season"] for r in sel]),
            "S": np.hstack([stats * (~test)[:, None], stats * test[:, None]]),
        }

    def rel(self, i: int, bet: str) -> str:
        rels = {relation(self.sport, x, bet) for _, x in self.aliases[self.conds[i]]}
        return "pick" if "pick" in rels else ("fade" if "fade" in rels else "ctx")

    def mine(self, i: int) -> bool:
        return any(self.groups.get(k) == "mine" for k, _ in self.aliases[self.conds[i]])

    def describe(self, bet: str, idx: Iterable[int]) -> dict[str, Any]:
        labels = MLB_BETS if self.sport == "mlb" else FB_BETS
        conds = []
        for i in idx:
            names = [self.names.get(k, k) for k, _ in self.aliases[self.conds[i]]]
            conds.append({"rel": self.rel(i, bet), "names": names,
                          "keys": [list(a) for a in self.aliases[self.conds[i]]]})  # fmt: skip
        text = "　＆　".join(f"{REL_ZH[c['rel']]}：{'＝'.join(c['names'])}" for c in conds)
        return {"bet": bet, "bet_label": labels[bet], "conds": conds,
                "text": f"押{labels[bet]}｜{text}"}  # fmt: skip


# ========================================================================= search
def search(board: Board, min_train: int = MIN_TRAIN) -> dict[str, Any]:
    """Every filter of 1-3 conditions with min_train early bets. A filter whose extra
    condition removes no bet (same count as a smaller filter, so the same bets) is
    skipped; so are conditions that never meet."""
    bets: list[str] = []
    conds: list[tuple[int, ...]] = []
    train: list[np.ndarray] = []
    test: list[np.ndarray] = []

    def add(b: str, idx: tuple[int, ...], row: np.ndarray) -> None:
        bets.append(b)
        conds.append(idx)
        train.append(row[:NC])
        test.append(row[NC:])

    m = len(board.conds)
    for b in board.bets:
        c, s = board.data[b]["C"], board.data[b]["S"]
        single = c.astype(float).T @ s
        n1 = [(row[0], row[NC]) for row in single]
        for i in range(m):
            if n1[i][0] >= min_train:
                add(b, (i,), single[i])
        pairs: dict[tuple[int, int], tuple[float, float]] = {}
        for i in range(m - 1):
            if n1[i][0] < min_train:
                continue
            sums = (c[:, i : i + 1] & c[:, i + 1 :]).astype(float).T @ s
            for off, row in enumerate(sums):
                j = i + 1 + off
                nn = (row[0], row[NC])
                if nn[0] < min_train or nn in (n1[i], n1[j]):
                    continue
                pairs[(i, j)] = nn
                add(b, (i, j), row)
        for (i, j), nij in pairs.items():
            base = c[:, i] & c[:, j]
            sums = (base[:, None] & c[:, j + 1 :]).astype(float).T @ s
            for off, row in enumerate(sums):
                k = j + 1 + off
                nn = (row[0], row[NC])
                smaller = [nij, pairs.get((i, k)), pairs.get((j, k)), n1[i], n1[j], n1[k]]
                if nn[0] < min_train or nn in smaller:
                    continue
                add(b, (i, j, k), row)
    return {"bet": bets, "conds": conds, "train": np.array(train), "test": np.array(test)}


def by_season(board: Board, bet: str, idx: Sequence[int]) -> list[dict[str, Any]]:
    d = board.data[bet]
    mask = np.all(d["C"][:, list(idx)], axis=1)
    out = []
    for season in sorted(set(d["season"][mask])):
        sel = mask & (d["season"] == season)
        s = as_score(sums_score(d["stats"][sel].sum(axis=0)))
        out.append({"season": str(season), **{k: s[k] for k in ("n", "hit", "fair", "gap_pp")}})
    return out


def rules(board: Board, found: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Selection fixed before the late seasons are looked at:
    A the 10 best early z (hit rate above the market); B the 10 best early t-stat of the
    運彩 ROI; C agreement filters only (every condition backs or fades the bet) that use
    the owner's own logic (T-10, steam), top 10 by early z; D Bonferroni survivors."""
    tr = sums_score(found["train"])
    bet, conds = found["bet"], found["conds"]
    m = len(bet)
    order = list(np.argsort(-tr["z"]))
    t_tw = np.nan_to_num(tr["roi_tw"] / tr["se_tw"], nan=-99.0)
    zcut = NormalDist().inv_cdf(1 - 0.05 / m)

    def agree(i: int) -> bool:
        return any(board.mine(j) for j in conds[i]) and all(
            board.rel(j, bet[i]) != "ctx" for j in conds[i]
        )

    picks = {
        "A": order[:RULE_N],
        "B": list(np.argsort(-t_tw)[:RULE_N]),
        "C": [i for i in order if agree(i)][:RULE_N],
        "D": [i for i in order if tr["z"][i] >= zcut],
    }
    te = sums_score(found["test"])
    out = {}
    for rule, idx in picks.items():
        out[rule] = [
            {
                **board.describe(bet[i], conds[i]),
                "train": as_score(tr, i),
                "test": as_score(te, i),
                "seasons": by_season(board, bet[i], conds[i]),
            }
            for i in idx
        ]
    return out


def deciles(found: dict[str, Any], min_test: int = 100) -> dict[str, Any]:
    """Do filters that beat the market early keep doing it? Filters by decile of the
    early z, the mean gap early and late."""
    tr, te = sums_score(found["train"]), sums_score(found["test"])
    ok = te["n"] >= min_test
    zs = tr["z"][ok]
    edges = np.quantile(zs, np.linspace(0, 1, 11))
    rows = []
    for q in range(10):
        upper = zs <= edges[q + 1] if q == 9 else zs < edges[q + 1]
        sel = np.zeros(len(ok), dtype=bool)
        sel[np.where(ok)[0][(zs >= edges[q]) & upper]] = True
        rows.append(
            {
                "decile": q + 1,
                "filters": int(sel.sum()),
                "train_gap_pp": round(float(np.mean(tr["gap"][sel])) * 100, 2),
                "test_gap_pp": round(float(np.mean(te["gap"][sel])) * 100, 2),
                "test_need_tw_pp": round(float(np.mean(te["need_tw"][sel])) * 100, 2),
                "train_roi_tw": round(float(np.mean(tr["roi_tw"][sel])) * 100, 1),
                "test_roi_tw": round(float(np.mean(te["roi_tw"][sel])) * 100, 1),
            }
        )
    return {
        "filters": int(ok.sum()),
        "rows": rows,
        "corr": round(float(np.corrcoef(tr["gap"][ok], te["gap"][ok])[0, 1]), 3),
    }


def votes(board: Board) -> dict[str, Any]:
    """Count, for every bet, the conditions that back or fade it and whose single filter
    beat the market early (z >= VOTE_Z); bet when the count reaches k."""
    parts = []
    for b in board.bets:
        d = board.data[b]
        single = sums_score(d["C"][~d["test"]].astype(float).T @ d["stats"][~d["test"]])
        backs = np.array([board.rel(j, b) != "ctx" for j in range(len(board.conds))])
        good = (single["z"] >= VOTE_Z) & (single["n"] >= 100) & backs
        parts.append(((d["C"] & good).sum(axis=1), d["stats"], d["test"]))
    out = {}
    for k in (1, 2, 3, 4):
        res = {}
        for period, want in (("train", False), ("test", True)):
            rows = [s[(v >= k) & (t == want)] for v, s, t in parts]
            res[period] = as_score(sums_score(np.vstack(rows).sum(axis=0)))
        out[str(k)] = res
    return out


# ========================================================================== model
def _sigmoid(x: np.ndarray) -> np.ndarray:
    return 1 / (1 + np.exp(-x))


def _logloss(y: np.ndarray, p: np.ndarray) -> float:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def fit_offset_logit(
    x: np.ndarray, y: np.ndarray, offset: np.ndarray, lam: float, iters: int = 30
) -> np.ndarray:
    """Logistic regression whose linear part starts from the market's log-odds
    (offset): the conditions can only move the market's probability; L2 penalty lam."""
    beta = np.zeros(x.shape[1])
    for _ in range(iters):
        p = _sigmoid(offset + x @ beta)
        grad = x.T @ (p - y) + lam * beta
        hess = (x * (p * (1 - p))[:, None]).T @ x + lam * np.eye(x.shape[1])
        step = np.linalg.solve(hess, grad)
        beta -= step
        if np.max(np.abs(step)) < 1e-8:
            break
    return beta


def _penalty(x: np.ndarray, y: np.ndarray, offset: np.ndarray, early: np.ndarray) -> float:
    """The L2 penalty that predicts the last third of the early seasons best."""
    fit, check = early[: len(early) * 2 // 3], early[len(early) * 2 // 3 :]
    scores = {}
    for lam in (1.0, 10.0, 100.0, 1000.0, 10000.0):
        beta = fit_offset_logit(x[fit], y[fit], offset[fit], lam)
        scores[lam] = _logloss(y[check], _sigmoid(offset[check] + x[check] @ beta))
    return min(scores, key=scores.__getitem__)


def model(board: Board) -> dict[str, Any]:
    """All conditions at once, per bet type. The penalty is chosen on the last third of
    the early seasons; the late seasons are scored once."""
    ll_market = ll_model = 0.0
    n_test = 0
    best_ev = -1.0
    picks = []
    for b in board.bets:
        d = board.data[b]
        stats, test = d["stats"], d["test"]
        y, p = stats[:, 1], stats[:, 2]
        offset = np.log(p / (1 - p))
        x = d["C"][:, d["C"][~test].sum(axis=0) >= 50].astype(float)
        lam = _penalty(x, y, offset, np.where(~test)[0])
        q = _sigmoid(offset + x @ fit_offset_logit(x[~test], y[~test], offset[~test], lam))
        ev = q * (1 / stats[:, 5 + 3 * len(LEVELS)]) - 1  # the 運彩 price
        ll_market += _logloss(y[test], p[test]) * test.sum()
        ll_model += _logloss(y[test], q[test]) * test.sum()
        n_test += int(test.sum())
        best_ev = max(best_ev, float(ev[test].max()))
        picks.append(stats[test & (ev >= EV_MIN)])
    chosen = np.vstack(picks)
    return {
        "test_logloss_market": round(ll_market / n_test, 5),
        "test_logloss_model": round(ll_model / n_test, 5),
        "test_max_ev_tw": round(best_ev * 100, 1),
        "test_bets_ev_min": as_score(sums_score(chosen.sum(axis=0))) if len(chosen) else {"n": 0},
    }


def owner(board: Board, sport: str) -> list[dict[str, Any]]:
    out = []
    for label, bet, conds in OWNER_COMBOS[sport]:
        d = board.data[bet]
        mask = np.all(d["C"][:, [board.col[c] for c in conds]], axis=1)
        row = {"label": label, "bet": bet}
        for period, sel in (
            ("all", mask),
            ("train", mask & ~d["test"]),
            ("test", mask & d["test"]),
        ):
            row[period] = as_score(sums_score(d["stats"][sel].sum(axis=0)))
        out.append(row)
    return out


# =========================================================================== main
def run(sport: str) -> dict[str, Any]:
    rows, strategies = mlb_games() if sport == "mlb" else football_games()
    board = Board(sport, rows, strategies)
    found = search(board)
    tr = sums_score(found["train"])
    te = sums_score(found["test"])
    m = len(found["bet"])
    early_tw = tr["roi_tw"] > 0
    null = NormalDist()
    summary = {
        "games": len(rows),
        "conditions": len(board.conds),
        "filters": m,
        "sizes": dict(Counter(str(len(c)) for c in found["conds"])),
        "train_z2": int((tr["z"] >= 2).sum()),
        "null_z2": round(m * (1 - null.cdf(2)), 1),
        "train_z3": int((tr["z"] >= 3).sum()),
        "null_z3": round(m * (1 - null.cdf(3)), 1),
        "bonferroni_z": round(null.inv_cdf(1 - 0.05 / m), 2),
        "train_tw_profit": int(early_tw.sum()),
        "train_tw_profit_test_profit": int((early_tw & (te["roi_tw"] > 0)).sum()),
        "train_tw_profit_test_roi": round(float(np.nanmean(te["roi_tw"][early_tw])) * 100, 1)
        if early_tw.any()
        else None,
        "rules": rules(board, found),
        "deciles": deciles(found),
        "votes": votes(board),
        "model": model(board),
        "owner": owner(board, sport),
    }
    print(
        f"{sport}: {m} filters; early z>=2 {summary['train_z2']} (chance {summary['null_z2']}); "
        f"early 運彩-profitable {summary['train_tw_profit']} -> late "
        f"{summary['train_tw_profit_test_profit']}; deciles corr {summary['deciles']['corr']}; "
        f"model log-loss {summary['model']['test_logloss_model']} vs market "
        f"{summary['model']['test_logloss_market']}, max EV 運彩 {summary['model']['test_max_ev_tw']}%",
        flush=True,
    )
    return summary


def main() -> int:
    summary = {
        "generated": datetime.now(UTC).date().isoformat(),
        "split": {"mlb": ["2021–2023", "2024–2025"],
                  "football": ["2019/20–2022/23", "2023/24–2026/27"]},
        "min_train": MIN_TRAIN,
        "sports": {sport: run(sport) for sport in ("mlb", "football")},
    }  # fmt: skip
    OUT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", OUT_PATH)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
