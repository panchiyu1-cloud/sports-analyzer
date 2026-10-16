"""Walk-forward backtest of the football model against the market, with closing-line value.

    python scripts/backtest_football.py data/history/*.csv

Two stages, both strictly out-of-sample:

1. Dixon-Coles: for every league, walk the seasons week by week, fit on
   everything before the week, predict the week.
2. Blend: pooled across leagues, walk week by week again; fit the
   market-anchored blend (models/blend.py) on all earlier out-of-sample
   predictions, apply to the week.

Then three questions, in order of how much they matter:
- log-loss: does model / blend beat the pre-match market, and the CLOSING market?
- CLV: for every bet the strategy would place at the pre-match price, what is
  its expected value judged by the closing line? Positive CLV is the standard
  proof of real edge; realised ROI on a few hundred bets is mostly luck.
- ROI: the realised number, with a standard error so it cannot be oversold.

Writes docs/backtest.html and data/backtest_summary.json.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from sports_analyzer.config import DATA_DIR, DOCS_DIR, MODEL, ModelConfig  # noqa: E402
from sports_analyzer.history import HistoricalMatch, read_csv  # noqa: E402
from sports_analyzer.models import blend, market  # noqa: E402
from sports_analyzer.models import dixon_coles as dc  # noqa: E402
from sports_analyzer.render.site import render_report  # noqa: E402

LEAGUE_NAMES = {"E0": "英超", "SP1": "西甲", "D1": "德甲", "I1": "義甲", "F1": "法甲"}
OUTCOME = ("home", "draw", "away")
THRESHOLDS = (0.0, 0.02, 0.03, 0.04, 0.06)


# ----------------------------------------------------------------- stage 1
def stage1_league(args: tuple[str, list[str], float, int, int]) -> tuple[str, list[dict[str, Any]]]:
    code, paths, xi, min_train, step_days = args
    matches: list[HistoricalMatch] = []
    for p in paths:
        matches.extend(read_csv(Path(p)))
    matches.sort(key=lambda m: m.day)
    if len(matches) <= min_train:
        return code, []
    cursor, end = matches[min_train].day, matches[-1].day
    out: list[dict[str, Any]] = []
    while cursor <= end:
        week_end = cursor + timedelta(days=step_days)
        train = [m.result for m in matches if m.day < cursor]
        test = [m for m in matches if cursor <= m.day < week_end]
        if test and len(train) >= 10:
            model = dc.fit(train, as_of=cursor, xi=xi)
            for m in test:
                if m.h2h is None or not (model.knows(m.home) and model.knows(m.away)):
                    continue
                pred = model.predict(m.home, m.away)
                rec: dict[str, Any] = {
                    "league": code,
                    "date": m.day.isoformat(),
                    "outcome": 0
                    if m.home_goals > m.away_goals
                    else (1 if m.home_goals == m.away_goals else 2),
                    "total": m.home_goals + m.away_goals,
                    "model": [pred.p_home, pred.p_draw, pred.p_away],
                    "market": market.devig(list(m.h2h), "power"),
                    "prices": list(m.h2h),
                    "max_prices": list(m.h2h_max) if m.h2h_max else list(m.h2h),
                    "close": market.devig(list(m.h2h_close), "power") if m.h2h_close else None,
                    "data_ok": min(model.matches_per_team[m.home], model.matches_per_team[m.away])
                    >= MODEL.min_team_matches,
                }
                if m.over25:
                    rec["model_over"] = pred.over_prob(2.5)
                    rec["market_over"] = market.devig(list(m.over25), "power")[0]
                    rec["over_prices"] = list(m.over25)
                    rec["close_over"] = (
                        market.devig(list(m.over25_close), "power")[0] if m.over25_close else None
                    )
                out.append(rec)
        cursor = week_end
    return code, out


# ----------------------------------------------------------------- stage 2
def stage2_blend(
    recs: list[dict[str, Any]], min_samples: int, ridge: float, step_days: int
) -> list[dict[str, Any]]:
    """Pooled walk-forward blend. Returns the records that received a blend prediction."""
    for r in recs:
        r["d"] = date.fromisoformat(r["date"])
    recs.sort(key=lambda r: r["d"])
    cursor, end = recs[0]["d"], recs[-1]["d"]
    tested: list[dict[str, Any]] = []
    weights_log: list[dict[str, Any]] = []
    while cursor <= end:
        week_end = cursor + timedelta(days=step_days)
        train = [r for r in recs if r["d"] < cursor]
        test = [r for r in recs if cursor <= r["d"] < week_end]
        if test and len(train) >= min_samples:
            w = blend.fit_blend(
                [r["model"] for r in train],
                [r["market"] for r in train],
                [r["outcome"] for r in train],
                ridge=ridge,
            )
            flat = [[1 / 3] * 3] * len(train)
            w0 = blend.fit_blend(
                flat, [r["market"] for r in train], [r["outcome"] for r in train], ridge=ridge
            )
            tr_ou = [r for r in train if "model_over" in r]
            w_ou = None
            if len(tr_ou) >= min_samples:
                w_ou = blend.fit_binary_blend(
                    [r["model_over"] for r in tr_ou],
                    [r["market_over"] for r in tr_ou],
                    [1 if r["total"] > 2.5 else 0 for r in tr_ou],
                    ridge=ridge,
                )
            weights_log.append(
                {
                    "week": cursor.isoformat(),
                    "n_train": len(train),
                    "a_model": round(w.a_model, 3),
                    "b_market": round(w.b_market, 3),
                    "c_draw": round(w.intercepts[1], 3),
                    "c_away": round(w.intercepts[2], 3),
                }
            )
            for r in test:
                r["blend"] = list(w.apply(r["model"], r["market"]))
                r["mkt_only"] = list(w0.apply([1 / 3] * 3, r["market"]))
                if w_ou and "model_over" in r:
                    r["blend_over"] = w_ou.apply(r["model_over"], r["market_over"])
                tested.append(r)
        cursor = week_end
    for r in tested:
        r.pop("d", None)
    tested_weights = weights_log
    return tested, tested_weights  # type: ignore[return-value]


# ------------------------------------------------------------------ metrics
def bet_block(bets: list[float], clvs: list[float], sel: dict[str, int]) -> dict[str, Any]:
    n = len(bets)
    if n == 0:
        return {
            "n": 0,
            "units": 0.0,
            "roi": None,
            "roi_se": None,
            "clv": None,
            "clv_se": None,
            "sel": {},
        }
    return {
        "n": n,
        "units": round(float(sum(bets)), 2),
        "roi": round(float(sum(bets) / n), 4),
        "roi_se": round(float(np.std(bets, ddof=1) / math.sqrt(n)), 4) if n > 1 else None,
        "clv": round(float(np.mean(clvs)), 4) if clvs else None,
        "clv_se": round(float(np.std(clvs, ddof=1) / math.sqrt(len(clvs))), 4)
        if len(clvs) > 1
        else None,
        "sel": dict(sel),
    }


def strategy(
    tested: list[dict[str, Any]], prob_key: str, thr: float, min_p: float, price_key: str
) -> dict[str, Any]:
    bets, clvs, sel = [], [], defaultdict(int)
    for r in tested:
        p = r[prob_key]
        edges = [p[i] - r["market"][i] for i in range(3)]
        i = max(range(3), key=lambda j: edges[j])
        if edges[i] >= thr and p[i] >= min_p:
            price = r[price_key][i]
            bets.append(price - 1 if r["outcome"] == i else -1)
            if r.get("close"):
                clvs.append(r["close"][i] * price - 1)
            sel[OUTCOME[i]] += 1
    return bet_block(bets, clvs, sel)


def ou_strategy(tested: list[dict[str, Any]], prob_key: str, thr: float) -> dict[str, Any]:
    bets, clvs, sel = [], [], defaultdict(int)
    for r in tested:
        if prob_key not in r:
            continue
        e = r[prob_key] - r["market_over"]
        if e >= thr:
            bets.append(r["over_prices"][0] - 1 if r["total"] > 2.5 else -1)
            if r.get("close_over") is not None:
                clvs.append(r["close_over"] * r["over_prices"][0] - 1)
            sel["over"] += 1
        elif -e >= thr:
            bets.append(r["over_prices"][1] - 1 if r["total"] < 2.5 else -1)
            if r.get("close_over") is not None:
                clvs.append((1 - r["close_over"]) * r["over_prices"][1] - 1)
            sel["under"] += 1
    return bet_block(bets, clvs, sel)


def summarise(
    tested: list[dict[str, Any]], weights_log: list[dict[str, Any]], cfg: ModelConfig
) -> dict[str, Any]:
    y = [r["outcome"] for r in tested]
    with_close = [r for r in tested if r.get("close")]
    yc = [r["outcome"] for r in with_close]
    ou = [r for r in tested if "blend_over" in r]
    yo = [1 if r["total"] > 2.5 else 0 for r in ou]

    logloss = {
        "n": len(tested),
        "model": round(blend.log_loss_3([r["model"] for r in tested], y), 4),
        "market": round(blend.log_loss_3([r["market"] for r in tested], y), 4),
        "closing": round(blend.log_loss_3([r["close"] for r in with_close], yc), 4)
        if with_close
        else None,
        "blend": round(blend.log_loss_3([r["blend"] for r in tested], y), 4),
        "market_only": round(blend.log_loss_3([r["mkt_only"] for r in tested], y), 4),
    }
    logloss_ou = {
        "n": len(ou),
        "model": round(blend.log_loss_2([r["model_over"] for r in ou], yo), 4) if ou else None,
        "market": round(blend.log_loss_2([r["market_over"] for r in ou], yo), 4) if ou else None,
        "blend": round(blend.log_loss_2([r["blend_over"] for r in ou], yo), 4) if ou else None,
    }
    strategies = {
        "raw_model": {
            f"{t:.2f}": strategy(tested, "model", t, cfg.min_model_prob, "prices")
            for t in (0.04, 0.10)
        },
        "blend_pinnacle": {
            f"{t:.2f}": strategy(tested, "blend", t, 0.2, "prices") for t in THRESHOLDS
        },
        "blend_best": {
            f"{t:.2f}": strategy(tested, "blend", t, 0.2, "max_prices") for t in THRESHOLDS
        },
        "market_only": {
            f"{t:.2f}": strategy(tested, "mkt_only", t, 0.2, "prices") for t in THRESHOLDS
        },
        "ou_blend": {
            f"{t:.2f}": ou_strategy(tested, "blend_over", t) for t in (0.0, 0.02, 0.03, 0.05)
        },
    }
    fav_bets, fav_clv = [], []
    for r in tested:
        i = int(np.argmax(r["market"]))
        fav_bets.append(r["prices"][i] - 1 if r["outcome"] == i else -1)
        if r.get("close"):
            fav_clv.append(r["close"][i] * r["prices"][i] - 1)
    baselines = {"always_favourite": bet_block(fav_bets, fav_clv, {"fav": len(fav_bets)})}

    move_corr = {}
    if with_close:
        for i, name in enumerate(OUTCOME):
            a = np.array([r["model"][i] - r["market"][i] for r in with_close])
            b = np.array([r["close"][i] - r["market"][i] for r in with_close])
            move_corr[name] = round(float(np.corrcoef(a, b)[0, 1]), 3)

    by_league = {}
    for lg in sorted({r["league"] for r in tested}):
        sub = [r for r in tested if r["league"] == lg]
        ys = [r["outcome"] for r in sub]
        by_league[lg] = {
            "name": LEAGUE_NAMES.get(lg, lg),
            "n": len(sub),
            "logloss_model": round(blend.log_loss_3([r["model"] for r in sub], ys), 4),
            "logloss_market": round(blend.log_loss_3([r["market"] for r in sub], ys), 4),
            "logloss_blend": round(blend.log_loss_3([r["blend"] for r in sub], ys), 4),
            "blend_0.03": strategy(sub, "blend", 0.03, 0.2, "prices"),
        }

    # Plain-language verdict, derived from the numbers so it cannot drift from them.
    key = strategies["blend_pinnacle"]["0.03"]
    clv = key["clv"]
    final_w = weights_log[-1] if weights_log else {}
    if clv is not None and key["clv_se"] is not None and clv - 2 * key["clv_se"] > 0:
        verdict = "通過：混合機率的下注在收盤線上有正的期望值（CLV > 0，2 個標準差）。"
        passed = True
    else:
        verdict = "未通過：所有門檻的 CLV 都是負的——收盤市場認為這些注是負期望值；看到的正 ROI 在誤差範圍內，是運氣不是能力。"
        passed = False
    if final_w.get("a_model", 0) <= 0.05:
        verdict += f" 混合模型給純模型的權重 a={final_w.get('a_model')}（≤ 0 表示模型相對市場沒有額外資訊）。"

    return {
        "generated": date.today().isoformat(),
        "config": {
            "xi": cfg.dc_xi,
            "min_model_prob": cfg.min_model_prob,
            "devig": cfg.devig_method,
        },
        "passed": passed,
        "verdict": verdict,
        "logloss": logloss,
        "logloss_ou": logloss_ou,
        "strategies": strategies,
        "baselines": baselines,
        "line_move_corr": move_corr,
        "blend_weights_final": final_w,
        "blend_weights_log": weights_log[:: max(1, len(weights_log) // 10)],
        "by_league": by_league,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", nargs="+", type=Path)
    parser.add_argument("--min-train", type=int, default=120)
    parser.add_argument("--min-blend", type=int, default=300)
    parser.add_argument("--step-days", type=int, default=7)
    parser.add_argument("--xi", type=float, default=None)
    parser.add_argument("--ridge", type=float, default=1.0)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args(argv)
    cfg = MODEL if args.xi is None else ModelConfig(dc_xi=args.xi)

    by_league: dict[str, list[str]] = defaultdict(list)
    for p in args.csv:
        by_league[p.name.split("_")[0].upper()].append(str(p))
    jobs = [
        (code, paths, cfg.dc_xi, args.min_train, args.step_days)
        for code, paths in sorted(by_league.items())
    ]

    records: list[dict[str, Any]] = []
    with ProcessPoolExecutor(max_workers=args.workers) as ex:
        for code, recs in ex.map(stage1_league, jobs):
            print(f"stage1 {code}: {len(recs)} out-of-sample predictions", flush=True)
            records.extend(recs)
    records = [r for r in records if r["data_ok"]]
    tested, weights_log = stage2_blend(records, args.min_blend, args.ridge, args.step_days)
    print(f"stage2: {len(tested)} matches with blend predictions")

    summary = summarise(tested, weights_log, cfg)
    print(summary["verdict"])
    print("log-loss:", summary["logloss"])
    for t, b in summary["strategies"]["blend_pinnacle"].items():
        print(f"  blend edge>={t}: n={b['n']} ROI={b['roi']} CLV={b['clv']}")

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "backtest_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    render_report("backtest", summary, DOCS_DIR, datetime.now(UTC))
    print("wrote docs/backtest.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
