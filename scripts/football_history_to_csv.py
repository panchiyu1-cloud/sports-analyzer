"""Flatten football-data.co.uk season files into one gzipped CSV of results + odds.

    python scripts/football_history_to_csv.py <folder of downloaded CSVs> <out.csv.gz>

Keeps, per match, the result and the early ("opening", collected a few days before)
and closing odds that football-data.co.uk publishes since 2019/20: 1X2, over/under
2.5 goals and the Asian handicap, as market average, best price and Pinnacle.
File names must be <DIV>_<SEASON>.csv (e.g. E1_2324.csv). Used by the manual
"Football odds history" workflow; the downloads themselves are never committed.
"""

from __future__ import annotations

import csv
import gzip
import sys
from pathlib import Path

KEEP = (
    "Div Date Time HomeTeam AwayTeam FTHG FTAG FTR HTHG HTAG "
    "AvgH AvgD AvgA MaxH MaxD MaxA PSH PSD PSA B365H B365D B365A "
    "AvgCH AvgCD AvgCA MaxCH MaxCD MaxCA PSCH PSCD PSCA B365CH B365CD B365CA "
    "Avg>2.5 Avg<2.5 Max>2.5 Max<2.5 P>2.5 P<2.5 "
    "AvgC>2.5 AvgC<2.5 MaxC>2.5 MaxC<2.5 PC>2.5 PC<2.5 "
    "AHh AvgAHH AvgAHA MaxAHH MaxAHA PAHH PAHA "
    "AHCh AvgCAHH AvgCAHA MaxCAHH MaxCAHA PCAHH PCAHA"
).split()


def main(folder: Path, out: Path) -> None:
    rows = 0
    with gzip.open(out, "wt", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Season", *KEEP])
        writer.writeheader()
        for path in sorted(folder.glob("*_*.csv")):
            season = path.stem.split("_")[-1]
            text = path.read_bytes().decode("utf-8", errors="replace").lstrip("﻿")
            reader = csv.DictReader(text.splitlines())
            for raw in reader:
                if not raw.get("HomeTeam") or not raw.get("FTR"):
                    continue
                writer.writerow({"Season": season, **{k: raw.get(k, "") for k in KEEP}})
                rows += 1
    print(f"{rows} matches -> {out}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
