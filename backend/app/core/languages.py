"""Interface languages the product speaks (Azerbaijani, English, Russian).

Used wherever the server must pick a language on the client's behalf -
today, the language of an OTP email. The value is validated on the request
and stored with the delivery, so the email is written in the language the
attendee was using when they asked for it, not in whatever is configured
when a worker later gets to it.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BeforeValidator

Language = Literal["az", "en", "ru"]
LANGUAGES: tuple[Language, ...] = ("az", "en", "ru")

# What a client that does not send a language gets: the language OTP emails
# were written in before language selection existed.
DEFAULT_LANGUAGE: Language = "en"


def _normalise(value: object) -> object:
    # "AZ" and " az " are the same choice; anything else (including a
    # locale like "az-AZ") falls through and fails the Literal check.
    return value.strip().lower() if isinstance(value, str) else value


RequestLanguage = Annotated[Language, BeforeValidator(_normalise)]
