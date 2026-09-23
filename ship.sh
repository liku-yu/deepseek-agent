#!/usr/bin/env bash
# Ship workflow: commit -> push to GitHub + cnb.cool -> rebuild the single-file exe.
#
#   ./ship.sh "commit message"
#   OUT=dist ./ship.sh "msg"      # custom exe output dir (default C:/develop/bin)
#
# Pushing retries a few times because the GitHub link can be flaky here.
set -euo pipefail
cd "$(dirname "$0")"

MSG="${1:-update}"
OUT="${OUT:-C:/develop/bin}"

# 1) commit (only if something changed)
git add -A
if git diff --cached --quiet; then
  echo "[ship] no changes to commit"
else
  git commit -q -m "$MSG"
  echo "[ship] committed: $(git rev-parse --short HEAD) $MSG"
fi

# 2) push to both remotes
for remote in origin github; do
  ok=0
  for i in 1 2 3; do
    if GIT_TERMINAL_PROMPT=0 \
       GIT_SSH_COMMAND="ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes -o ConnectTimeout=10" \
       git push "$remote" main; then
      ok=1; break
    fi
    echo "[ship] push to $remote failed (try $i), retrying..."
    sleep 2
  done
  if [ "$ok" != 1 ]; then
    echo "[ship] ERROR: push to $remote failed" >&2
    exit 1
  fi
  echo "[ship] pushed -> $remote"
done

# 3) rebuild the exe
mkdir -p "$OUT"
uv run pyinstaller --noconfirm --clean --distpath "$OUT" deepseek-agent.spec >/dev/null
echo "[ship] built: $(cygpath -w "$OUT" 2>/dev/null || echo "$OUT")/deepseek-agent.exe"
