"""Blast-radius policy for auto-merge. Ported from policy.ts.

A failing verdict means "leave the PR for human review", not "reject".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..config import MergePolicy
from .gh import ChangedFile


@dataclass
class PolicyVerdict:
    ok: bool
    reasons: list[str] = field(default_factory=list)


def _glob_to_regexp(glob: str) -> re.Pattern[str]:
    """Minimal glob → regex supporting ** and * — enough for path prefixes."""
    escaped = re.escape(glob)
    # re.escape turns * into \*, ** into \*\*. Restore glob semantics:
    escaped = escaped.replace(r"\*\*", "\x00")  # placeholder for **
    escaped = escaped.replace(r"\*", "[^/]*")
    escaped = escaped.replace("\x00", ".*")
    return re.compile(f"^{escaped}$")


def evaluate_diff(files: list[ChangedFile], policy: MergePolicy) -> PolicyVerdict:
    """Decide whether a diff is safe to auto-merge: every changed file must match
    an allowed glob, and total changed lines must be within the limit."""
    reasons: list[str] = []
    matchers = [_glob_to_regexp(g) for g in policy.allowed_globs]

    def allowed(f: str) -> bool:
        return any(m.match(f) for m in matchers)

    if not files:
        return PolicyVerdict(ok=False, reasons=["no changed files"])

    outside = [f.file for f in files if not allowed(f.file)]
    if outside:
        reasons.append(f"files outside allowed globs: {', '.join(outside)}")

    total = sum(f.added + f.deleted for f in files)
    if total > policy.max_changed_lines:
        reasons.append(f"diff too large: {total} lines > limit {policy.max_changed_lines}")

    return PolicyVerdict(ok=not reasons, reasons=reasons)
