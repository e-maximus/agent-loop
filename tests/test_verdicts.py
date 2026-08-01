"""Verdict parsing for the gate prompts.

The regression these cover: the gates used to branch on a substring test, so a
security reviewer that named its finding on the label line ("VERDICT: FAIL —
leaked password") opened the gate, because PASSWORD contains PASS.
"""

from __future__ import annotations

import pytest

from agent_loop.github.verdicts import parse_verdict

SECURITY = ("PASS", "FAIL")
CRITIC = ("APPROVE", "REVISE")
TRIAGE = ("ALLOW", "REJECT")
INTENT = ("QUESTION", "CHANGE_REQUEST", "NONE")


def security(text: str) -> str:
    return parse_verdict(text, "VERDICT", SECURITY, default="FAIL")


# ── the substring bug: security vocabulary containing "PASS" ───────────────
@pytest.mark.parametrize(
    "line",
    [
        "VERDICT: FAIL — hardcoded password in config.ts",
        "VERDICT: FAIL: password is logged in plaintext",
        "VERDICT: FAIL — auth bypass in the new middleware",
        "VERDICT: FAIL (the diff does not pass input sanitization)",
        "VERDICT: FAIL — passphrase committed to the repo",
    ],
)
def test_security_fail_is_not_opened_by_pass_substrings(line):
    assert security(line) == "FAIL"


@pytest.mark.parametrize(
    "line",
    [
        "VERDICT: NOT APPROVED — this does not fix the issue",
        "VERDICT: REVISE — the approved pattern is not followed",
    ],
)
def test_critic_negations_do_not_approve(line):
    assert parse_verdict(line, "VERDICT", CRITIC, default="REVISE") == "REVISE"


def test_triage_allow_mentioning_the_word_reject():
    # Used to be read as a rejection: "REJECT" appears inside the reasoning.
    line = "VERDICT: ALLOW — no reason to reject this maintenance request"
    assert parse_verdict(line, "VERDICT", TRIAGE, default="REJECT") == "ALLOW"


def test_question_about_a_change_is_not_a_change_request():
    line = "INTENT: QUESTION — the author asks about the change to auth.ts"
    assert parse_verdict(line, "INTENT", INTENT, default="NONE") == "QUESTION"


# ── the happy paths the prompts actually ask for ───────────────────────────
def test_plain_verdicts():
    assert security("VERDICT: PASS") == "PASS"
    assert security("VERDICT: FAIL") == "FAIL"
    assert parse_verdict("VERDICT: APPROVE", "VERDICT", CRITIC, default="REVISE") == "APPROVE"
    assert parse_verdict("INTENT: NONE", "INTENT", INTENT, default="QUESTION") == "NONE"


def test_markdown_and_leading_blank_lines():
    assert security("**VERDICT: PASS**") == "PASS"
    assert security("`VERDICT:` **FAIL**") == "FAIL"
    assert security("\n\nVERDICT: PASS\n\nNothing found.") == "PASS"


def test_bare_token_without_the_label():
    assert security("PASS") == "PASS"
    assert parse_verdict("APPROVE — ships as is", "VERDICT", CRITIC, default="REVISE") == "APPROVE"


def test_label_found_after_a_preamble():
    text = "Here is my security review:\n\nVERDICT: PASS\n\nNo issues found."
    assert security(text) == "PASS"


def test_unique_prefix_resolves():
    # Models routinely shorten CHANGE_REQUEST to CHANGE.
    assert parse_verdict("INTENT: CHANGE", "INTENT", INTENT, default="NONE") == "CHANGE_REQUEST"


# ── everything unreadable takes the caller's safe branch ───────────────────
@pytest.mark.parametrize(
    "text",
    [
        "",
        "I reviewed the diff and have some thoughts.",
        "VERDICT: MAYBE",
        "VERDICT: FAIL, though it would PASS with one change",  # self-contradicting
    ],
)
def test_unparseable_falls_back_to_default(text):
    assert security(text) == "FAIL"
    assert parse_verdict(text, "VERDICT", TRIAGE, default="REJECT") == "REJECT"


def test_ambiguous_prefix_is_not_guessed():
    # RE matches both REVISE and REJECT → no unique option.
    assert parse_verdict("VERDICT: RE", "VERDICT", ("REVISE", "REJECT"), default="REVISE") == "REVISE"
