"""Read-only view of the admin audit trail (`admin_audit`).

What an administrator may SEE of an audit row is decided here, by an explicit
allowlist per event kind - never by "everything except the secrets":

* `before_value` / `after_value` are opaque JSONB written by many code paths over
  time. Nothing from them reaches the client unless the event's allowlist names
  that field AND the value has the field's plain type (an integer, a short text,
  a boolean, one of a fixed set of words). A nested object, a list, an oversized
  text or a value of the wrong type is dropped, so a secret can neither be named
  by a field nor smuggled inside an allowed one.
* Fields that exist in old rows but are not needed on screen are hidden on
  purpose: a staff account's e-mail address and full name (`staff.create`,
  `staff.deactivate`) are personal data and the staff account's id and role say
  everything an audit reader needs. The response only says HOW MANY fields were
  withheld (`hidden_field_count`), never their names or values.
* An event kind this module does not know (a newer or older writer) is shown as
  metadata only - who, when, what kind, which target - with `details="limited"`.
  The raw JSON is never passed through.
* Strings that survive the allowlist still go through the log-masking functions
  (`mask_secrets`), a second line of defence for a card token or a download
  ticket that somehow ended up in an administrator-typed text.

Reading the trail writes nothing: viewing the audit must not create audit rows.

Keyset pagination uses the same signed, filter-bound cursor as the other admin
lists (`admin/pagination.py`): `(created_at DESC, id DESC)`, `from` inclusive,
`to` exclusive.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import DateTime, Integer, literal, select, tuple_
from sqlalchemy.orm import Session

from app.core.privacy import mask_secrets
from app.modules.admin.models import AdminAudit
from app.modules.admin.pagination import decode_cursor, encode_cursor, filter_fingerprint
from app.modules.admin.schemas import (
    AttendeeFilters,
    AuditEvent,
    AuditExportResult,
    AuditListQuery,
    AuditPage,
    AuditValue,
    PointsFilters,
)
from app.modules.staff.models import StaffAccount

KIND = "audit"

TEXT_LIMIT = 500
_MAX_JS_INT = 2**53  # keeps every integer exactly representable in the browser

_Coerce = Callable[[Any], tuple[bool, AuditValue]]
_REJECT: tuple[bool, AuditValue] = (False, None)


# --- field coercers: a value is shown only when it has exactly this shape ------


def _integer(value: Any) -> tuple[bool, AuditValue]:
    if isinstance(value, bool) or not isinstance(value, int) or abs(value) > _MAX_JS_INT:
        return _REJECT
    return True, value


def _boolean(value: Any) -> tuple[bool, AuditValue]:
    return (True, value) if isinstance(value, bool) else _REJECT


def _text(value: Any) -> tuple[bool, AuditValue]:
    if not isinstance(value, str):
        return _REJECT
    cleaned = mask_secrets("".join(ch for ch in value if ch.isprintable() or ch in "\n\t"))
    if len(cleaned) > TEXT_LIMIT:
        cleaned = cleaned[: TEXT_LIMIT - 1] + "…"
    return True, cleaned


def _reference(value: Any) -> tuple[bool, AuditValue]:
    """A compound identifier such as "3:7" (booth:interest)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return _integer(value)
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9:_.-]{1,64}", value):
        return True, value
    return _REJECT


