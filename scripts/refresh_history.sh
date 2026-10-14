#!/usr/bin/env bash
# Refresh this season's football-data.co.uk CSVs (results, xG, shots, cards, odds).
#
# The model trains on them and the post-match review reads the match statistics
# from them. The site updates them a couple of times a week, so a daily download
# is enough. A failed or non-CSV download never replaces the file we already have.
set -uo pipefail

yy=$(date -u +%y)
month=$(date -u +%-m)
if [ "$month" -ge 7 ]; then
  season="${yy}$(printf %02d $((10#$yy + 1)))"
else
  season="$(printf %02d $((10#$yy - 1)))${yy}"
fi

mkdir -p data/history
for div in E0 SP1 D1 I1 F1; do
  url="https://www.football-data.co.uk/mmz4281/${season}/${div}.csv"
  out="data/history/${div}_${season}.csv"
  tmp="$(mktemp)"
  if curl -fsSL --retry 3 --max-time 60 -o "$tmp" "$url" && bash scripts/is_football_csv.sh "$tmp"; then
    mv "$tmp" "$out"
    echo "ok   $out ($(wc -l < "$out") lines)"
  else
    rm -f "$tmp"
    echo "keep $out (download failed or was not a CSV)"
  fi
done
