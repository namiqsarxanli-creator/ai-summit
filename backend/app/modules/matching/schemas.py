"""The recommendation response — ARCHITECTURE.md §11.7, verbatim shape.

`match` is `None` in the no-interests edge case (§11.4): a distinct
JSON `null`, never a `score: 0` object standing in for "no data".
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.modules.booths.schemas import BoothOut


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MatchedInterest(_Strict):
    slug: str
    name: str


class Match(_Strict):
    score: int
    matched_interests: list[MatchedInterest]
    explanation: str
    algorithm_version: str


class Recommendation(_Strict):
    booth: BoothOut
    match: Match | None