def _weight(value: Any) -> tuple[bool, AuditValue]:
    """The interest weight, written as a decimal string ("0.50")."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return _REJECT
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return _REJECT
    if not number.is_finite() or abs(number) >= Decimal(10) ** 6:
        return _REJECT
    return True, format(number, "f")


def _one_of(*words: str) -> _Coerce:
    allowed = frozenset(words)

    def coerce(value: Any) -> tuple[bool, AuditValue]:
        return (True, value) if isinstance(value, str) and value in allowed else _REJECT

    return coerce


def _optional(coerce: _Coerce) -> _Coerce:
    """The field may legitimately be null (a booth id on an admin account)."""

    def inner(value: Any) -> tuple[bool, AuditValue]:
        return (True, None) if value is None else coerce(value)

    return inner


# --- the allowlists: what is shown for each kind of event ----------------------

_STAFF: dict[str, _Coerce] = {
    "id": _integer,
    "role": _one_of("ADMIN", "BOOTH_STAFF"),
    "booth_id": _optional(_integer),
    "is_active": _boolean,
    # deliberately absent (personal data, not needed to read the trail):
    #   email, full_name
}
_BOOTH: dict[str, _Coerce] = {
    "id": _integer,
    "slug": _text,
    "name": _text,
    "description": _optional(_text),
    "location_label": _optional(_text),
    "is_active": _boolean,
    "allow_repeat_task_points": _optional(_boolean),
}
_WEIGHT: dict[str, _Coerce] = {
    "id": _reference,
    "booth_id": _integer,
    "interest_id": _integer,
    "weight": _weight,
    "removed": _boolean,
}
_INTEREST: dict[str, _Coerce] = {
    "id": _integer,
    "slug": _text,
    "name": _text,
    "sort_order": _integer,
    "is_active": _boolean,
}
_TASK: dict[str, _Coerce] = {
    "id": _integer,
    "booth_id": _integer,
    "title": _text,
    "description": _optional(_text),
    "sort_order": _integer,
    "is_active": _boolean,
    "points": _optional(_integer),
    "allow_repeat_points": _optional(_boolean),
}
_PRESET: dict[str, _Coerce] = {
    "id": _integer,
    "booth_id": _optional(_integer),
    "points": _integer,
    "label": _optional(_text),
    "sort_order": _integer,
    "is_active": _boolean,
}
_CONFIG: dict[str, _Coerce] = {
    "feedback_points": _integer,
    "leaderboard_top_n": _integer,
    "allow_repeat_task_points_default": _boolean,
    "competition_mode": _boolean,
    "reminders_enabled": _boolean,
}

# A points reversal (points/reversal.py). `reason` is the administrator's own
# text (3-300 characters); everything else is an id, a count or one of a fixed
# set of words. The attendee is shown by id only.
_REVERSAL: dict[str, _Coerce] = {
    "points_log_id": _integer,
    "reversal_id": _integer,
    "user_id": _integer,
    "booth_id": _optional(_integer),
    "task_id": _optional(_integer),
    "source": _one_of(
        "TASK",
        "FEEDBACK",
        "PROFILE",
        "PANEL",
        "QMETER",
        "MASTERCLASS",
        "PLAYGROUND",
        "CONNECTION",
        "MENTOR",
        "FOUNDER",
    ),
    "points": _integer,
    "reversed": _boolean,
    "reason": _text,
}
# Password events (staff/password.py): which account, who started a reset, and how
# many sessions ended. NEVER a password, a hash or a code - the writers do not
# record them, and nothing else could pass this list.
_PASSWORD_CHANGED: dict[str, _Coerce] = {"id": _integer, "sessions_revoked": _integer}
_PASSWORD_RESET_REQUESTED: dict[str, _Coerce] = {
    "id": _integer,
    "initiated_by": _one_of("self", "admin"),
}

# The masterclass programme (masterclasses/admin.py). Titles, descriptions and
# speakers are administrator text; times are ISO strings; the rest are ids,
# counts and flags. No attendee data is ever written into these events.
_MC_EVENT: dict[str, _Coerce] = {
    "id": _integer,
    "code": _text,
    "title": _text,
    "venue": _optional(_text),
    "timezone": _text,
    "event_date": _text,
    "competition_closes_at": _optional(_text),
}
_MC_BLOCK: dict[str, _Coerce] = {
    "id": _integer,
    "event_id": _integer,
    "code": _text,
    "title": _text,
    "starts_at": _text,
    "ends_at": _text,
    "change_deadline": _text,
    "sort_order": _integer,
    "display_number": _optional(_integer),
}
_MC_ROOM: dict[str, _Coerce] = {
    "id": _integer,
    "event_id": _integer,
    "name": _text,
    "sort_order": _integer,
    "display_number": _optional(_integer),
}
_MASTERCLASS: dict[str, _Coerce] = {
    "id": _integer,
    "event_id": _integer,
    "block_id": _optional(_integer),
    "room_id": _optional(_integer),
    "title": _text,
    "description": _optional(_text),
    "speaker": _optional(_text),
    "capacity": _integer,
    "waitlist_capacity": _integer,
    "is_published": _boolean,
    "sort_order": _integer,
    "standing_capacity": _integer,
}

# Beyond Now competition (competition/routers.py, competition/panels.py). The
# Qmeter reason is the Game Master's own text; the attendee is shown by id only.
_PANEL: dict[str, _Coerce] = {
    "id": _integer,
    "event_id": _integer,
    "code": _text,
    "title": _text,
    "starts_at": _text,
    "ends_at": _text,
    "qr_version": _integer,
}
_SCOPE: dict[str, _Coerce] = {
    "id": _integer,
    "staff_account_id": _integer,
    "scope": _one_of("MASTERCLASS_DOOR", "GAME_MASTER", "MENTOR_HOST"),
    "target_id": _optional(_integer),
    "revoked": _boolean,
}
_ELIGIBILITY: dict[str, _Coerce] = {
    "id": _integer,
    "excluded": _boolean,
    "reason": _optional(_text),
}
_MATCH_TABLE: dict[str, _Coerce] = {
    "id": _integer,
    "event_id": _integer,
    "label": _text,
    "is_active": _boolean,
}
_MENTOR_SESSION: dict[str, _Coerce] = {
    "id": _integer,
    "event_id": _integer,
    "code": _text,
    "title": _text,
    "mentor_name": _optional(_text),
    "starts_at": _text,
    "ends_at": _text,
    "capacity": _integer,
    "waitlist_capacity": _integer,
    "is_open": _boolean,
}
_FINAL: dict[str, _Coerce] = {
    "id": _integer,
    "version": _integer,
    "status": _one_of("draft", "published", "superseded", "discarded"),
    "created_at": _text,
    "published_at": _optional(_text),
    "entries": _integer,
    "founders": _integer,
    "founder_rule_confirmed": _boolean,
}
_PAIR_PHOTO: dict[str, _Coerce] = {
    "id": _integer,
    "moderation": _one_of("pending", "approved", "rejected"),
    "on_screen": _boolean,
}
_OFFLINE_SCAN: dict[str, _Coerce] = {
    "id": _integer,
    "status": _one_of("pending_review", "applied", "rejected", "failed"),
    "reason": _optional(_text),
    # What the phone claimed and what the reviewer verified, side by side.
    "device_scanned_at": _optional(_text),
    "verified_at": _optional(_text),
    "outcome": _optional(_text),
}
_BOOTH_LEADS: dict[str, _Coerce] = {"id": _integer, "leads": _integer}
_QMETER: dict[str, _Coerce] = {
    "id": _integer,
    "user_id": _integer,
    "panel_id": _integer,
    "points": _integer,
    "reason": _text,
}

_MANUAL_POINTS: dict[str, _Coerce] = {
    "id": _integer,
    "user_id": _integer,
    "source": _one_of("PROFILE", "PANEL", "QMETER", "MASTERCLASS", "PLAYGROUND", "CONNECTION"),
    "points": _integer,
    "reason": _text,
}

# action -> allowlist. A writer that adds an action must add it here (a test runs
# every admin operation and fails on a kind that is missing); until then the
# event is shown as metadata only.
FIELD_ALLOWLISTS: Mapping[str, Mapping[str, _Coerce]] = {
    "staff.create": _STAFF,
    "staff.deactivate": _STAFF,
    "booth.create": _BOOTH,
    "booth.update": _BOOTH,
    "booth.weight": _WEIGHT,
    "booth.weight.remove": _WEIGHT,
    "interest.create": _INTEREST,
    "interest.update": _INTEREST,
    "task.create": _TASK,
    "task.update": _TASK,
    "preset.create": _PRESET,
    "preset.update": _PRESET,
    "event_config.update": _CONFIG,
    "points.reverse": _REVERSAL,
    "staff.password_change": _PASSWORD_CHANGED,
    "staff.password_reset": _PASSWORD_CHANGED,
    "staff.password_reset_requested": _PASSWORD_RESET_REQUESTED,
    "masterclass_event.create": _MC_EVENT,
    "masterclass_event.update": _MC_EVENT,
    "masterclass_block.create": _MC_BLOCK,
    "masterclass_block.update": _MC_BLOCK,
    "masterclass_room.create": _MC_ROOM,
    "masterclass_room.update": _MC_ROOM,
    "masterclass.create": _MASTERCLASS,
    "masterclass.update": _MASTERCLASS,
    "masterclass.delete": _MASTERCLASS,
    "panel.create": _PANEL,
    "panel.update": _PANEL,
    "staff_scope.grant": _SCOPE,
    "staff_scope.revoke": _SCOPE,
    "competition.eligibility": _ELIGIBILITY,
    "competition.qmeter": _QMETER,
    "competition.manual_points": _MANUAL_POINTS,
    "match_table.create": _MATCH_TABLE,
    "match_table.update": _MATCH_TABLE,
    "mentor_session.create": _MENTOR_SESSION,
    "mentor_session.update": _MENTOR_SESSION,
    "final.create": _FINAL,
    "final.publish": _FINAL,
    "final.discard": _FINAL,
    "final.export": _FINAL,
    "pair_photo.moderate": _PAIR_PHOTO,
    "offline_scan.approve": _OFFLINE_SCAN,
    "offline_scan.reject": _OFFLINE_SCAN,
    "booth.leads": _BOOTH_LEADS,
}
EXPORT_ACTIONS = ("export.started", "export.finished", "export.rejected")
KNOWN_ACTIONS: tuple[str, ...] = (*FIELD_ALLOWLISTS, *EXPORT_ACTIONS)

_EXPORT_KINDS = ("attendees", "points-log", "sponsor-contacts")
_EXPORT_OUTCOMES = ("stream_finished", "interrupted", "failed")
_FILTER_NAMES = frozenset(AttendeeFilters.model_fields) | frozenset(PointsFilters.model_fields)
_WORD = re.compile(r"[a-z][a-z0-9_]{0,63}")
_CLASS_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,79}")


def _count(value: Any) -> int | None:
    ok, shown = _integer(value)
    return shown if ok and isinstance(shown, int) and shown >= 0 else None


def _word(value: Any, pattern: re.Pattern[str]) -> str | None:
    return value if isinstance(value, str) and pattern.fullmatch(value) else None


def _moment(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _object(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


# --- serialisers ---------------------------------------------------------------


def _shown(
    allow: Mapping[str, _Coerce], raw: dict[str, Any] | None
) -> dict[str, AuditValue] | None:
    """The allowlisted, well-typed part of `raw`."""
    if raw is None:
        return None
    shown: dict[str, AuditValue] = {}
    for name, value in raw.items():
        coerce = allow.get(name)
        if coerce is None:
            continue
        ok, clean = coerce(value)
        if ok:
            shown[name] = clean
    return shown


def _changed(
    before: dict[str, AuditValue] | None, after: dict[str, AuditValue] | None
) -> list[str]:
    """The fields the event changed. A create (no "before") or a removal (no
    "after") changed every field it shows. An update changed the fields whose
    value differs; a field only one side recorded is context, not a change (the
    writer of `booth.weight` stores just the weight as "before" but the ids too
    as "after")."""
    if before is None or after is None:
        return sorted((before or after or {}).keys())
    return sorted(name for name in before.keys() & after.keys() if before[name] != after[name])


def _export_result(action: str, raw: dict[str, Any]) -> tuple[AuditExportResult, int]:
    """The outcome and the counts of an export event; nothing else of the row.
    (`filters_used` carries only the NAMES of the filters that were set - the
    exporting code never stored their values.)"""
    kind = raw.get("kind")
    used = raw.get("filters_used")
    filters = (
        sorted(n for n in used if isinstance(n, str) and n in _FILTER_NAMES)
        if isinstance(used, list)
        else []
    )
    outcome: str | None
    if action == "export.started":
        outcome = "started"
    elif action == "export.rejected":
        outcome = "rejected"
    else:
        found = raw.get("outcome")
        outcome = found if isinstance(found, str) and found in _EXPORT_OUTCOMES else None
    result = AuditExportResult(
        kind=kind if isinstance(kind, str) and kind in _EXPORT_KINDS else None,
        outcome=outcome,
        reason=_word(raw.get("reason"), _WORD),
        error_class=_word(raw.get("error_class"), _CLASS_NAME),
        rows_expected=_count(raw.get("rows_expected")),
        rows_written=_count(raw.get("rows_written")),
        rows_matched=_count(raw.get("rows_matched")),
        max_rows=_count(raw.get("max_rows")),
        bytes_written=_count(raw.get("bytes_written")),
        elapsed_ms=_count(raw.get("elapsed_ms")),
        snapshot_at=_moment(raw.get("snapshot_at")),
        filters_used=filters,
    )
    named = {
        "kind", "filters_used", "outcome", "reason", "error_class", "rows_expected",
        "rows_written", "rows_matched", "max_rows", "bytes_written", "elapsed_ms", "snapshot_at",
    }  # fmt: skip
    return result, sum(1 for name in raw if name not in named)


@dataclass(frozen=True)
class AuditRow:
    id: int
    created_at: datetime
    actor_id: int
    actor_name: str | None
    action: str
    target_type: str
    target_id: str | None
    before_value: Any
    after_value: Any


def serialize(row: AuditRow) -> AuditEvent:
    """One audit row as the client may see it (see the module docstring)."""
    before_raw, after_raw = _object(row.before_value), _object(row.after_value)
    unreadable = _unreadable(row.before_value, row.after_value)
    base: dict[str, Any] = {
        "id": row.id,
        "created_at": row.created_at,
        "actor_id": row.actor_id,
        "actor_name": _text(row.actor_name)[1] if row.actor_name else None,
        "action": _text(row.action)[1],
        "target_type": _text(row.target_type)[1],
        "target_id": _text(row.target_id)[1] if row.target_id is not None else None,
    }
    allow = FIELD_ALLOWLISTS.get(row.action)
    if allow is not None:
        before, after = _shown(allow, before_raw), _shown(allow, after_raw)
        return AuditEvent(
            **base,
            operation_id=None,
            before=before,
            after=after,
            changed_fields=_changed(before, after),
            result=None,
            hidden_field_count=len(_held_names(allow, before_raw, after_raw)) + unreadable,
            details="known",
        )
    if row.action in EXPORT_ACTIONS:
        result, extra = _export_result(row.action, after_raw or {})
        return AuditEvent(
            **base,
            # started / finished / rejected of ONE export share the target id: that
            # is the id of the operation, and what ties its events together.
            operation_id=row.target_id if row.target_type == "export" else None,
            before=None,
            after=None,
            changed_fields=[],
            result=result,
            hidden_field_count=extra + len(before_raw or {}) + unreadable,
            details="known",
        )
    # Unknown kind: metadata only. How many fields there were is the one thing said.
    names = (before_raw or {}).keys() | (after_raw or {}).keys()
    return AuditEvent(
        **base,
        operation_id=None,
        before=None,
        after=None,
        changed_fields=[],
        result=None,
        hidden_field_count=len(names) + unreadable,
        details="limited",
    )


def _held_names(
    allow: Mapping[str, _Coerce], before: dict[str, Any] | None, after: dict[str, Any] | None
) -> set[str]:
    """Distinct field names withheld from either side (a name counted once even
    when both the before and the after value had it)."""
    held: set[str] = set()
    for raw in (before, after):
        for name, value in (raw or {}).items():
            coerce = allow.get(name)
            if coerce is None or not coerce(value)[0]:
                held.add(name)
    return held


def _unreadable(*stored: Any) -> int:
    """A stored value that is not even a JSON object (a malformed old row) counts
    as one withheld item."""
    return sum(1 for value in stored if value is not None and not isinstance(value, dict))


# --- the list ------------------------------------------------------------------


def audit_page(db: Session, query: AuditListQuery) -> AuditPage:
    """The audit trail, newest first, keyset-paginated. Read-only."""
    filters = {
        "actor_id": query.actor_id,
        "action": query.action,
        "target_type": query.target_type,
        "target_id": query.target_id,
        "created_from": query.created_from,
        "created_to": query.created_to,
    }
    fingerprint = filter_fingerprint(KIND, filters)
    stmt = select(
        AdminAudit.id,
        AdminAudit.created_at,
        AdminAudit.staff_account_id,
        StaffAccount.full_name,
        AdminAudit.action,
        AdminAudit.target_type,
        AdminAudit.target_id,
        AdminAudit.before_value,
        AdminAudit.after_value,
    ).outerjoin(StaffAccount, StaffAccount.id == AdminAudit.staff_account_id)
    if query.actor_id is not None:
        stmt = stmt.where(AdminAudit.staff_account_id == query.actor_id)
    if query.action is not None:
        stmt = stmt.where(AdminAudit.action == query.action)
    if query.target_type is not None:
        stmt = stmt.where(AdminAudit.target_type == query.target_type)
    if query.target_id is not None:
        stmt = stmt.where(AdminAudit.target_id == query.target_id)
    if query.created_from is not None:
        stmt = stmt.where(AdminAudit.created_at >= query.created_from)
    if query.created_to is not None:
        stmt = stmt.where(AdminAudit.created_at < query.created_to)
    if query.cursor is not None:
        at, row_id = decode_cursor(KIND, query.cursor, fingerprint=fingerprint)
        stmt = stmt.where(
            tuple_(AdminAudit.created_at, AdminAudit.id)
            < tuple_(literal(at, DateTime(timezone=True)), literal(row_id, Integer))
        )
    rows = db.execute(
        stmt.order_by(AdminAudit.created_at.desc(), AdminAudit.id.desc()).limit(query.limit + 1)
    ).all()
    page, more = rows[: query.limit], len(rows) > query.limit
    next_cursor = (
        encode_cursor(
            KIND, created_at=page[-1].created_at, row_id=page[-1].id, fingerprint=fingerprint
        )
        if more
        else None
    )
    return AuditPage(
        items=[
            serialize(
                AuditRow(
                    id=r.id,
                    created_at=r.created_at,
                    actor_id=r.staff_account_id,
                    actor_name=r.full_name,
                    action=r.action,
                    target_type=r.target_type,
                    target_id=r.target_id,
                    before_value=r.before_value,
                    after_value=r.after_value,
                )
            )
            for r in page
        ],
        next_cursor=next_cursor,
        limit=query.limit,
    )
