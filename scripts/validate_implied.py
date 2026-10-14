"""Validate (and calibrate) the market-implied goal model on football-data.co.uk history.

For every match with closing 1X2 and over/under 2.5 prices (Pinnacle where the
season has them, else Betfair Exchange), fit `models.implied` and check:

1. Market vs market: its Asian handicap price against the same book's closing
   Asian handicap price (a market it was not fitted to).
2. Results: its probabilities for the derived markets 運彩 sells against what
   happened, raw and after a per-market logistic calibration. The calibration is
   judged out of sample: fitted on one season, scored on another, both ways.

The final calibration (fitted on every season) goes to data/implied_calibration.json,
the report to data/implied_validation.json.

Usage:  python scripts/validate_implied.py [data/history/*.csv]
"""

from __future__ import annotations

import csv
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.optimize import minimize

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sports_analyzer.models.implied import Calibration, fit_implied  # noqa: E402
from sports_analyzer.models.market import devig  # noqa: E402

BOOKS = (
    # (1X2 closing, over/under 2.5 closing, Asian handicap closing)
    (("PSCH", "PSCD", "PSCA"), ("PC>2.5", "PC<2.5"), ("PCAHH", "PCAHA")),
    (("BFECH", "BFECD", "BFECA"), ("BFEC>2.5", "BFEC<2.5"), ("BFECAHH", "BFECAHA")),
)


def fair(prices: list[float | None]) -> list[float] | None:
    if any(p is None or p <= 1.0 for p in prices):
        return None
    raw = [1 / p for p in prices]  # type: ignore[operator]
    if sum(raw) <= 1.0:  # an exchange can close below 100%: just normalise
        return [r / sum(raw) for r in raw]
    return devig(prices, "power")  # type: ignore[arg-type]


def num(row: dict[str, str], key: str) -> float | None:
    try:
        return float(row.get(key) or "")
    except ValueError:
        return None


def load(paths: list[str]) -> list[dict]:
    out = []
    for path in paths:
        season = Path(path).stem.split("_")[-1]
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                if "" in (
                    row.get("FTHG", ""),
                    row.get("FTAG", ""),
                    row.get("HTHG", ""),
                    row.get("HTAG", ""),
                ):
                    continue
                for h2h_cols, ou_cols, ah_cols in BOOKS:
                    f1 = fair([num(row, c) for c in h2h_cols])
                    f2 = fair([num(row, c) for c in ou_cols])
                    if not f1 or not f2:
                        continue
                    out.append(
                        {
                            "season": season,
                            "h2h": f1,
                            "over25": f2[0],
                            "ah_line": num(row, "AHCh"),
                            "ah": fair([num(row, c) for c in ah_cols]),
                            "fh": int(float(row["FTHG"])),
                            "fa": int(float(row["FTAG"])),
                            "hh": int(float(row["HTHG"])),
                            "ha": int(float(row["HTAG"])),
                        }
                    )
                    break
    return out


def derived(m: dict, share: float) -> list[tuple[str, str, float, float]]:
    """(calibration kind, report label, raw probability, outcome) for one match."""
    g = fit_implied(*m["h2h"], m["over25"])
    fh, fa, hh, ha = m["fh"], m["fa"], m["hh"], m["ha"]
    total = fh + fa
    rows = []
    for line in (1.5, 3.5, 4.5):
        rows.append(
            (f"total_over@{line:g}", f"大小 {line}（大）", g.total_over(line), float(total > line))
        )
    for side, goals, name in (("home", fh, "主隊"), ("away", fa, "客隊")):
        for line in (0.5, 1.5, 2.5):
            rows.append(
                (
                    f"team_over_{side}@{line:g}",
                    f"{name}大小 {line}（大）",
                    g.team_over(side, line),
                    float(goals > line),
                )
            )
    rows.append(("btts", "兩隊都進球（是）", g.both_score(), float(fh > 0 and fa > 0)))
    rows.append(("odd", "總進球單數", g.odd_total(), float(total % 2 == 1)))
    for hcp, label in ((-1, "讓分 1:0"), (1, "讓分 0:1"), (-2, "讓分 2:0"), (2, "讓分 0:2")):
        h, d, a = g.handicap3(hcp)
        margin = fh - fa + hcp
        rows.append((f"hcp3_home@{hcp:g}", f"{label} 主", h, float(margin > 0)))
        rows.append((f"hcp3_draw@{hcp:g}", f"{label} 和", d, float(margin == 0)))
        rows.append((f"hcp3_away@{hcp:g}", f"{label} 客", a, float(margin < 0)))
    half = g.first_half(share)
    h, d, a = half.p_1x2()
    rows.append(("ht_home", "上半場 主勝", h, float(hh > ha)))
    rows.append(("ht_draw", "上半場 和局", d, float(hh == ha)))
    rows.append(("ht_away", "上半場 客勝", a, float(hh < ha)))
    for line in (0.5, 1.5, 2.5):
        rows.append(
            (
                f"ht_over@{line:g}",
                f"上半場大小 {line}（大）",
                half.total_over(line),
                float(hh + ha > line),
            )
        )
    return rows


