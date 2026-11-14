"""The public form of a person's name: first name and surname initial ("Anar N.").

The event's rules show competitors this way (PDF: "ad + soyadın ilk hərfi"), on
the attendees' leaderboard, the venue screen and the final alike. The full
surname is kept for the person's own profile and for authorised staff/admin
screens, never put on a public or shared surface.
"""

from __future__ import annotations


def short_name(first: str, last: str) -> str:
    initial = last.strip()[:1]
    return f"{first.strip()} {initial}." if initial else first.strip()
