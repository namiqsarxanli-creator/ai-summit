"""The attendee edits their OWN profile: details, interests, sponsor consent.

Real PostgreSQL, real registration and activation. What is pinned here:

* only the signed-in attendee's own row can be edited - there is no id in the
  request and no e-mail field;
* the same validation as registration, plus a clear error for an unknown or
  retired interest;
* one writer at a time and a stale form never wins (`expected_version`);
* a change that changes nothing writes nothing;
* consent: value + timestamp + version + audit row are one transaction, a
  repeated value writes no audit row, and the sponsor export follows the value.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.db.session import SessionLocal
from app.modules.admin import exports
from app.modules.admin.schemas import SponsorExportRequest
from tests.m04_support import (
    assert_fenced_before_any_read,
    execute,
    fetch_all,
    fetch_one,
    make_client,
    register_payload,
)
from tests.profile_support import (
    CONSENT,
    PROFILE,
    attendee_fixture,  # noqa: F401 - fixture
    emails_fixture,  # noqa: F401 - fixture
    error_of,
    profile_body,
)

Attendee = Callable[..., tuple[str, TestClient]]
Emails = Callable[[], str]


def _profile(client: TestClient) -> dict[str, Any]:
    response = client.get(PROFILE)
    assert response.status_code == 200
    return dict(response.json())


def _put(client: TestClient, body: dict[str, Any]) -> Any:
    return client.put(PROFILE, json=body)


@pytest.fixture
def retired_interest() -> Iterator[str]:
    """An interest that exists but has been retired from the catalogue."""
    slug = f"retired-{uuid.uuid4().hex[:8]}"
    execute(
        "INSERT INTO interests (slug, name, sort_order, is_active) "
        "VALUES (:s, 'Retired', 900, false)",
        s=slug,
    )
    yield slug
    execute(
        "DELETE FROM user_interests "
        "WHERE interest_id IN (SELECT id FROM interests WHERE slug = :s)",
        s=slug,
    )
    execute("DELETE FROM interests WHERE slug = :s", s=slug)


# --- who can edit what -----------------------------------------------------------


def test_editing_requires_an_attendee_session() -> None:
    with make_client() as anonymous:
        body: dict[str, Any] = {
            "first_name": "A",
            "last_name": "B",
            "phone": "1",
            "profession": "C",
            "linkedin_url": None,
            "interest_slugs": [],
            "expected_version": 1,
        }
        assert error_of(anonymous.put(PROFILE, json=body)) == (401, "UNAUTHENTICATED")
        assert error_of(anonymous.put(CONSENT, json={"granted": True})) == (401, "UNAUTHENTICATED")


def test_a_provisional_registration_cannot_edit(emails: Emails) -> None:
    email = emails()
    with make_client() as c:
        assert c.post("/api/attendees/register", json=register_payload(email)).status_code == 200
        assert error_of(c.put(CONSENT, json={"granted": True})) == (401, "UNAUTHENTICATED")


def test_an_attendee_edits_their_own_profile_and_nobody_elses(
    emails: Emails, attendee: Attendee
) -> None:
    _, mine = attendee(emails())
    other_email, theirs = attendee(emails(), first_name="Ada", profession="Mathematician")
    before_other = _profile(theirs)

    profile = _profile(mine)
    response = _put(
        mine,
        profile_body(
            profile,
            first_name="  Margaret  ",
            last_name="Hamilton",
            phone="+994 50 000 00 00",
            profession="Flight Software Lead",
            linkedin_url="https://www.linkedin.com/in/margaret-demo",
            interest_slugs=["generative-ai"],
        ),
    )
    assert response.status_code == 200, response.text
    saved = response.json()
    assert saved["first_name"] == "Margaret"  # whitespace stripped, like registration
    assert saved["last_name"] == "Hamilton"
    assert saved["phone"] == "+994 50 000 00 00"
    assert saved["profession"] == "Flight Software Lead"
    assert [i["slug"] for i in saved["interests"]] == ["generative-ai"]
    assert saved["version"] == profile["version"] + 1
    assert _profile(mine) == saved

    # the other attendee's row is exactly as it was
    assert _profile(theirs) == before_other
    row = fetch_one("SELECT first_name, profession FROM users WHERE email = :e", e=other_email)
    assert tuple(row) == ("Ada", "Mathematician")


@pytest.mark.parametrize(
    "extra",
    [
        {"email": "someone-else@test.facemark.demo"},
        {"id": 1},
        {"user_id": 1},
        {"sponsor_consent": True},
        {"photo_key": "0" * 32},
        {"status": "ACTIVE"},
    ],
)
def test_the_body_cannot_name_anyone_or_carry_fields_that_are_not_editable(
    emails: Emails, attendee: Attendee, extra: dict[str, Any]
) -> None:
    email, client = attendee(emails())
    profile = _profile(client)
    response = _put(client, {**profile_body(profile), **extra})
    assert response.status_code == 422
    assert fetch_one("SELECT email::text FROM users WHERE email = :e", e=email)[0] == email
    assert _profile(client) == profile  # nothing moved


# --- validation: the same rules as registration ---------------------------------------


INVALID_FIELDS: list[dict[str, Any]] = [
    {"first_name": ""},
    {"first_name": "   "},
    {"first_name": "x" * 201},
    {"last_name": ""},
    {"last_name": "x" * 201},
    {"phone": ""},
    {"phone": "5" * 51},
    {"profession": "   "},
    {"profession": "p" * 201},
    {"linkedin_url": "https://example.com/in/someone"},
    {"linkedin_url": "javascript:alert(1)"},
    {"linkedin_url": "not a url"},
]


@pytest.mark.parametrize("bad", INVALID_FIELDS)
def test_validation_matches_registration(
    emails: Emails, attendee: Attendee, bad: dict[str, Any]
) -> None:
    """Whatever registration refuses, the editor refuses - same rule, same status."""
    _, client = attendee(emails())
    profile = _profile(client)
    edited = _put(client, profile_body(profile, **bad))
    assert edited.status_code == 422, bad

    with make_client() as anonymous:
        registered = anonymous.post(
            "/api/attendees/register", json=register_payload(emails(), **bad)
        )
    assert registered.status_code == 422, bad
    assert _profile(client) == profile


def test_a_blank_linkedin_means_none_and_a_valid_one_is_kept(
    emails: Emails, attendee: Attendee
) -> None:
    _, client = attendee(emails())
    profile = _profile(client)
    cleared = _put(client, profile_body(profile, linkedin_url="  "))
    assert cleared.status_code == 200
    assert cleared.json()["linkedin_url"] is None
    restored = _put(
        client,
        profile_body(cleared.json(), linkedin_url="https://www.linkedin.com/in/grace-hopper-demo"),
    )
    assert restored.json()["linkedin_url"] == "https://www.linkedin.com/in/grace-hopper-demo"


def test_the_version_is_required_and_positive(emails: Emails, attendee: Attendee) -> None:
    _, client = attendee(emails())
    body = profile_body(_profile(client))
    del body["expected_version"]
    assert _put(client, body).status_code == 422
    assert _put(client, {**body, "expected_version": 0}).status_code == 422


# --- interests ------------------------------------------------------------------------


def test_an_unknown_interest_is_refused_by_name_and_nothing_is_saved(
    emails: Emails, attendee: Attendee
) -> None:
    _, client = attendee(emails())
    profile = _profile(client)
    response = _put(
        client,
        profile_body(
            profile, first_name="Changed", interest_slugs=["generative-ai", "no-such-thing"]
        ),
    )
    assert error_of(response) == (400, "UNKNOWN_INTEREST")
    assert response.json()["error"]["details"] == {"slugs": ["no-such-thing"]}
    assert _profile(client) == profile  # the first name did not change either


def test_a_retired_interest_cannot_be_newly_picked_but_one_already_held_stays(
    emails: Emails, attendee: Attendee, retired_interest: str
) -> None:
    email, client = attendee(emails())
    profile = _profile(client)

    refused = _put(
        client,
        profile_body(
            profile, interest_slugs=[*[i["slug"] for i in profile["interests"]], retired_interest]
        ),
    )
    assert error_of(refused) == (400, "UNKNOWN_INTEREST")
    assert refused.json()["error"]["details"] == {"slugs": [retired_interest]}

    # the attendee already holds it (chosen before it was retired): saving the
    # form again must not fail because of it
    execute(
        "INSERT INTO user_interests (user_id, interest_id) "
        "SELECT u.id, i.id FROM users u, interests i WHERE u.email = :e AND i.slug = :s",
        e=email,
        s=retired_interest,
    )
    held = _profile(client)
    assert retired_interest in [i["slug"] for i in held["interests"]]
    kept = _put(client, profile_body(held, profession="Still here"))
    assert kept.status_code == 200
    assert retired_interest in [i["slug"] for i in kept.json()["interests"]]

    # ... and it can be removed
    removed = _put(
        client,
        profile_body(
            kept.json(),
            interest_slugs=[
                i["slug"] for i in kept.json()["interests"] if i["slug"] != retired_interest
            ],
        ),
    )
    assert retired_interest not in [i["slug"] for i in removed.json()["interests"]]


def test_interests_can_be_emptied_and_duplicates_collapse(
    emails: Emails, attendee: Attendee
) -> None:
    _, client = attendee(emails())
    profile = _profile(client)
    doubled = _put(client, profile_body(profile, interest_slugs=["generative-ai", "generative-ai"]))
    assert [i["slug"] for i in doubled.json()["interests"]] == ["generative-ai"]
    emptied = _put(client, profile_body(doubled.json(), interest_slugs=[]))
    assert emptied.status_code == 200
    assert emptied.json()["interests"] == []


def test_recommendations_follow_a_changed_interest(emails: Emails, attendee: Attendee) -> None:
    """Matching reads the interests live (nothing is cached), so the booths an
    attendee is told about change with the very next request."""
    _, client = attendee(emails(), interest_slugs=["generative-ai"])
    first = client.get("/api/attendees/me/recommendations")
    assert first.status_code == 200
    profile = _profile(client)
    changed = _put(client, profile_body(profile, interest_slugs=["data-analytics"]))
    assert changed.status_code == 200
    second = client.get("/api/attendees/me/recommendations")
    assert second.status_code == 200
    shared_after = {
        slug
        for rec in second.json()
        for slug in (m["slug"] for m in rec.get("matched_interests", []))
    }
    assert "generative-ai" not in shared_after


# --- no-op edits ------------------------------------------------------------------------


def test_saving_what_is_already_saved_writes_nothing(emails: Emails, attendee: Attendee) -> None:
    email, client = attendee(emails())
    profile = _profile(client)
    again = _put(client, profile_body(profile))
    assert again.status_code == 200
    assert again.json() == profile  # same version, same everything
    assert (
        fetch_one("SELECT profile_version FROM users WHERE email = :e", e=email)[0]
        == profile["version"]
    )
    # a no-op is also not a conflict for the NEXT edit
    assert _put(client, profile_body(profile, profession="Next")).status_code == 200


# --- optimistic concurrency ----------------------------------------------------------------


def test_a_stale_version_is_a_conflict_that_carries_the_current_profile(
    emails: Emails, attendee: Attendee
) -> None:
    email, device_a = attendee(emails())
    device_b = make_client()
    device_b.cookies.update(device_a.cookies)
    try:
        loaded = _profile(device_a)
        assert _put(device_b, profile_body(loaded, profession="From device B")).status_code == 200

        stale = _put(device_a, profile_body(loaded, profession="From device A", first_name="Typed"))
        assert error_of(stale) == (409, "PROFILE_CONFLICT")
        current = stale.json()["error"]["details"]["profile"]
        assert current["profession"] == "From device B"
        assert current["version"] == loaded["version"] + 1
        # nothing from the stale form was saved
        row = fetch_one("SELECT first_name, profession FROM users WHERE email = :e", e=email)
        assert tuple(row) == (loaded["first_name"], "From device B")
        # the details never leak anything the profile does not already show its owner
        assert set(current) == set(loaded)

        # re-submitting on top of the current version succeeds
        retried = _put(
            device_a, profile_body(current, profession="From device A", first_name="Typed")
        )
        assert retried.status_code == 200
        assert retried.json()["version"] == current["version"] + 1
    finally:
        device_b.close()


def test_two_simultaneous_edits_of_one_version_cannot_both_win(
    emails: Emails, attendee: Attendee
) -> None:
    email, first = attendee(emails())
    second = make_client()
    second.cookies.update(first.cookies)
    try:
        loaded = _profile(first)
        barrier = threading.Barrier(2)
        results: dict[str, Any] = {}

        def edit(name: str, client: TestClient, profession: str) -> None:
            body = profile_body(loaded, profession=profession)
            barrier.wait(timeout=10)
            results[name] = _put(client, body)

        threads = [
            threading.Thread(target=edit, args=("a", first, "Edit A")),
            threading.Thread(target=edit, args=("b", second, "Edit B")),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        codes = sorted(r.status_code for r in results.values())
        assert codes == [200, 409], codes
        winner = next(r for r in results.values() if r.status_code == 200).json()
        loser = next(r for r in results.values() if r.status_code == 409).json()
        assert loser["error"]["code"] == "PROFILE_CONFLICT"
        assert loser["error"]["details"]["profile"]["profession"] == winner["profession"]
        row = fetch_one("SELECT profession, profile_version FROM users WHERE email = :e", e=email)
        assert tuple(row) == (winner["profession"], loaded["version"] + 1)
    finally:
        second.close()


def test_profile_edits_take_the_reset_fence_first(emails: Emails, attendee: Attendee) -> None:
    _, client = attendee(emails())
    profile = _profile(client)
    response = assert_fenced_before_any_read(
        lambda: _put(client, profile_body(profile, profession="Fenced"))
    )
    assert response.status_code == 200
    response = assert_fenced_before_any_read(lambda: client.put(CONSENT, json={"granted": True}))
    assert response.status_code == 200


# --- sponsor consent ----------------------------------------------------------------------------


def _audit(email: str) -> list[Any]:
    return fetch_all(
        "SELECT a.granted, a.consent_version, a.source FROM consent_audit a "
        "JOIN users u ON u.id = a.user_id WHERE u.email = :e ORDER BY a.id",
        e=email,
    )


def test_consent_is_granted_and_withdrawn_with_an_audit_row_each(
    emails: Emails, attendee: Attendee
) -> None:
    email, client = attendee(emails(), sponsor_consent=False)
    assert [tuple(r) for r in _audit(email)] == [(False, "registration-v1", "registration")]

    granted = client.put(CONSENT, json={"granted": True})
    assert granted.status_code == 200
    state = granted.json()
    assert state["sponsor_consent"] is True
    assert state["changed"] is True
    assert state["consent_version"] == "registration-v1"
    assert state["sponsor_consent_at"] is not None
    row = fetch_one(
        "SELECT sponsor_consent, sponsor_consent_at, sponsor_consent_version FROM users "
        "WHERE email = :e",
        e=email,
    )
    assert row[0] is True and row[1] is not None and row[2] == "registration-v1"
    assert [tuple(r) for r in _audit(email)][-1] == (True, "registration-v1", "settings_update")

    # the audit row and the user row carry the SAME moment
    moments = fetch_one(
        "SELECT u.sponsor_consent_at = a.changed_at FROM users u JOIN consent_audit a "
        "ON a.user_id = u.id WHERE u.email = :e AND a.source = 'settings_update'",
        e=email,
    )
    assert moments[0] is True

    withdrawn = client.put(CONSENT, json={"granted": False})
    assert withdrawn.json()["sponsor_consent"] is False
    assert withdrawn.json()["sponsor_consent_at"] is None
    assert withdrawn.json()["consent_version"] is None
    assert [tuple(r) for r in _audit(email)][-1] == (False, "registration-v1", "settings_update")
    assert len(_audit(email)) == 3
    # the profile screen reads the same state
    assert _profile(client)["sponsor_consent"] is False


def test_repeating_the_same_consent_creates_no_audit_event(
    emails: Emails, attendee: Attendee
) -> None:
    email, client = attendee(emails(), sponsor_consent=True)
    audits = len(_audit(email))
    stamp = fetch_one("SELECT sponsor_consent_at FROM users WHERE email = :e", e=email)[0]

    again = client.put(CONSENT, json={"granted": True})
    assert again.status_code == 200
    assert again.json()["changed"] is False
    assert len(_audit(email)) == audits  # no fake event
    assert fetch_one("SELECT sponsor_consent_at FROM users WHERE email = :e", e=email)[0] == stamp

    # same for a withdrawal that is already in force
    client.put(CONSENT, json={"granted": False})
    audits = len(_audit(email))
    repeated = client.put(CONSENT, json={"granted": False})
    assert repeated.json()["changed"] is False
    assert len(_audit(email)) == audits


def test_the_consent_value_must_be_a_boolean_and_nothing_else(
    emails: Emails, attendee: Attendee
) -> None:
    _, client = attendee(emails())
    assert client.put(CONSENT, json={}).status_code == 422
    assert client.put(CONSENT, json={"granted": "yes"}).status_code == 422
    assert client.put(CONSENT, json={"granted": True, "user_id": 1}).status_code == 422


def test_a_failed_audit_write_rolls_the_consent_change_back(
    emails: Emails, attendee: Attendee, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The value and its audit row are ONE transaction: if the audit row cannot
    be written, the value does not change either."""
    email, client = attendee(emails(), sponsor_consent=False)
    monkeypatch.setattr("app.modules.attendees.profile.CONSENT_VERSION", None)  # NOT NULL violation
    with make_client(raise_server_exceptions=False) as failing:
        failing.cookies.update(client.cookies)
        response = failing.put(CONSENT, json={"granted": True})
    assert response.status_code == 500
    monkeypatch.undo()
    assert fetch_one("SELECT sponsor_consent FROM users WHERE email = :e", e=email)[0] is False
    assert [tuple(r)[2] for r in _audit(email)] == ["registration"]  # no new audit row


