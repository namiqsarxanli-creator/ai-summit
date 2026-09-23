"""The matching core — ARCHITECTURE.md §11.2–§11.6. Pure: no database,
no clock, no randomness, no I/O. Same inputs, same output, always.

The formula (§11.3), verbatim:

    raw      = Σ_{i ∈ M} w(i)          where M = A ∩ B
    coverage = raw / |A|
    focus    = raw / Σ_{i ∈ B} w(i)
    score01  = α · coverage + (1 − α) · focus        α = 0.7
    score    = round_half_up(100 · score01)

Arithmetic is EXACT. Weights arrive as `Decimal` (numeric(3,2), §11.3:
"use Decimal for w(i)"), and the divisions are carried out as
`Fraction`s built from those Decimals, so no division is ever rounded
before the final step. The final step is `floor(x + 1/2)` on that exact
value — §11.3's round-half-up, never Python's banker's `round()`. (A
Decimal division would be exact for most inputs but rounds at 28 digits
for repeating quotients such as 1/3; a Fraction never does.)

There is exactly one number: this function's `score`. Nothing downstream
may adjust it (§24.5).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

ALGORITHM_VERSION = "weighted-v1"
MAX_NAMED_INTERESTS = 3


@dataclass(frozen=True)
class MatchConfig:
    alpha: Decimal = Decimal("0.7")


DEFAULT_CONFIG = MatchConfig()


@dataclass(frozen=True)
class MatchResult:
    score: int
    matched: frozenset[int]  # M = A ∩ B
    raw: Fraction
    coverage: Fraction
    focus: Fraction
    score01: Fraction


def round_half_up(value: Fraction) -> int:
    """§11.3: halves round UP (78.5 → 79, 79.5 → 80) — unlike `round()`."""
    return math.floor(value + Fraction(1, 2))


def score(
    attendee_interest_ids: frozenset[int],
    booth_weights: Mapping[int, Decimal],
    config: MatchConfig = DEFAULT_CONFIG,
) -> MatchResult:
    """Scores one attendee against one booth.

    §11.4's first and third edge cases are the CALLER's to route — no
    interests means no score is computed at all (`match: null`), and a
    booth with no profile is not scored at all — so both are refused
    here rather than silently turned into a number.
    """
    if not attendee_interest_ids:
        raise ValueError("no interests selected: no score is computed (§11.4)")
    if not booth_weights:
        raise ValueError("booth has no interest profile: it is not scored (§11.4)")
    for weight in booth_weights.values():
        if not isinstance(weight, Decimal) or not Decimal(0) < weight <= Decimal(1):
            raise ValueError(f"booth weight must be a Decimal in (0, 1], got {weight!r}")

    matched = attendee_interest_ids & booth_weights.keys()
    raw = sum((Fraction(booth_weights[i]) for i in matched), Fraction(0))
    coverage = raw / len(attendee_interest_ids)
    focus = raw / sum((Fraction(w) for w in booth_weights.values()), Fraction(0))
    alpha = Fraction(config.alpha)
    score01 = alpha * coverage + (1 - alpha) * focus
    return MatchResult(
        score=round_half_up(100 * score01),
        matched=frozenset(matched),
        raw=raw,
        coverage=coverage,
        focus=focus,
        score01=score01,
    )


# --------------------------------------------------------------------------
# Explanation — a template over the ACTUAL matched interests (§11.5)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class InterestRef:
    slug: str
    name: str


def band(score_value: int) -> str:
    """§11.5: ≥80 Strong, 60–79 Good, 40–59 Partial, 1–39 Some overlap.

    §11.5 defines no band for 0 because M = ∅ gives 0 and has no
    explanation. A non-empty M can still round to 0 in principle (one
    tiny weight against many interests); there IS overlap then, so it
    reads "Some overlap" rather than inventing a fifth band."""
    if score_value >= 80:
        return "Strong match"
    if score_value >= 60:
        return "Good match"
    if score_value >= 40:
        return "Partial match"
    return "Some overlap"


def order_matched(
    matched: frozenset[int],
    booth_weights: Mapping[int, Decimal],
    interests: Mapping[int, InterestRef],
) -> list[InterestRef]:
    """By contribution w(i) descending, tie-broken by name ascending."""
    return [
        interests[i] for i in sorted(matched, key=lambda i: (-booth_weights[i], interests[i].name))
    ]


def explanation(score_value: int, ordered_matched: Sequence[InterestRef]) -> str:
    """ "{band} based on your interests in {A}, {B} and {C}." — at most
    three named, only interests in M. Raises for an empty M: that case
    has no explanation (§11.4)."""
    if not ordered_matched:
        raise ValueError("no matched interests: no explanation (§11.4)")
    names = [i.name for i in ordered_matched[:MAX_NAMED_INTERESTS]]
    joined = names[0] if len(names) == 1 else f"{', '.join(names[:-1])} and {names[-1]}"
    return f"{band(score_value)} based on your interests in {joined}."
