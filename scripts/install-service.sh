#!/usr/bin/env bash
# Install agent-loop as a LaunchAgent pair on this machine: the daemon itself
# and the release watcher that keeps it up to date with main.
#
# Idempotent — safe to re-run after changing a plist template.
#
#   scripts/install-service.sh [target-dir]     # default ~/agent-loop-prod
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ROOT="${1:-${AGENT_LOOP_HOME:-$HOME/agent-loop-prod}}"
REPO="e-maximus/agent-loop"
DOMAIN="gui/$(id -u)"
AGENTS="$HOME/Library/LaunchAgents"

# ── Interpreter: the project needs 3.12+, /usr/bin/python3 is older ────────
pick_python() {
    for candidate in "${PYTHON:-}" python3.12 python3; do
        [ -n "$candidate" ] || continue
        local path; path="$(command -v "$candidate" 2>/dev/null || true)"
        [ -n "$path" ] || continue
        if "$path" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
            echo "$path"; return 0
        fi
    done
    echo "✖ need python 3.12+ — set PYTHON=/path/to/python3.12" >&2
    exit 1
}
PYTHON_BIN="$(pick_python)"

# ── Prod checkout, kept separate from your dev clone ───────────────────────
# The watcher runs `git reset --hard` here; it must never be a tree you work in.
if [ ! -d "$ROOT/.git" ]; then
    echo "→ cloning $REPO → $ROOT"
    gh repo clone "$REPO" "$ROOT"
fi

cd "$ROOT"
git config --local --get remote.origin.url | grep -q '^https' || {
    echo "→ switching origin to https (unattended fetch has no ssh-agent)"
    git remote set-url origin "https://github.com/$REPO.git"
    gh auth setup-git
}

echo "→ installing into $ROOT/.venv ($("$PYTHON_BIN" -V))"
[ -d "$ROOT/.venv" ] || "$PYTHON_BIN" -m venv "$ROOT/.venv"
"$ROOT/.venv/bin/pip" install -e "$ROOT[dev]" --quiet

mkdir -p "$ROOT/data/logs"

# ── Secrets and sources: gitignored, so they survive `git reset --hard` ────
for f in .env agent-loop.config.yaml; do
    if [ ! -f "$ROOT/$f" ]; then
        if [ "$SRC" != "$ROOT" ] && [ -f "$SRC/$f" ]; then
            cp "$SRC/$f" "$ROOT/$f"
            echo "→ copied $f from $SRC"
        else
            echo "⚠ $ROOT/$f is missing — create it before starting"
        fi
    fi
done

# ── LaunchAgents ──────────────────────────────────────────────────────────
mkdir -p "$AGENTS"
for label in com.agent-loop com.agent-loop.release-watch; do
    sed -e "s|@ROOT@|$ROOT|g" -e "s|@HOME@|$HOME|g" \
        "$ROOT/deploy/$label.plist.template" >"$AGENTS/$label.plist"
    launchctl bootout "$DOMAIN/$label" 2>/dev/null || true
    launchctl bootstrap "$DOMAIN" "$AGENTS/$label.plist"
    echo "✓ loaded $label"
done

echo
echo "Docker Desktop must start at login for container mode to work."
echo "Sleep will pause the agent: sudo pmset -c sleep 0"
echo
"$ROOT/scripts/agent-loopctl" status