def fit_platt(pairs: list[tuple[float, float]]) -> tuple[float, float]:
    """logit(p) -> a + b logit(p) minimising log-loss (identity start, mild ridge)."""
    p = np.clip(np.array([x for x, _ in pairs]), 1e-4, 1 - 1e-4)
    y = np.array([o for _, o in pairs])
    z = np.log(p / (1 - p))

    def loss(v: np.ndarray) -> float:
        q = 1 / (1 + np.exp(-(v[0] + v[1] * z)))
        q = np.clip(q, 1e-9, 1 - 1e-9)
        ridge = 0.5 * (v[0] ** 2 + (v[1] - 1) ** 2)  # towards "no correction"
        return float(-(y * np.log(q) + (1 - y) * np.log(1 - q)).sum() + ridge)

    res = minimize(loss, np.array([0.0, 1.0]), method="L-BFGS-B")
    return float(res.x[0]), float(res.x[1])


def score(pairs: list[tuple[float, float]]) -> dict[str, float]:
    n = len(pairs)
    pred = sum(p for p, _ in pairs) / n
    actual = sum(o for _, o in pairs) / n
    ordered = sorted(pairs)
    worst = 0.0
    for k in range(5):
        chunk = ordered[k * n // 5 : (k + 1) * n // 5]
        if chunk:
            gap = sum(p for p, _ in chunk) / len(chunk) - sum(o for _, o in chunk) / len(chunk)
            worst = max(worst, abs(gap))
    ll = (
        -sum(o * math.log(max(p, 1e-9)) + (1 - o) * math.log(max(1 - p, 1e-9)) for p, o in pairs)
        / n
    )
    return {
        "n": n,
        "diff_pp": round(100 * (pred - actual), 2),
        "worst_bin_pp": round(100 * worst, 2),
        "two_se_pp": round(200 * math.sqrt(max(actual * (1 - actual), 1e-9) / n), 2),
        "log_loss": round(ll, 5),
    }


def main(paths: list[str]) -> dict:
    matches = load(paths)
    share = sum(m["hh"] + m["ha"] for m in matches) / sum(m["fh"] + m["fa"] for m in matches)
    seasons = sorted({m["season"] for m in matches})
    print(f"{len(matches)} matches, seasons {seasons}; first-half share of goals {share:.3f}")

    ah = []
    rows_by_season: dict[str, list[tuple[str, str, float, float]]] = defaultdict(list)
    for m in matches:
        if m["ah"] is not None and m["ah_line"] is not None:
            g = fit_implied(*m["h2h"], m["over25"])
            ah.append(g.asian_home(m["ah_line"]) - m["ah"][0])
        rows_by_season[m["season"]].extend(derived(m, share))
    abs_ah = sorted(abs(e) for e in ah)
    report: dict = {
        "matches": len(matches),
        "seasons": seasons,
        "first_half_share": round(share, 4),
        "asian_handicap": {
            "n": len(ah),
            "mae_pp": round(100 * sum(abs_ah) / len(abs_ah), 2),
            "bias_pp": round(100 * sum(ah) / len(ah), 2),
            "p90_pp": round(100 * abs_ah[int(0.9 * len(abs_ah))], 2),
        },
        "markets": {},
    }
    print(
        "Asian handicap vs closing price: n={n} MAE {mae_pp}pp bias {bias_pp:+}pp p90 {p90_pp}pp".format(
            **report["asian_handicap"]
        )
    )

    kinds = sorted({k for rows in rows_by_season.values() for k, _, _, _ in rows})
    # Out of sample: calibrate on every other season, score on this one.
    oos_raw: dict[str, list] = defaultdict(list)
    oos_cal: dict[str, list] = defaultdict(list)
    big = [s for s in seasons if len(rows_by_season[s]) > 1000]
    for test in big:
        train = [r for s in seasons if s != test for r in rows_by_season[s]]
        cal = Calibration(
            {k: fit_platt([(p, o) for kk, _, p, o in train if kk == k]) for k in kinds}
        )
        for k, label, p, o in rows_by_season[test]:
            oos_raw[label].append((p, o))
            oos_cal[label].append((cal.apply(k, p), o))
    print(f"\nout of sample (each of {big} scored with a calibration fitted on the others)")
    print(
        f"{'market':16s} {'n':>5s} {'raw diff':>8s} {'raw bin':>7s} {'cal diff':>8s} {'cal bin':>7s} {'±2se':>5s} {'LL gain':>8s}"
    )
    label_of = {k: label for rows in rows_by_season.values() for k, label, _, _ in rows}
    helps: list[str] = []
    for key, label in label_of.items():
        r, c = score(oos_raw[label]), score(oos_cal[label])
        gain = r["log_loss"] - c["log_loss"]
        # Keep a correction only where it beat the raw price out of sample.
        if gain > 0:
            helps.append(key)
        report["markets"][label] = {"key": key, "raw": r, "calibrated": c, "kept": gain > 0}
        print(
            f"{label:16s} {r['n']:5d} {r['diff_pp']:+8.1f} {r['worst_bin_pp']:7.1f} "
            f"{c['diff_pp']:+8.1f} {c['worst_bin_pp']:7.1f} {r['two_se_pp']:5.1f} {gain:+8.4f}"
            + ("  kept" if gain > 0 else "")
        )

    final = {
        k: [
            round(v, 4)
            for v in fit_platt(
                [(p, o) for rows in rows_by_season.values() for kk, _, p, o in rows if kk == k]
            )
        ]
        for k in kinds
        if k in helps
    }
    (ROOT / "data" / "implied_calibration.json").write_text(
        json.dumps({"fitted_on": seasons, "matches": len(matches), "params": final}, indent=1)
        + "\n",
        encoding="utf-8",
    )
    (ROOT / "data" / "implied_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print("\ncalibration:", final)
    return report


if __name__ == "__main__":
    main(sys.argv[1:] or [str(p) for p in sorted((ROOT / "data" / "history").glob("*.csv"))])
