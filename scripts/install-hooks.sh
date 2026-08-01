#!/usr/bin/env bash
# Install the repo's git hooks into this checkout.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
hooks_dir="$(git -C "$root" rev-parse --git-path hooks)"

install -m 755 "$root/scripts/hooks/pre-push" "$hooks_dir/pre-push"
echo "✓ installed pre-push hook → $hooks_dir/pre-push"
