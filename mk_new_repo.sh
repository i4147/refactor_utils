#!/usr/bin/env bash
# apply-ai-zip.sh — extract ai.zip into a fresh branch, commit, optionally push.
# Usage:
#   ./apply-ai-zip.sh                 # branch "ai-version", push if origin exists
#   ./apply-ai-zip.sh -b ai-v2        # custom branch name
#   ./apply-ai-zip.sh -z other.zip    # custom zip
#   ./apply-ai-zip.sh --no-push       # local only
#   ./apply-ai-zip.sh --force         # auto-stash dirty tree, delete existing branch
#   ./apply-ai-zip.sh --keep-zip      # also commit ai.zip itself
set -euo pipefail

BRANCH="ai-version"
ZIP="ai.zip"
PUSH=1
FORCE=0
KEEP_ZIP=0

usage() { sed -n '2,10p' "$0"; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    -b|--branch)   BRANCH="$2"; shift 2 ;;
    -z|--zip)      ZIP="$2";    shift 2 ;;
    --no-push)     PUSH=0; shift ;;
    --force)       FORCE=1; shift ;;
    --keep-zip)    KEEP_ZIP=1; shift ;;
    -h|--help)     usage ;;
    *) echo "unknown arg: $1"; usage ;;
  esac
done

# --- sanity checks -----------------------------------------------------------
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || { echo "Not a git repo."; exit 1; }
ROOT="$(git rev-parse --show-toplevel)"
[[ "$(pwd -P)" == "$(cd "$ROOT" && pwd -P)" ]] || { echo "Run me from repo root: $ROOT"; exit 1; }
command -v unzip >/dev/null || { echo "unzip not installed"; exit 1; }
[[ -f "$ZIP" ]] || { echo "Missing $ZIP in repo root"; exit 1; }

# --- clean working tree (or stash) -------------------------------------------
if [[ -n "$(git status --porcelain)" ]]; then
  if [[ $FORCE -eq 1 ]]; then
    git stash push -u -m "pre-ai-zip $(date +%s)" >/dev/null
    echo "Stashed dirty changes."
  else
    echo "Working tree is dirty. Commit/stash first, or rerun with --force."; git status --short; exit 1
  fi
fi

BASE="$(git rev-parse --abbrev-ref HEAD)"
echo "Base branch: $BASE"

# --- create branch -----------------------------------------------------------
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
  if [[ $FORCE -eq 1 ]]; then
    git branch -D "$BRANCH"
    echo "Deleted existing branch $BRANCH (--force)."
  else
    echo "Branch '$BRANCH' already exists. Use -b <other-name> or --force."; exit 1
  fi
fi
git checkout -b "$BRANCH"

# --- extract to temp, strip any .git, then copy in ---------------------------
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
unzip -q "$ZIP" -d "$TMP"

# never let a rogue .git from the zip corrupt the repo
find "$TMP" -type d -name .git -prune -exec rm -rf {} + 2>/dev/null || true

top_count=$(ls -A "$TMP" | wc -l | tr -d ' ')
if [[ "$top_count" -eq 1 ]]; then
  only=$(ls -A "$TMP")
  if [[ -d "$TMP/$only" ]]; then
    echo "NOTE: zip has one top-level folder '$only' → files will land in ./$only"
    echo "      (If you wanted them at repo root, repackage the zip first.)"
  fi
fi

if command -v rsync >/dev/null 2>&1; then
  rsync -a "$TMP"/ ./
else
  (cd "$TMP" && tar cf - .) | tar xf -
fi

# --- stage + commit ----------------------------------------------------------
git add -A
if [[ $KEEP_ZIP -eq 0 ]]; then
  git reset -q -- "$ZIP" 2>/dev/null || true   # don't commit the zip by default
fi

if git diff --cached --quiet; then
  echo "No changes to commit — zip identical to current tree. Cleaning up."
  git checkout "$BASE"
  git branch -D "$BRANCH"
  exit 0
fi

git commit -m "AI-generated version from $ZIP"

# --- push --------------------------------------------------------------------
if [[ $PUSH -eq 1 ]]; then
  if git remote get-url origin >/dev/null 2>&1; then
    git push -u origin "$BRANCH"
  else
    echo "No 'origin' remote — skipped push."
  fi
fi

echo
echo "✔ Done. You're on branch: $BRANCH"
echo "  Compare:  git diff $BASE..$BRANCH --stat"
echo "  Back to base:  git checkout $BASE"