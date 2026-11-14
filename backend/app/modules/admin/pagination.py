"""Keyset pagination for the admin lists (attendees, points ledger).

Both lists are ordered newest first by `(created_at DESC, id DESC)`. `id` is
unique, so the order is total and stable: two rows with the same timestamp are
always in the same order, a page boundary can fall between them without
repeating or skipping either, and a row inserted while an administrator pages
cannot shift the pages already read (rows are appended at the head of the
order, a cursor points into the middle of it).

A cursor is opaque to clients and is
  * **signed** (HMAC, server secret) - a hand-made or edited cursor is refused;
  * **bound to the list and to the filters** it was issued for - a cursor from
    another list, or from the same list under different filters / search /
    date range, is refused instead of silently yielding a meaningless page.
Page SIZE is not part of the binding: a client may change `limit` between
pages.

Date filters are timezone-aware and half-open: `from` is INCLUSIVE, `to` is
EXCLUSIVE (`from <= created_at < to`), so consecutive ranges tile with no gap
and no overlap.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime
from typing import Any

from app.core.config import get_settings
from app.core.errors import InvalidCursorError
from app.core.security import canonical_json, sign_hmac_sha256

_DOMAIN = "admin-list-cursor:v1:"
_SIG_LEN = 22


def filter_fingerprint(kind: str, filters: dict[str, Any]) -> str:
    """What a cursor is bound to. Only filters that were actually set count
    (so `status=None` and an absent `status` are the same), datetimes are
    normalised to UTC so equal instants in different offsets are equal."""
    normal: dict[str, object] = {}
    for key, value in filters.items():
        if value is None:
            continue
        if isinstance(value, datetime):
            value = value.astimezone(UTC).isoformat()
        normal[key] = value
    payload: dict[str, object] = {"kind": kind, "filters": normal}
    return hashlib.sha256(canonical_json(payload)).hexdigest()[:24]


def _sign(body: str) -> str:
    return sign_hmac_sha256(get_settings().scan_ref_secret, _DOMAIN + body)[:_SIG_LEN]


def encode_cursor(kind: str, *, created_at: datetime, row_id: int, fingerprint: str) -> str:
    payload = {
        "k": kind,
        "t": created_at.astimezone(UTC).isoformat(),
        "i": row_id,
        "f": fingerprint,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
    return f"{body}.{_sign(body)}"


def decode_cursor(kind: str, raw: str, *, fingerprint: str) -> tuple[datetime, int]:
    """The position a valid cursor points at, or `InvalidCursorError` (422)."""
    try:
        body, _, signature = raw.partition(".")
        if not body or not hmac.compare_digest(signature, _sign(body)):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        if not isinstance(payload, dict) or payload["k"] != kind or payload["f"] != fingerprint:
            raise ValueError("cursor belongs to another list or other filters")
        created_at = datetime.fromisoformat(str(payload["t"]))
        row_id = int(payload["i"])
        if created_at.tzinfo is None or row_id < 1:
            raise ValueError("malformed position")
        return created_at, row_id
    except (ValueError, KeyError, TypeError) as err:  # incl. binascii.Error, JSON errors
        raise InvalidCursorError() from err
