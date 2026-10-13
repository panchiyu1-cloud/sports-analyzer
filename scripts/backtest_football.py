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
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinja2 import Environment, select_autoescape  # noqa: E402

from sports_analyzer.config import DATA_DIR, DOCS_DIR, MODEL, ModelConfig  # noqa: E402
from sports_analyzer.history import HistoricalMatch, read_csv  # noqa: E402
from sports_analyzer.models import blend, market  # noqa: E402
from sports_analyzer.models import dixon_coles as dc  # noqa: E402

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


# ------------------------------------------------------------------- report
TEMPLATE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>回測報告</title>
<style>
:root{--bg:#f6f5f1;--card:#fff;--ink:#1d1d1b;--muted:#6b6a66;--line:#e4e2db;--good:#2f6f5e;--bad:#a63d2f;--warn-bg:#fbf3e2;--warn-ink:#6b4b12}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141513;--card:#1d1f1c;--ink:#ecebe6;--muted:#9b9a94;--line:#2c2e2a;--good:#7fc4ad;--bad:#e08a7c;--warn-bg:#3a3120;--warn-ink:#f0d9a8}}
:root[data-theme="dark"]{--bg:#141513;--card:#1d1f1c;--ink:#ecebe6;--muted:#9b9a94;--line:#2c2e2a;--good:#7fc4ad;--bad:#e08a7c;--warn-bg:#3a3120;--warn-ink:#f0d9a8}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif;line-height:1.55}
.wrap{max-width:1040px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.4rem}h2{font-size:1.05rem;margin-top:30px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:.86rem}
th,td{padding:7px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:500;font-size:.78rem}tr:last-child td{border-bottom:0}
.pos{color:var(--good)}.neg{color:var(--bad)}.muted{color:var(--muted);font-size:.85rem}.scroll{overflow-x:auto}a{color:var(--good)}
.verdict{background:var(--warn-bg);color:var(--warn-ink);border-radius:10px;padding:12px 16px;font-size:.95rem}
.verdict.pass{background:#e3f1ea;color:#1d4a3c}
</style></head><body><div class="wrap">
<h1>回測報告</h1>
<p class="muted">產生：{{ s.generated }} · 五大聯賽 · 逐週走勢（嚴格 out-of-sample）· 衰減 xi={{ s.config.xi }} · <a href="index.html">回到分析頁</a></p>
<div class="verdict {{ 'pass' if s.passed else '' }}"><b>結論</b>　{{ s.verdict }}</div>

<h2>1. 預測品質（log-loss，越低越好）</h2>
<div class="scroll"><table>
<tr><th>純模型 (Dixon-Coles)</th><th>賽前市場 (Pinnacle 週五)</th><th>收盤市場</th><th>混合 (模型+市場)</th><th>只校準市場 (無模型)</th><th>場數</th></tr>
<tr><td>{{ s.logloss.model }}</td><td>{{ s.logloss.market }}</td><td>{{ s.logloss.closing }}</td><td>{{ s.logloss.blend }}</td><td>{{ s.logloss.market_only }}</td><td>{{ s.logloss.n }}</td></tr>
</table></div>
<p class="muted">大小 2.5：純模型 {{ s.logloss_ou.model }} · 市場 {{ s.logloss_ou.market }} · 混合 {{ s.logloss_ou.blend }}（{{ s.logloss_ou.n }} 場）。模型能否預測盤口走向（模型偏差 vs 收盤變動的相關係數）：主 {{ s.line_move_corr.home }}、和 {{ s.line_move_corr.draw }}、客 {{ s.line_move_corr.away }}——接近 0 代表模型看不到市場稍後才納入的資訊。</p>

<h2>2. 混合機率的下注表現（每注 1 單位，賽前 Pinnacle 價）</h2>
<p class="muted"><b>CLV</b> = 以收盤公允機率衡量這張注的期望值。這是判斷有沒有真本事的標準；ROI 只有幾百注時誤差 ±5–10%，看 ROI 會被運氣騙。</p>
<div class="scroll"><table><tr><th>edge 門檻</th><th>注數</th><th>損益</th><th>ROI（±1se）</th><th>CLV（±1se）</th><th>選項分布</th></tr>
{% for t, b in s.strategies.blend_pinnacle.items() %}<tr><td>≥ {{ (t|float*100)|int }}pp</td><td>{{ b.n }}</td><td class="{{ 'pos' if b.units > 0 else 'neg' }}">{{ '%+.1f'|format(b.units) }}</td><td>{{ '%+.1f%%'|format(b.roi*100) if b.roi is not none else '—' }} (±{{ '%.1f%%'|format(b.roi_se*100) if b.roi_se else '—' }})</td><td class="{{ 'pos' if b.clv and b.clv > 0 else 'neg' }}">{{ '%+.2f%%'|format(b.clv*100) if b.clv is not none else '—' }} (±{{ '%.2f%%'|format(b.clv_se*100) if b.clv_se else '—' }})</td><td class="muted">{{ b.sel }}</td></tr>{% endfor %}
</table></div>
<p class="muted">同樣的注改用各家最佳賽前價：{% for t, b in s.strategies.blend_best.items() %}≥{{ (t|float*100)|int }}pp ROI {{ '%+.1f%%'|format(b.roi*100) if b.roi is not none else '—' }} / CLV {{ '%+.2f%%'|format(b.clv*100) if b.clv is not none else '—' }}；{% endfor %}</p>

<h2>3. 對照組</h2>
<div class="scroll"><table><tr><th>策略</th><th>注數</th><th>ROI</th><th>CLV</th></tr>
{% for t, b in s.strategies.raw_model.items() %}<tr><td>純模型 edge ≥ {{ (t|float*100)|int }}pp（Phase 1 作法）</td><td>{{ b.n }}</td><td class="{{ 'pos' if b.roi and b.roi > 0 else 'neg' }}">{{ '%+.1f%%'|format(b.roi*100) if b.roi is not none else '—' }}</td><td class="{{ 'pos' if b.clv and b.clv > 0 else 'neg' }}">{{ '%+.2f%%'|format(b.clv*100) if b.clv is not none else '—' }}</td></tr>{% endfor %}
{% for t, b in s.strategies.market_only.items() if b.n > 0 %}<tr><td>只校準市場、不用模型 edge ≥ {{ (t|float*100)|int }}pp</td><td>{{ b.n }}</td><td class="{{ 'pos' if b.roi and b.roi > 0 else 'neg' }}">{{ '%+.1f%%'|format(b.roi*100) if b.roi is not none else '—' }}</td><td class="{{ 'pos' if b.clv and b.clv > 0 else 'neg' }}">{{ '%+.2f%%'|format(b.clv*100) if b.clv is not none else '—' }}</td></tr>{% endfor %}
<tr><td>永遠買熱門</td><td>{{ s.baselines.always_favourite.n }}</td><td class="neg">{{ '%+.1f%%'|format(s.baselines.always_favourite.roi*100) }}</td><td class="neg">{{ '%+.2f%%'|format(s.baselines.always_favourite.clv*100) if s.baselines.always_favourite.clv is not none else '—' }}</td></tr>
{% for t, b in s.strategies.ou_blend.items() %}<tr><td>大小 2.5 混合 edge ≥ {{ (t|float*100)|int }}pp</td><td>{{ b.n }}</td><td class="{{ 'pos' if b.roi and b.roi > 0 else 'neg' }}">{{ '%+.1f%%'|format(b.roi*100) if b.roi is not none else '—' }}</td><td class="{{ 'pos' if b.clv and b.clv > 0 else 'neg' }}">{{ '%+.2f%%'|format(b.clv*100) if b.clv is not none else '—' }}</td></tr>{% endfor %}
</table></div>

<h2>4. 混合權重（走勢末端）</h2>
<p class="muted">a = 純模型權重，b = 市場權重（1 = 照市場），c = 和局／客勝截距。{{ s.blend_weights_final }}</p>

<h2>5. 各聯賽（混合 edge ≥ 3pp）</h2>
<div class="scroll"><table><tr><th>聯賽</th><th>場數</th><th>模型 ll</th><th>市場 ll</th><th>混合 ll</th><th>注數</th><th>ROI</th><th>CLV</th></tr>
{% for code, r in s.by_league.items() %}<tr><td>{{ r.name }}</td><td>{{ r.n }}</td><td>{{ r.logloss_model }}</td><td>{{ r.logloss_market }}</td><td>{{ r.logloss_blend }}</td><td>{{ r['blend_0.03'].n }}</td><td class="{{ 'pos' if r['blend_0.03'].roi and r['blend_0.03'].roi > 0 else 'neg' }}">{{ '%+.1f%%'|format(r['blend_0.03'].roi*100) if r['blend_0.03'].roi is not none else '—' }}</td><td class="{{ 'pos' if r['blend_0.03'].clv and r['blend_0.03'].clv > 0 else 'neg' }}">{{ '%+.2f%%'|format(r['blend_0.03'].clv*100) if r['blend_0.03'].clv is not none else '—' }}</td></tr>{% endfor %}
</table></div>
<p class="muted">資料：football-data.co.uk（賽前與收盤賠率，Pinnacle 優先、Bet365 備援）。</p>
</div></body></html>"""


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
    html = (
        Environment(autoescape=select_autoescape(["html"])).from_string(TEMPLATE).render(s=summary)
    )
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "backtest.html").write_text(html, encoding="utf-8")
    print("wrote docs/backtest.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