def test_the_sponsor_export_follows_consent_and_the_edited_details(
    emails: Emails, attendee: Attendee
) -> None:
    email, client = attendee(emails(), sponsor_consent=False)

    def exported() -> list[Any]:
        db = SessionLocal()
        try:
            rows = db.execute(exports._sponsor_statement(SponsorExportRequest())).all()  # noqa: SLF001
            return [r for r in rows if str(r.email) == email]
        finally:
            db.close()

    assert exported() == []  # no consent, not exported
    client.put(CONSENT, json={"granted": True})
    assert [r.first_name for r in exported()] == ["Grace"]

    profile = _profile(client)
    _put(client, profile_body(profile, first_name="Renamed", phone="+994 55 111 11 11"))
    (row,) = exported()
    assert (row.first_name, row.phone) == ("Renamed", "+994 55 111 11 11")

    client.put(CONSENT, json={"granted": False})
    assert exported() == []  # withdrawn: gone from the very next export


def test_consent_changes_do_not_touch_the_profile_version(
    emails: Emails, attendee: Attendee
) -> None:
    """A device that is only toggling consent must not make another device's
    open profile form conflict."""
    _, client = attendee(emails())
    loaded = _profile(client)
    client.put(CONSENT, json={"granted": True})
    assert _profile(client)["version"] == loaded["version"]
    assert _put(client, profile_body(loaded, profession="Fine")).status_code == 200


def test_the_profile_response_reports_consent_time_and_version(
    emails: Emails, attendee: Attendee
) -> None:
    _, client = attendee(emails(), sponsor_consent=True)
    profile = _profile(client)
    assert profile["sponsor_consent"] is True
    assert profile["sponsor_consent_version"] == "registration-v1"
    assert profile["sponsor_consent_at"] is not None
    assert profile["version"] == 1
    assert profile["photo"] is None
