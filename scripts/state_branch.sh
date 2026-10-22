#!/usr/bin/env bash
# Persists the sent-signal registry between GitHub Actions runs on a dedicated
# orphan branch ("scanner-state"), so main stays free of runtime commits.
#   state_branch.sh load   -> writes state/sent_signals.json from the branch (if it exists)
#   state_branch.sh save   -> commits + pushes state/sent_signals.json to the branch
# Everything lives in functions and main() runs last: `save` swaps the working tree to
# the state branch, which would otherwise delete this script while bash is reading it.
set -euo pipefail

BRANCH="${STATE_BRANCH:-scanner-state}"
FILE="sent_signals.json"
DEST="state/${FILE}"

remote_has_branch() {
  git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1
}

load() {
  mkdir -p state
  if remote_has_branch; then
    git fetch --no-tags --depth=1 origin "$BRANCH"
    if git show "FETCH_HEAD:${FILE}" > "${DEST}.tmp" 2>/dev/null; then
      mv "${DEST}.tmp" "$DEST"
      echo "Loaded state from branch ${BRANCH}."
    else
      rm -f "${DEST}.tmp"
      echo "State branch exists but has no ${FILE} yet - starting empty."
    fi
  else
    echo "No ${BRANCH} branch yet - starting empty (it is created on the first save)."
  fi
}

save() {
  if [ ! -f "$DEST" ]; then
    echo "No ${DEST} to save (scanner produced no state)."
    return 0
  fi
  local tmp
  tmp="$(mktemp)"
  cp "$DEST" "$tmp"

  git config user.name  "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

  if remote_has_branch; then
    git fetch --no-tags --depth=1 origin "$BRANCH"
    git checkout -f -B "$BRANCH" FETCH_HEAD
  else
    git checkout --orphan "$BRANCH"
    git rm -rf --quiet . || true
  fi

  cp "$tmp" "$FILE"
  git add "$FILE"
  if git diff --cached --quiet; then
    echo "State unchanged - nothing to push."
  else
    git commit --quiet -m "scanner state $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git push origin "$BRANCH"
    echo "State pushed to ${BRANCH}."
  fi
}

main() {
  case "${1:-}" in
    load) load ;;
    save) save ;;
    *) echo "usage: $0 load|save" >&2; return 2 ;;
  esac
}

main "$@"
exit $?
