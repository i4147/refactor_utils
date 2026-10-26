#!/usr/bin/env bash
# git_helpers.sh — Git/PR helpers used during arena session
# Usage: source ./tools/arena/git_helpers.sh  OR  ./tools/arena/git_helpers.sh <cmd>
# Commands: status, log, branch, pr-create, pr-merge, push, fetch, reset-main
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

cmd="${1:-help}"
case "$cmd" in
  status)
    git status --porcelain | head -n 100
    git diff --stat HEAD 2>&1 | head -n 50
    ;;
  log)
    git log --oneline --graph --all -10
    ;;
  branch)
    git branch -vv
    gh auth status 2>&1 | head -10
    ;;
  pr-create)
    # $2=title $3=body
    TITLE="${2:-Extend pyrefactor CLI & harden edge-case handling (prompt.md + data file support)}"
    BODY="${3:-See README for flag tables, edge cases, impl plan, test matrix.}"
    gh pr create --title "$TITLE" --body "$BODY" --base main 2>&1 | tail
    ;;
  pr-view)
    gh pr view "${2:-1}" --json number,title,state,baseRefName,headRefName,mergeable 2>&1
    ;;
  pr-merge)
    gh pr merge "${2:-1}" --merge 2>&1 | tail
    ;;
  push)
    BRANCH="$(git rev-parse --abbrev-ref HEAD)"
    git push origin "$BRANCH" 2>&1 | tail -20
    ;;
  fetch)
    git fetch origin 2>&1 | head
    git log origin/main --oneline -3
    ;;
  reset-main)
    # Reset current branch to main (used to bring prompt.md implementation)
    git reset --hard main 2>&1 | tail
    ;;
  help|*)
    echo "Usage: $0 <status|log|branch|pr-create|pr-view|pr-merge|push|fetch|reset-main>"
    echo "  pr-create [title] [body] — gh pr create"
    echo "  pr-view [num]            — gh pr view"
    echo "  pr-merge [num]           — gh pr merge --merge (last command must be merge/close per arena rules)"
    echo "  push                     — git push origin <current-branch>"
    echo "  status/log/branch/fetch/reset-main"
    ;;
esac
