"""Parsing the labelled verdicts the gate prompts ask the model to emit.

Every gate in the pipeline (intake triage, critic, diff security review, PR
comment classification) asks for a first line like `VERDICT: PASS` and then
branches on it. Matching that with a plain substring test is unsafe: `PASS` is a
substring of `PASSWORD` and `BYPASS`, and `APPROVE` of `NOT APPROVED` — exactly
the vocabulary a security reviewer uses when it means the opposite. A model that
appends its reason to the label line ("VERDICT: FAIL — leaked password") would
flip the gate open.

So matching is anchored: the option must appear as a whole word, and when a line
mentions two different options at once the result is ambiguous and we fall back
to the caller's default. Callers pass the *safe* option as the default, so any
parse failure blocks rather than opens.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# The label ("VERDICT", "INTENT"), then optional markdown/punctuation noise
# (**bold**, backticks), then the option token. \b keeps PASS out of PASSWORD.
_LABELLED = r"{label}\s*[:\-]?\s*[*_`\s]*([A-Z][A-Z_]*)\b"


def _first_line(text: str) -> str:
    """The first non-empty line. Models (reasoning ones especially) like to open
    with a blank line, which would otherwise lose the verdict."""
    for line in text.split("\n"):
        if line.strip():
            return line
    return ""


def _resolve(token: str, options: Sequence[str]) -> str | None:
    """Map a captured token onto one of the options. Exact match, else a unique
    prefix — the model writing `INTENT: CHANGE` for CHANGE_REQUEST is common
    enough to accept, while an ambiguous prefix (RE → REVISE/REJECT) is not."""
    token = token.upper()
    if token in options:
        return token
    prefixed = [o for o in options if o.startswith(token)]
    return prefixed[0] if len(prefixed) == 1 else None


def parse_verdict(
    text: str, label: str, options: Sequence[str], *, default: str
) -> str:
    """Extract one of `options` from a labelled verdict in `text`.

    Tried in order: the label on the first non-empty line, a bare option token on
    that line, then the label anywhere in the body (a model that opens with
    "Here is my review:" still gets read correctly). Anything ambiguous or
    unrecognised returns `default`.
    """
    options = [o.upper() for o in options]
    first = _first_line(text)

    m = re.search(_LABELLED.format(label=re.escape(label)), first, re.IGNORECASE)
    if m and (hit := _resolve(m.group(1), options)):
        return hit

    # No usable label — accept a bare token, but only if the line names exactly
    # one option. "FAIL, does not PASS validation" must not resolve to either.
    bare = {o for o in options if re.search(rf"\b{re.escape(o)}\b", first, re.IGNORECASE)}
    if len(bare) == 1:
        return bare.pop()
    if bare:
        return default  # the line contradicts itself — take the safe branch

    body = re.search(_LABELLED.format(label=re.escape(label)), text, re.IGNORECASE)
    if body and (hit := _resolve(body.group(1), options)):
        return hit

    return default
