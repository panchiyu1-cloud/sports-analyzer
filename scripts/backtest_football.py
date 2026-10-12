"""Rolling backtest of the Dixon-Coles model against closing odds.

    python scripts/backtest_football.py data/history/E0_2425.csv data/history/E0_2526.csv ...

For every league it walks the season week by week: fit on everything before the
week, predict the week's matches, compare with the de-vigged closing odds
(Pinnacle if present, else Bet365) and record what a flat 1-unit bet on every
edge >= threshold would have returned.

Outputs docs/backtest.html and data/backtest_summary.json. The honest headline
is the *log-loss* comparison: if the model's log-loss is not close to the
market's, the edges it reports are mostly noise.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from jinja2 import Environment, select_autoescape  # noqa: E402

from sports_analyzer.config import DATA_DIR, DOCS_DIR, MODEL, ModelConfig  # noqa: E402
from sports_analyzer.history import HistoricalMatch, read_csv  # noqa: E402
from sports_analyzer.models import dixon_coles as dc  # noqa: E402
from sports_analyzer.models import market  # noqa: E402

LEAGUE_NAMES = {"E0": "英超", "SP1": "西甲", "D1": "德甲", "I1": "義甲", "F1": "法甲"}


def run_league(code: str, matches: list[HistoricalMatch], cfg: ModelConfig, min_train: int, step_days: int) -> dict[str, Any]:
    matches = sorted(matches, key=lambda m: m.day)
    if len(matches) <= min_train:
        return {"code": code, "name": LEAGUE_NAMES.get(code, code), "error": f"only {len(matches)} matches"}

    start = matches[min_train].day
    end = matches[-1].day
    cursor = start
    stats = _Stats()
    fits = 0
    while cursor <= end:
        week_end = cursor + timedelta(days=step_days)
        train = [m.result for m in matches if m.day < cursor]
        test = [m for m in matches if cursor <= m.day < week_end]
        if test and len(train) >= 10:
            model = dc.fit(train, as_of=cursor, xi=cfg.dc_xi, max_goals=cfg.dc_max_goals)
            fits += 1
            for m in test:
                if not (model.knows(m.home) and model.knows(m.away)):
                    continue
                stats.add(model.predict(m.home, m.away), m, cfg)
        cursor = week_end
    out = stats.summary()
    out.update({"code": code, "name": LEAGUE_NAMES.get(code, code), "fits": fits, "first_test_day": start.isoformat(), "last_day": end.isoformat()})
    return out


class _Stats:
    def __init__(self) -> None:
        self.n = 0
        self.ll_model = 0.0
        self.ll_market = 0.0
        self.brier_model = 0.0
        self.brier_market = 0.0
        self.bets_1x2: list[float] = []
        self.bets_ou: list[float] = []
        self.n_with_odds = 0
        self.by_edge: dict[str, list[float]] = defaultdict(list)

    def add(self, pred: dc.Prediction, m: HistoricalMatch, cfg: ModelConfig) -> None:
        outcome = 0 if m.home_goals > m.away_goals else (1 if m.home_goals == m.away_goals else 2)
        probs = [pred.p_home, pred.p_draw, pred.p_away]
        self.n += 1
        self.ll_model += -math.log(max(probs[outcome], 1e-12))
        self.brier_model += sum((p - (1 if i == outcome else 0)) ** 2 for i, p in enumerate(probs))
        if m.h2h is None:
            return
        self.n_with_odds += 1
        fair = market.devig(list(m.h2h), cfg.devig_method)
        self.ll_market += -math.log(max(fair[outcome], 1e-12))
        self.brier_market += sum((p - (1 if i == outcome else 0)) ** 2 for i, p in enumerate(fair))
        # Flat-stake strategy on the single best 1X2 edge.
        edges = [probs[i] - fair[i] for i in range(3)]
        best = max(range(3), key=lambda i: edges[i])
        if edges[best] >= cfg.edge_threshold and probs[best] >= cfg.min_model_prob:
            price = m.h2h[best]
            pnl = price - 1.0 if best == outcome else -1.0
            self.bets_1x2.append(pnl)
            bucket = "≥10pp" if edges[best] >= 0.10 else ("7–10pp" if edges[best] >= 0.07 else "4–7pp")
            self.by_edge[bucket].append(pnl)
        # Over/under 2.5 at the closing line.
        if m.over25:
            over_p = pred.over_prob(2.5)
            fair_ou = market.devig(list(m.over25), cfg.devig_method)
            total = m.home_goals + m.away_goals
            for side, mp, fp, price in (("over", over_p, fair_ou[0], m.over25[0]), ("under", 1 - over_p, fair_ou[1], m.over25[1])):
                if mp - fp >= cfg.edge_threshold and mp >= cfg.min_model_prob:
                    won = (total > 2.5) if side == "over" else (total < 2.5)
                    self.bets_ou.append(price - 1.0 if won else -1.0)

    def summary(self) -> dict[str, Any]:
        def roi(bets: list[float]) -> dict[str, Any]:
            if not bets:
                return {"n": 0, "units": 0.0, "roi": None}
            return {"n": len(bets), "units": round(sum(bets), 2), "roi": round(sum(bets) / len(bets), 4)}

        n_odds = max(self.n_with_odds, 1)
        return {
            "matches": self.n,
            "matches_with_odds": self.n_with_odds,
            "logloss_model": round(self.ll_model / max(self.n, 1), 4),
            "logloss_market": round(self.ll_market / n_odds, 4),
            "brier_model": round(self.brier_model / max(self.n, 1), 4),
            "brier_market": round(self.brier_market / n_odds, 4),
            "bets_1x2": roi(self.bets_1x2),
            "bets_ou25": roi(self.bets_ou),
            "by_edge": {k: roi(v) for k, v in sorted(self.by_edge.items())},
        }


TEMPLATE = """<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>回測報告</title>
<style>
:root{--bg:#f6f5f1;--card:#fff;--ink:#1d1d1b;--muted:#6b6a66;--line:#e4e2db;--good:#2f6f5e;--bad:#a63d2f}
@media(prefers-color-scheme:dark){:root:not([data-theme="light"]){--bg:#141513;--card:#1d1f1c;--ink:#ecebe6;--muted:#9b9a94;--line:#2c2e2a;--good:#7fc4ad;--bad:#e08a7c}}
:root[data-theme="dark"]{--bg:#141513;--card:#1d1f1c;--ink:#ecebe6;--muted:#9b9a94;--line:#2c2e2a;--good:#7fc4ad;--bad:#e08a7c}
body{margin:0;background:var(--bg);color:var(--ink);font-family:"Noto Sans TC","PingFang TC","Microsoft JhengHei",system-ui,sans-serif;line-height:1.5}
.wrap{max-width:1000px;margin:0 auto;padding:24px 16px 64px}h1{font-size:1.4rem}h2{font-size:1.05rem;margin-top:28px}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden;font-size:.9rem}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);white-space:nowrap}th{color:var(--muted);font-weight:500;font-size:.8rem}tr:last-child td{border-bottom:0}
.pos{color:var(--good)}.neg{color:var(--bad)}.muted{color:var(--muted);font-size:.85rem}.scroll{overflow-x:auto}a{color:var(--good)}
</style></head><body><div class="wrap">
<h1>回測報告</h1>
<p class="muted">產生：{{ generated }} · 門檻 edge ≥ {{ (cfg.edge_threshold*100)|int }}pp · 模型最低機率 {{ (cfg.min_model_prob*100)|int }}% · 衰減 xi={{ cfg.dc_xi }} · <a href="index.html">回到分析頁</a></p>
<h2>模型 vs 市場（越低越好）</h2>
<p class="muted">市場 = 收盤賠率去水後的機率（Pinnacle 優先）。收盤盤口是最難打敗的基準；模型 log-loss 若明顯高於市場，代表模型比市場差。</p>
<div class="scroll"><table><tr><th>聯賽</th><th>測試場數</th><th>模型 log-loss</th><th>市場 log-loss</th><th>差距</th><th>模型 Brier</th><th>市場 Brier</th><th>重新擬合次數</th></tr>
{% for r in leagues %}{% if r.error %}<tr><td>{{ r.name }}</td><td colspan="7" class="muted">{{ r.error }}</td></tr>{% else %}
<tr><td>{{ r.name }}</td><td>{{ r.matches_with_odds }}</td><td>{{ r.logloss_model }}</td><td>{{ r.logloss_market }}</td>
<td class="{{ 'pos' if r.logloss_model <= r.logloss_market else 'neg' }}">{{ '%+.4f'|format(r.logloss_model - r.logloss_market) }}</td>
<td>{{ r.brier_model }}</td><td>{{ r.brier_market }}</td><td>{{ r.fits }}</td></tr>{% endif %}{% endfor %}
</table></div>
<h2>平注策略（每注 1 單位，收盤價）</h2>
<div class="scroll"><table><tr><th>聯賽</th><th>1X2 注數</th><th>1X2 損益</th><th>1X2 ROI</th><th>大小2.5 注數</th><th>大小 損益</th><th>大小 ROI</th></tr>
{% for r in leagues if not r.error %}<tr><td>{{ r.name }}</td>
<td>{{ r.bets_1x2.n }}</td><td class="{{ 'pos' if r.bets_1x2.units > 0 else 'neg' }}">{{ '%+.2f'|format(r.bets_1x2.units) }}</td><td>{{ '%+.1f%%'|format(r.bets_1x2.roi*100) if r.bets_1x2.roi is not none else '—' }}</td>
<td>{{ r.bets_ou25.n }}</td><td class="{{ 'pos' if r.bets_ou25.units > 0 else 'neg' }}">{{ '%+.2f'|format(r.bets_ou25.units) }}</td><td>{{ '%+.1f%%'|format(r.bets_ou25.roi*100) if r.bets_ou25.roi is not none else '—' }}</td></tr>{% endfor %}
<tr><td><b>合計</b></td><td>{{ total.bets_1x2.n }}</td><td class="{{ 'pos' if total.bets_1x2.units > 0 else 'neg' }}"><b>{{ '%+.2f'|format(total.bets_1x2.units) }}</b></td><td>{{ '%+.1f%%'|format(total.bets_1x2.roi*100) if total.bets_1x2.roi is not none else '—' }}</td>
<td>{{ total.bets_ou25.n }}</td><td class="{{ 'pos' if total.bets_ou25.units > 0 else 'neg' }}"><b>{{ '%+.2f'|format(total.bets_ou25.units) }}</b></td><td>{{ '%+.1f%%'|format(total.bets_ou25.roi*100) if total.bets_ou25.roi is not none else '—' }}</td></tr>
</table></div>
<h2>1X2 依 edge 大小</h2>
<div class="scroll"><table><tr><th>edge 區間</th><th>注數</th><th>損益</th><th>ROI</th></tr>
{% for k, v in total.by_edge.items() %}<tr><td>{{ k }}</td><td>{{ v.n }}</td><td class="{{ 'pos' if v.units > 0 else 'neg' }}">{{ '%+.2f'|format(v.units) }}</td><td>{{ '%+.1f%%'|format(v.roi*100) if v.roi is not none else '—' }}</td></tr>{% endfor %}
</table></div>
<p class="muted">注意：樣本數少於幾百注時，ROI 的隨機波動可以輕易超過 ±10%。請看 log-loss 差距與注數，不要只看損益。</p>
</div></body></html>"""


def combine(leagues: list[dict[str, Any]]) -> dict[str, Any]:
    def merge(key: str) -> dict[str, Any]:
        n = sum(r[key]["n"] for r in leagues if not r.get("error"))
        units = sum(r[key]["units"] for r in leagues if not r.get("error"))
        return {"n": n, "units": round(units, 2), "roi": round(units / n, 4) if n else None}

    by_edge: dict[str, dict[str, Any]] = {}
    for r in leagues:
        for k, v in r.get("by_edge", {}).items():
            cur = by_edge.setdefault(k, {"n": 0, "units": 0.0})
            cur["n"] += v["n"]
            cur["units"] = round(cur["units"] + v["units"], 2)
    for v in by_edge.values():
        v["roi"] = round(v["units"] / v["n"], 4) if v["n"] else None
    return {"bets_1x2": merge("bets_1x2"), "bets_ou25": merge("bets_ou25"), "by_edge": dict(sorted(by_edge.items()))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", nargs="+", type=Path)
    parser.add_argument("--min-train", type=int, default=120, help="matches before the first prediction")
    parser.add_argument("--step-days", type=int, default=7)
    parser.add_argument("--xi", type=float, default=None, help="override decay")
    parser.add_argument("--edge", type=float, default=None, help="override edge threshold")
    args = parser.parse_args(argv)

    cfg = MODEL
    if args.xi is not None or args.edge is not None:
        cfg = ModelConfig(
            dc_xi=args.xi if args.xi is not None else MODEL.dc_xi,
            edge_threshold=args.edge if args.edge is not None else MODEL.edge_threshold,
        )

    by_league: dict[str, list[HistoricalMatch]] = defaultdict(list)
    for path in args.csv:
        code = path.name.split("_")[0].upper()
        rows = read_csv(path)
        by_league[code].extend(rows)
        print(f"{path.name}: {len(rows)} matches")

    leagues = [run_league(code, rows, cfg, args.min_train, args.step_days) for code, rows in sorted(by_league.items())]
    total = combine(leagues)
    for r in leagues:
        if r.get("error"):
            print(f"{r['name']}: {r['error']}")
        else:
            print(f"{r['name']}: n={r['matches_with_odds']} logloss model {r['logloss_model']} vs market {r['logloss_market']} | 1X2 bets {r['bets_1x2']['n']} ROI {r['bets_1x2']['roi']}")

    DATA_DIR.mkdir(exist_ok=True)
    (DATA_DIR / "backtest_summary.json").write_text(json.dumps({"generated": date.today().isoformat(), "config": cfg.__dict__, "leagues": leagues, "total": total}, ensure_ascii=False, indent=1), encoding="utf-8")
    env = Environment(autoescape=select_autoescape(["html"]))
    html = env.from_string(TEMPLATE).render(generated=date.today().isoformat(), cfg=cfg, leagues=leagues, total=total)
    DOCS_DIR.mkdir(exist_ok=True)
    (DOCS_DIR / "backtest.html").write_text(html, encoding="utf-8")
    print("wrote docs/backtest.html")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
