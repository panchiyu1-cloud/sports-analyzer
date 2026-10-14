#!/usr/bin/env bash
# Exit 0 when the file is a football-data.co.uk CSV (header starts with "Div").
#
# Since 2024/25 the site's files begin with a UTF-8 byte-order mark (3 bytes), so the
# header has to be looked for past it: checking only the first 4 bytes rejected every
# new file and the downloads silently kept the old ones (fixed 2026-10-14).
set -u
head -c 16 "$1" 2>/dev/null | grep -q "Div,"
