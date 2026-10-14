#!/usr/bin/env bash
# Commit generated files from a workflow run and push them to main.
#
#   scripts/commit_generated.sh "<commit message>" <path>...
#
# The phone uploads the football and baseball feeds a few seconds apart, so two
# runs can be in flight for the same minute. Generated files (snapshots, the
# page) never merge cleanly, so when another run pushed first we do not try to
# rebase them: we take the newer main as is and lay this run's outputs on top.
# This run started from everything the earlier one saw, so "ours wins" is right.
set -euo pipefail

msg="$1"
shift
# Only paths that exist: a step that wrote nothing (e.g. no finished games to review
# yet) must not make `git add` fail on a missing pathspec.
paths=()
for p in "$@"; do
  if [ -e "$p" ]; then
    paths+=("$p")
  fi
done
if [ "${#paths[@]}" -eq 0 ]; then
  echo "nothing to commit (none of the paths exist)"
  exit 0
fi

git config user.name "sports-analyzer bot"
git config user.email "actions@users.noreply.github.com"
git add -- "${paths[@]}"
if git diff --cached --quiet; then
  echo "nothing changed"
  exit 0
fi
git commit -m "$msg"

for attempt in 1 2 3; do
  if git pull --rebase origin main && git push origin HEAD:main; then
    exit 0
  fi
  echo "push attempt $attempt failed: another run pushed first; replaying our outputs on top of main"
  git rebase --abort 2>/dev/null || true
  keep="$(mktemp -d)"
  # The failed rebase may have left the tree half-way; take the files from our commit.
  for p in "${paths[@]}"; do
    git archive HEAD -- "$p" 2>/dev/null | tar -x -C "$keep" || true
  done
  git fetch origin main
  git reset --hard origin/main
  cp -R "$keep"/. .
  git add -- "${paths[@]}"
  if git diff --cached --quiet; then
    echo "main already has these outputs"
    exit 0
  fi
  git commit -m "$msg"
done
echo "could not push after 3 attempts" >&2
exit 1
