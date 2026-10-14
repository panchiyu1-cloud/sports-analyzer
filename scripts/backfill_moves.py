"""Rebuild the line-move tape (data/moves/) from the snapshot history in git.

    python scripts/backfill_moves.py [--since 2026-10-12]

Every committed version of data/snapshots/*/{football,mlb}.json is read in commit
order. Its international blocks go on the tape at their own odds_at (older snapshots
had none: the snapshot's generated_at stands in, as pipeline._backfill_odds_at does),
and its 運彩 blocks at their captured_at. moves.record drops what is already there,
so running it twice changes nothing.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sports_analyzer import moves  # noqa: E402
from sports_analyzer.config import MOVES_DIR  # noqa: E402


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def main(since: str) -> int:
    commits = git(
        "log", "--reverse", f"--since={since}", "--format=%H", "--", "data/snapshots"
    ).split()
    added = 0
    for sha in commits:
        for path in git("ls-tree", "-r", "--name-only", sha, "data/snapshots").split():
            if not path.endswith(("/football.json", "/mlb.json")):
                continue
            try:
                snap = json.loads(git("show", f"{sha}:{path}"))
            except (subprocess.CalledProcessError, ValueError):
                continue
            sport = "football" if path.endswith("football.json") else "mlb"
            cards = (
                [c for comp in snap.get("competitions", []) for c in comp.get("cards", [])]
                if sport == "football"
                else snap.get("games", [])
            )
            added += moves.record(
                MOVES_DIR, moves.intl_rows(cards, sport, snap.get("generated_at"))
            )
            added += moves.record(MOVES_DIR, moves.tw_rows(cards, sport))
    print(f"{len(commits)} commits read, {added} prices added to {MOVES_DIR}")
    return added


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-10-12")
    main(parser.parse_args().since)
