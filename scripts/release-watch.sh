#!/usr/bin/env bash
# Release watcher for the local prod checkout.
#
# Pull-based on purpose: this machine is behind NAT and the repo is public, so
# nothing from GitHub reaches in — the machine asks instead. Run every few
# minutes by the com.agent-loop.release-watch LaunchAgent.
#
# A release is a *version bump*: main is the working branch, so a new commit
# alone means nothing. When `version` in pyproject.toml differs from what is
# deployed, roll main forward — but only into an idle agent, and only if the
# test suite passes on the new code. Anything else rolls back.
set -euo pipefail

ROOT="${AGENT_LOOP_HOME:-$HOME/agent-loop-prod}"
LABEL="com.agent-loop"
VENV="$ROOT/.venv"
DATA="$ROOT/data"
LOGS="$DATA/logs"
LOG="$LOGS/release-watch.log"
LOCK="$DATA/.release-watch.lock"

IDLE_TIMEOUT="${IDLE_TIMEOUT:-900}"   # wait up to 15 min for the agent to go idle
HEALTH_WAIT="${HEALTH_WAIT:-30}"      # must stay up this long to count as deployed

mkdir -p "$LOGS"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >>"$LOG"; }

notify() {
    log "ALERT $*"
    /usr/bin/osascript -e "display notification \"$1\" with title \"agent-loop deploy\"" 2>/dev/null || true
}

# ── Single instance ───────────────────────────────────────────────────────
# A deploy can outlast the 5-minute interval (waiting for idle), so the next
# firing must not start a second one. mkdir is the atomic primitive available
# everywhere; a lock older than an hour is stale from a killed run.
if [ -d "$LOCK" ] && [ -n "$(find "$LOCK" -maxdepth 0 -mmin +60 2>/dev/null)" ]; then
    log "removing stale lock"
    rmdir "$LOCK" 2>/dev/null || true
fi
if ! mkdir "$LOCK" 2>/dev/null; then
    exit 0  # another run is in progress
fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT

cd "$ROOT"

# Housekeeping every tick, not only on release days — per-task logs and stale
# checkouts grow whether or not anything is deployed.
"$ROOT/scripts/prune-logs.sh" >>"$LOG" 2>&1 || true

pyproject_version() {  # $1 = git revision
    git show "$1:pyproject.toml" | sed -n 's/^version = "\(.*\)"/\1/p' | head -1
}

# ── Is there anything to deploy? ──────────────────────────────────────────
if ! git fetch --quiet origin main 2>>"$LOG"; then
    log "git fetch failed — offline?"
    exit 0
fi

remote_sha="$(git rev-parse origin/main)"
deployed_sha="$(cat "$DATA/deployed.sha" 2>/dev/null || echo "")"
[ "$remote_sha" = "$deployed_sha" ] && exit 0

remote_version="$(pyproject_version origin/main)"
local_version="$(pyproject_version HEAD)"
if [ -z "$remote_version" ]; then
    log "could not read version from origin/main — skipping"
    exit 0
fi
if [ "$remote_version" = "$local_version" ]; then
    # New commits, same version: not a release. Nothing to record — the version
    # comparison is the gate, so re-checking next tick costs one `git show`.
    exit 0
fi

log "release $local_version → $remote_version ($(git rev-parse --short=12 origin/main))"

# ── Wait for the agent to go idle ─────────────────────────────────────────
# queue.stop() cancels the in-flight worker rather than draining it. The task
# is re-queued on the next boot, so nothing is lost — but its LLM spend is.
waited=0
while [ "$waited" -lt "$IDLE_TIMEOUT" ]; do
    running="$(/usr/bin/sqlite3 "$DATA/agent-loop.db" \
        "SELECT COUNT(*) FROM tasks WHERE status='running'" 2>/dev/null || echo 0)"
    [ "$running" = "0" ] && break
    sleep 20
    waited=$((waited + 20))
done
if [ "${running:-0}" != "0" ]; then
    log "still busy after ${IDLE_TIMEOUT}s — deferring to the next tick"
    exit 0
fi

# ── Roll forward ──────────────────────────────────────────────────────────
previous_sha="$(git rev-parse HEAD)"

roll_back() {
    log "rolling back to $(git rev-parse --short=12 "$previous_sha") ($local_version)"
    git reset --hard --quiet "$previous_sha"
    "$VENV/bin/pip" install -e ".[dev]" --quiet >>"$LOG" 2>&1 || true
    printf '%s\n' "$previous_sha" >"$DATA/deployed.sha"
    restart_agent
}

restart_agent() {
    launchctl kickstart -k "gui/$(id -u)/$LABEL" >>"$LOG" 2>&1
}

agent_is_up() {
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -q "state = running"
}

git reset --hard --quiet "origin/main"

# -c constraints.txt: install the exact dependency set this release was tested
# against. Without it the reinstall resolves whatever PyPI serves today, and a
# transitive release becomes an unattended deploy nobody made.
if ! "$VENV/bin/pip" install -e ".[dev]" -c constraints.txt --quiet >>"$LOG" 2>&1; then
    notify "pip install failed for $remote_version"
    roll_back
    exit 1
fi

if ! "$VENV/bin/python" -m pytest -q >>"$LOG" 2>&1; then
    notify "tests failed for $remote_version — not deploying"
    roll_back
    exit 1
fi

# Written before the restart: version.py reads it at import to stamp traces.
printf '%s\n' "$remote_sha" >"$DATA/deployed.sha"
printf '%s\n' "$remote_version" >"$DATA/deployed.version"

restart_agent
sleep "$HEALTH_WAIT"

if ! agent_is_up; then
    notify "$remote_version did not stay up — rolling back"
    roll_back
    exit 1
fi

log "deployed $remote_version ($(git rev-parse --short=12 HEAD))"
