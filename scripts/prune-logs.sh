#!/usr/bin/env bash
# Keep data/ from growing forever.
#
# Per-task logs are written for every issue the agent touches and nothing ever
# deletes them; the daemon's stdout files grow monotonically. Clones under
# data/repos are deliberately left alone — they are reused between tasks, and
# re-cloning a repo with node_modules is expensive.
set -euo pipefail

ROOT="${AGENT_LOOP_HOME:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
LOGS="$ROOT/data/logs"
KEEP_DAYS="${LOG_KEEP_DAYS:-14}"
MAX_MB="${LOG_MAX_MB:-50}"

[ -d "$LOGS" ] || exit 0

# Per-task logs older than KEEP_DAYS.
find "$LOGS" -type f -name '*.log' -mtime +"$KEEP_DAYS" -not -name 'daemon.*' \
    -not -name 'release-watch*' -delete 2>/dev/null || true

# The append-only streams: truncate to the last 20k lines once they get big.
# The daemon holds these open, so truncate in place rather than rotating.
for f in "$LOGS/daemon.out.log" "$LOGS/daemon.err.log" "$LOGS/release-watch.log" \
         "$LOGS/release-watch.out.log" "$LOGS/agent-loop.log"; do
    [ -f "$f" ] || continue
    size_mb=$(( $(stat -f %z "$f") / 1024 / 1024 ))
    if [ "$size_mb" -ge "$MAX_MB" ]; then
        tail -n 20000 "$f" >"$f.trim" && cat "$f.trim" >"$f" && rm -f "$f.trim"
    fi
done
