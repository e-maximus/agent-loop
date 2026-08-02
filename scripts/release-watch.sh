#!/usr/bin/env bash
# Release watcher for the local prod checkout.
#
# Pull-based on purpose: this machine is behind NAT and the repo is public, so
# nothing from GitHub reaches in — the machine asks instead. Run every few
# minutes by the com.agent-loop.release-watch LaunchAgent.
#
# A release is a *version bump*: main is the working branch, so a new commit
# alone means nothing. When `version` in pyproject.toml differs from what is
# deployed, roll main forward — but only if the test suite passes on the new
# code. Anything else rolls back.
#
# How the restart is handled depends on the size of the bump, because the
# restart is the expensive part: `queue.stop()` cancels the in-flight task
# rather than draining it, so an interrupted fix loses everything it has spent
# on the strong model and starts from `baseline` on the next boot.
#
#   MINOR / MAJOR — new capability or a changed contract. Worth interrupting
#     for: wait up to IDLE_TIMEOUT for the agent to go idle, then restart. If it
#     is still busy, defer to the next tick rather than killing the task.
#
#   PATCH — a fix or a refactor. Not worth interrupting for: install it, then
#     leave the running process alone and mark a restart as pending. The next
#     tick that finds the agent idle applies it. So a patch reaches the machine
#     immediately and takes effect at the next natural gap in the work.
#
# The cost of the patch path is a window where the checkout is newer than the
# process running from it. `agent-loopctl status` reports that state rather than
# claiming the new version is live.
set -euo pipefail

ROOT="${AGENT_LOOP_HOME:-$HOME/agent-loop-prod}"
LABEL="com.agent-loop"
VENV="$ROOT/.venv"
DATA="$ROOT/data"
LOGS="$DATA/logs"
LOG="$LOGS/release-watch.log"
LOCK="$DATA/.release-watch.lock"
# Written when a patch is installed into a busy agent; cleared when the restart
# it asks for has happened.
PENDING="$DATA/.restart-pending"

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

restart_agent() {
    launchctl kickstart -k "gui/$(id -u)/$LABEL" >>"$LOG" 2>&1
}

agent_is_up() {
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -q "state = running"
}

running_tasks() {
    /usr/bin/sqlite3 "$DATA/agent-loop.db" \
        "SELECT COUNT(*) FROM tasks WHERE status='running'" 2>/dev/null || echo 0
}

bump_kind() {  # $1 = deployed version, $2 = incoming version → major|minor|patch
    # Only the release segment matters; any -rc/+build suffix is dropped. An
    # unreadable or absent deployed version is treated as major, because the
    # safe answer to "how big is this change?" is "big enough to restart for".
    local old="${1%%[-+]*}" new="${2%%[-+]*}"
    case "$old" in [0-9]*.[0-9]*.*) ;; *) echo major; return ;; esac
    case "$new" in [0-9]*.[0-9]*.*) ;; *) echo major; return ;; esac

    local old_rest="${old#*.}" new_rest="${new#*.}"
    if [ "${old%%.*}" != "${new%%.*}" ]; then
        echo major
    elif [ "${old_rest%%.*}" != "${new_rest%%.*}" ]; then
        echo minor
    else
        echo patch
    fi
}

# ── Apply a restart a previous patch deferred ─────────────────────────────
# Runs before the deploy check: the agent may have gone idle since, and a patch
# that is installed but not running is not yet a deployed patch.
if [ -f "$PENDING" ] && [ "$(running_tasks)" = "0" ]; then
    log "applying deferred restart for $(cat "$DATA/deployed.version" 2>/dev/null || echo "?")"
    restart_agent
    rm -f "$PENDING"
fi

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

kind="$(bump_kind "$local_version" "$remote_version")"
log "release $local_version → $remote_version ($kind, $(git rev-parse --short=12 origin/main))"

# ── Wait for the agent to go idle — minor/major only ──────────────────────
# A patch installs into a running agent and asks for a restart later, so there
# is nothing to wait for. Bigger bumps interrupt: wait, and if the agent is
# still working after IDLE_TIMEOUT, defer rather than cancel its task.
if [ "$kind" = "patch" ]; then
    log "patch release — installing without interrupting the agent"
else
    waited=0
    while [ "$waited" -lt "$IDLE_TIMEOUT" ]; do
        running="$(running_tasks)"
        [ "$running" = "0" ] && break
        sleep 20
        waited=$((waited + 20))
    done
    if [ "${running:-0}" != "0" ]; then
        log "still busy after ${IDLE_TIMEOUT}s — deferring to the next tick"
        exit 0
    fi
fi

# ── Roll forward ──────────────────────────────────────────────────────────
previous_sha="$(git rev-parse HEAD)"

roll_back() {
    log "rolling back to $(git rev-parse --short=12 "$previous_sha") ($local_version)"
    git reset --hard --quiet "$previous_sha"
    "$VENV/bin/pip" install -e ".[dev]" -c constraints.txt --quiet >>"$LOG" 2>&1 || true
    printf '%s\n' "$previous_sha" >"$DATA/deployed.sha"
    # A patch that failed on the way in never restarted anything: the process is
    # still running the code we just restored, so restarting would interrupt a
    # task for nothing. Bigger bumps have already stopped the agent by here.
    if [ "$kind" = "patch" ]; then
        rm -f "$PENDING"
    else
        restart_agent
    fi
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

# ── Restart, or arrange for one ───────────────────────────────────────────
if [ "$kind" = "patch" ] && [ "$(running_tasks)" != "0" ]; then
    # Installed under a working agent. Leave it alone; the next tick that finds
    # it idle picks the new code up. Health is not checked here because the
    # process being verified is still the old one.
    : >"$PENDING"
    log "installed $remote_version — restart deferred until the agent is idle"
    exit 0
fi

restart_agent
rm -f "$PENDING"
sleep "$HEALTH_WAIT"

if ! agent_is_up; then
    notify "$remote_version did not stay up — rolling back"
    roll_back
    exit 1
fi

log "deployed $remote_version ($(git rev-parse --short=12 HEAD))"
