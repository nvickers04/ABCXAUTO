"""Objectivity taxonomy + banned taste phrases for shell prompts.

Shell text must be Fact, Gate, or labeled Heuristic. Taste belongs in
Grok judgment — never hard-coded narrative.
"""

from __future__ import annotations

# Fact | Gate | Heuristic | Taste — see plan objective_shell_language.
TAXONOMY = {
    "fact": "Observable broker/market/code state",
    "gate": "Hard rule enforced in code",
    "heuristic": "Computed signal; must say heuristic ≠ recommendation",
    "taste": "Style/narrative — Grok judgment or delete",
}

# Case-insensitive substrings forbidden in shell playbook/posture/features/pressure.
BANNED_TASTE_PHRASES: tuple[str, ...] = (
    "harvest",
    "mild bull",
    "breakout thesis",
    "prefer acting",
    "prefer quality",
    "uptrend support",
    "conviction high",
    "ranked ideas",
)


def find_banned_phrases(text: str) -> list[str]:
    """Return banned phrases found in text (case-insensitive)."""
    low = (text or "").lower()
    return [p for p in BANNED_TASTE_PHRASES if p in low]


def assert_no_banned_phrases(text: str, *, label: str = "text") -> None:
    found = find_banned_phrases(text)
    if found:
        raise AssertionError(f"{label} contains banned taste phrases: {found}")
