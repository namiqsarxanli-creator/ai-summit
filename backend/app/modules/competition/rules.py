"""The Beyond Now competition: 100 points, earned only by VERIFIED participation.

Source: Beyond_Now_oyun_telimati_teqdimat.pdf, "Bal cədvəli: maksimum 100".
Each category has its own per-unit value and its own maximum; the 100 is the sum
of the category maximums, never a clamp on the total.

| Category    | Per unit                         | Maximum |
|-------------|----------------------------------|---------|
| PROFILE     | 5, once                          | 5       |
| PANEL       | 8 per panel (entry AND exit QR)  | 16      |
| QMETER      | 2 per panel (Game Master check)  | 4       |
| MASTERCLASS | 12 per block (entry AND exit)    | 24      |
| PLAYGROUND  | 3 per distinct booth, first 8    | 24      |
| CONNECTION  | 3 match / 1 other, best 4        | 12      |
| FOUNDER     | 15, once (all four zones)        | 15      |

Owner's decision (2 Nov 2026): Meet the Mentor is not held on 13 Nov, so it is
no competition category any more; its 5 points went to the Founder bonus
(10 -> 15). A MENTOR award would score nothing (ledger.award: "not_scored").

Decisions recorded with the organiser (16 Oct 2026):
* Legacy booth TASK / FEEDBACK rewards stay in the ledger history but do NOT
  count in the competition ranking once competition mode is on.
* PROFILE completion = an ACTIVE (e-mail verified) attendee whose interest card
  answers all four questions: a sector, at least one topic, at least one "looking
  for" and at least one "offering". Company and LinkedIn stay optional.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from app.modules.points.models import PointsSource


@dataclass(frozen=True)
class Category:
    source: PointsSource
    maximum: int


PROFILE_POINTS: Final = 5
PANEL_POINTS: Final = 8
QMETER_POINTS: Final = 2
MASTERCLASS_POINTS: Final = 12
PLAYGROUND_POINTS: Final = 3
PLAYGROUND_SCORING_BOOTHS: Final = 8
MENTOR_POINTS: Final = 5
FOUNDER_POINTS: Final = 15

CATEGORIES: Final[dict[PointsSource, Category]] = {
    c.source: c
    for c in (
        Category(PointsSource.PROFILE, 5),
        Category(PointsSource.PANEL, 16),
        Category(PointsSource.QMETER, 4),
        Category(PointsSource.MASTERCLASS, 24),
        Category(PointsSource.PLAYGROUND, 24),
        Category(PointsSource.CONNECTION, 12),
        Category(PointsSource.FOUNDER, 15),
    )
}
COMPETITION_SOURCES: Final = tuple(CATEGORIES)
assert sum(c.maximum for c in CATEGORIES.values()) == 100

# The Grand Final check-in (owner's decision, 3 Nov 2026, FACEMARK's participant
# guide): a `panels` row with this code is not a Main Stage panel. Its screen shows
# one ENTRY code for the row's whole time; scanning it records that the person
# stayed to the end, which Beyond Now Founder requires (competition/founder.py).
GRAND_FINAL_CODE: Final = "grand-final"
# The Game Master opens the check-in when the hall is ready (FACEMARK, 8 Nov 2026:
# "the programme always slips"): from that moment, for this many minutes.
GRAND_FINAL_CHECKIN_MINUTES: Final = 15

# Scan windows (PDF "Kim, nəyi, nə vaxt skan edir").
PANEL_ENTRY_MINUTES: Final = 15  # first 15 minutes
PANEL_EXIT_MINUTES: Final = 10  # last 10 minutes
MASTERCLASS_ENTRY_MINUTES: Final = 10
MASTERCLASS_EXIT_MINUTES: Final = 10
MASTERCLASS_NO_SHOW_MINUTES: Final = 5  # an unclaimed seat is released after +5
