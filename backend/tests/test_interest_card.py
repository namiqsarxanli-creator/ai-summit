"""The company field and the Beyond Now interest card (migration 0016).

Registration and profile editing share one contract (`CompanyAndCard`); the card
holds stable identifiers with maximums only (1 sector, 3 topics, 2 sought, 2
offered); the older interests that drive booth recommendations are untouched;
nothing new reaches a public or exported surface.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.modules.admin import exports
from app.modules.attendees.interest_card import (
    OFFERING,
    SECTORS,
    SEEKING,
    TOPICS,
    InterestCard,
    refused_choices,
)
from app.modules.attendees.schemas import ProfileUpdate, RegisterRequest
from tests.m04_support import (
    captured_otp,
    execute,
    fetch_all,
    fetch_one,
    make_client,
    register_payload,
)
from tests.profile_support import (
    PROFILE,
    attendee_fixture,  # noqa: F401 - fixture
    emails_fixture,  # noqa: F401 - fixture
    error_of,
    profile_body,
)

Attendee = Callable[..., tuple[str, TestClient]]
Emails = Callable[[], str]

REGISTER = "/api/attendees/register"
VERIFY = "/api/auth/otp/verify"

FULL_CARD: dict[str, Any] = {
    "sector": "bank_finance",
    "topics": ["data", "ai_strategy", "fintech"],
    "seeking": ["partner", "mentor"],
    "offering": ["mentoring", "sharing_experience"],
}
EMPTY_CARD: dict[str, Any] = {"sector": None, "topics": [], "seeking": [], "offering": []}


def _profile(client: TestClient) -> dict[str, Any]:
    response = client.get(PROFILE)
    assert response.status_code == 200
    return dict(response.json())


def _stored(email: str) -> Any:
    return fetch_one(
        "SELECT company, card_sector, card_topics, card_seeking, card_offering, profession "
        "FROM users WHERE email = :e",
        e=email.lower(),
    )


# --- the catalogue -------------------------------------------------------------------


def test_the_catalogue_matches_the_documents() -> None:
    assert (len(SECTORS), len(TOPICS), len(SEEKING), len(OFFERING)) == (13, 15, 9, 10)
    for group in (SECTORS, TOPICS, SEEKING, OFFERING):
        assert len(set(group)) == len(group)
        assert all(value.isascii() and value == value.lower() for value in group)


def test_the_frontend_mirrors_the_catalogue() -> None:
    """frontend/src/lib/interestCard.ts lists the same identifiers, in order."""
    import pathlib
    import re

    source = pathlib.Path(__file__).resolve().parents[2] / "frontend/src/lib/interestCard.ts"
    if not source.exists():
        pytest.skip("frontend not present next to the backend (image build)")
    text = source.read_text(encoding="utf-8")
    for name, ids in {
        "SECTORS": SECTORS,
        "TOPICS": TOPICS,
        "SEEKING": SEEKING,
        "OFFERING": OFFERING,
    }.items():
        block = re.search(rf"export const {name} = \[(.*?)\] as const", text, re.S)
        assert block is not None, name
        assert tuple(re.findall(r'id: "([a-z_]+)"', block.group(1))) == ids, name


# --- the contract --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "values"),
    [
        ("topics", ["data", "erp", "hr", "design"]),
        ("seeking", ["partner", "mentor", "investor"]),
        ("offering", ["mentoring", "vacancy", "investment"]),
    ],
)
def test_more_than_the_maximum_is_refused(field: str, values: list[str]) -> None:
    with pytest.raises(ValidationError):
        InterestCard.model_validate({field: values})


@pytest.mark.parametrize(
    "card",
    [
        {"sector": "Bank və maliyyə"},  # a label, not an identifier
        {"topics": ["data", "Data"]},
        {"seeking": ["partner", "partner"]},
        {"offering": ["career_advice", "career_advice"]},
        {"sector": ["bank_finance", "energy"]},
        {"extra_question": []},
    ],
)
def test_malformed_or_repeated_choices_are_refused_by_the_schema(card: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        InterestCard.model_validate(card)


@pytest.mark.parametrize(
    ("card", "group"),
    [
        ({"sector": "unknown"}, "sector"),
        ({"topics": ["data", "quantum"]}, "topics"),
        ({"topics": ["friendship"]}, "topics"),  # an identifier from another group
        ({"offering": ["partner"]}, "offering"),
    ],
)
def test_choices_outside_the_catalogue_are_refused(card: dict[str, Any], group: str) -> None:
    assert group in refused_choices(InterestCard.model_validate(card))


def test_only_a_choice_already_stored_in_the_same_group_is_let_through() -> None:
    stored = InterestCard.model_validate({"sector": "old_sector", "topics": ["data", "old_topic"]})
    keep = InterestCard.model_validate({"sector": "old_sector", "topics": ["old_topic"]})
    assert refused_choices(keep, stored) == {}
    added = InterestCard.model_validate({"topics": ["old_topic", "new_unknown"]})
    assert refused_choices(added, stored) == {"topics": ["new_unknown"]}
    moved = InterestCard.model_validate({"seeking": ["old_topic"]})
    assert refused_choices(moved, stored) == {"seeking": ["old_topic"]}
    # Registration has nothing stored: the retired choice is refused there.
    assert refused_choices(keep) == {"sector": ["old_sector"], "topics": ["old_topic"]}


def test_every_group_is_optional() -> None:
    assert InterestCard.model_validate({}).model_dump() == EMPTY_CARD
    assert InterestCard.model_validate(FULL_CARD).model_dump() == FULL_CARD


def test_registration_and_profile_editing_share_one_contract() -> None:
    reg = RegisterRequest.model_fields
    edit = ProfileUpdate.model_fields
    for name in ("company", "interest_card"):
        assert reg[name].annotation == edit[name].annotation
    long = "x" * 201
    for model, base in (
        (RegisterRequest, register_payload("a@x.demo")),
        (
            ProfileUpdate,
            {
                "first_name": "A",
                "last_name": "B",
                "phone": "1",
                "profession": "P",
                "interest_slugs": [],
                "expected_version": 1,
            },
        ),
    ):
        assert model.model_validate({**base, "company": "  Acme  "}).company == "Acme"
        # Required whenever sent (owner, 23 Oct 2026): blank or null is refused.
        for blank in ("   ", "", None):
            with pytest.raises(ValidationError):
                model.model_validate({**base, "company": blank})
        with pytest.raises(ValidationError):
            model.model_validate({**base, "company": long})
        with pytest.raises(ValidationError):
            model.model_validate({**base, "interest_card": {"topics": ["data"] * 2}})


# --- registration ------------------------------------------------------------------------


def test_registration_stores_the_company_and_card_and_keeps_the_role_separate(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    body = register_payload(unique_email, company="Kapital Bank", interest_card=FULL_CARD)
    assert client.post(REGISTER, json=body).status_code == 200
    code = captured_otp(otp_log.records, unique_email)
    assert client.post(VERIFY, json={"email": unique_email, "code": code}).status_code == 200

    row = _stored(unique_email)
    assert row.company == "Kapital Bank"
    assert row.profession == body["profession"]  # never merged with the company
    assert (row.card_sector, row.card_topics, row.card_seeking, row.card_offering) == (
        FULL_CARD["sector"],
        FULL_CARD["topics"],
        FULL_CARD["seeking"],
        FULL_CARD["offering"],
    )
    profile = _profile(client)
    assert profile["company"] == "Kapital Bank"
    assert profile["interest_card"] == FULL_CARD


def test_registration_requires_a_company_but_not_a_card(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    body = register_payload(unique_email)
    for missing in ({k: v for k, v in body.items() if k != "company"}, {**body, "company": "  "}):
        refused = client.post(REGISTER, json=missing)
        assert refused.status_code == 422
        assert "company" in str(refused.json())
    assert client.post(REGISTER, json=body).status_code == 200
    row = _stored(unique_email)
    assert row.company == body["company"]
    assert (row.card_sector, row.card_topics, row.card_seeking, row.card_offering) == (
        None,
        [],
        [],
        [],
    )


def test_registration_refuses_a_card_over_the_limits(client: TestClient, unique_email: str) -> None:
    body = register_payload(unique_email, interest_card={"topics": ["data", "erp", "hr", "design"]})
    response = client.post(REGISTER, json=body)
    assert response.status_code == 422
    locs = [e["loc"] for e in response.json()["error"]["details"]["errors"]]
    assert any(loc[:3] == ["body", "interest_card", "topics"] for loc in locs)
    assert fetch_one("SELECT count(*) FROM users WHERE email = :e", e=unique_email)[0] == 0


def test_registration_refuses_a_choice_outside_the_catalogue(
    client: TestClient, unique_email: str
) -> None:
    body = register_payload(unique_email, interest_card={"topics": ["data", "quantum"]})
    response = client.post(REGISTER, json=body)
    assert error_of(response) == (422, "VALIDATION_ERROR")
    errors = response.json()["error"]["details"]["errors"]
    assert [e["loc"] for e in errors] == [["body", "interest_card", "topics"]]
    assert "quantum" not in response.text  # the value is not echoed back
    assert fetch_one("SELECT count(*) FROM users WHERE email = :e", e=unique_email)[0] == 0


def test_a_forged_resubmission_cannot_change_the_owners_company_or_card(
    client: TestClient, unique_email: str, otp_log: pytest.LogCaptureFixture
) -> None:
    """The registration-intent binding (migration 0015) covers the new fields too."""
    owner = register_payload(unique_email, company="Owner Co", interest_card=FULL_CARD)
    client.post(REGISTER, json=owner)
    with make_client() as other_browser:
        other_browser.post(
            REGISTER,
            json=register_payload(
                unique_email, company="Forged", interest_card={"sector": "energy"}
            ),
        )
    code = captured_otp(otp_log.records, unique_email)
    assert client.post(VERIFY, json={"email": unique_email, "code": code}).status_code == 200
    row = _stored(unique_email)
    assert row.company == "Owner Co"
    assert row.card_sector == "bank_finance"
    assert row.card_topics == FULL_CARD["topics"]


# --- profile editing ----------------------------------------------------------------------


def test_editing_saves_the_card_and_bumps_the_version(attendee: Attendee, emails: Emails) -> None:
    email, client = attendee(emails())
    before = _profile(client)
    response = client.put(
        PROFILE, json=profile_body(before, company="AIWorks", interest_card=FULL_CARD)
    )
    assert response.status_code == 200
    after = response.json()
    assert after["version"] == before["version"] + 1
    assert after["company"] == "AIWorks"
    assert after["interest_card"] == FULL_CARD

    # The company cannot be emptied again (it is required); an empty card is valid.
    blank = client.put(PROFILE, json=profile_body(after, company="", interest_card=EMPTY_CARD))
    assert blank.status_code == 422
    cleared = client.put(
        PROFILE, json=profile_body(after, company="AIWorks", interest_card=EMPTY_CARD)
    ).json()
    assert cleared["company"] == "AIWorks"
    assert cleared["interest_card"] == EMPTY_CARD
    assert cleared["version"] == after["version"] + 1


def test_saving_the_same_card_again_changes_nothing(attendee: Attendee, emails: Emails) -> None:
    email, client = attendee(emails(), company="X", interest_card=FULL_CARD)
    before = _profile(client)
    again = client.put(
        PROFILE, json=profile_body(before, company="X", interest_card=FULL_CARD)
    ).json()
    assert again["version"] == before["version"]


def test_a_client_that_predates_the_card_leaves_it_as_it_is(
    attendee: Attendee, emails: Emails
) -> None:
    """A cached page of the app sends no company and no card: both stay."""
    email, client = attendee(emails(), company="Keep Co", interest_card=FULL_CARD)
    before = _profile(client)
    edited = client.put(PROFILE, json=profile_body(before, profession="New role")).json()
    assert edited["profession"] == "New role"
    assert edited["company"] == "Keep Co"
    assert edited["interest_card"] == FULL_CARD


def test_a_stale_version_is_still_a_conflict_and_saves_nothing(
    attendee: Attendee, emails: Emails
) -> None:
    email, client = attendee(emails())
    before = _profile(client)
    assert client.put(PROFILE, json=profile_body(before, company="First")).status_code == 200
    stale = client.put(
        PROFILE, json=profile_body(before, company="Second", interest_card=FULL_CARD)
    )
    assert error_of(stale) == (409, "PROFILE_CONFLICT")
    assert stale.json()["error"]["details"]["profile"]["company"] == "First"
    row = _stored(email)
    assert row.company == "First"
    assert row.card_topics == []


def test_the_card_limits_are_checked_on_editing_too(attendee: Attendee, emails: Emails) -> None:
    email, client = attendee(emails())
    before = _profile(client)
    for card in (
        {"seeking": ["partner", "mentor", "investor"]},
        {"offering": ["vacancy", "vacancy"]},
        {"sector": "nope"},
    ):
        response = client.put(PROFILE, json=profile_body(before, interest_card=card))
        assert error_of(response) == (422, "VALIDATION_ERROR")
    assert _profile(client)["version"] == before["version"]


def test_an_older_profile_with_many_interests_can_still_be_edited(
    attendee: Attendee, emails: Emails
) -> None:
    """The card's 3-topic maximum is not applied to the older interests: an
    attendee who picked more than three of those keeps them all and can still
    edit every other field."""
    slugs = [r[0] for r in fetch_all("SELECT slug FROM interests WHERE is_active ORDER BY id")]
    assert len(slugs) > 3
    email, client = attendee(emails(), interest_slugs=slugs)
    before = _profile(client)
    response = client.put(
        PROFILE, json=profile_body(before, company="Many Interests Ltd", interest_card=FULL_CARD)
    )
    assert response.status_code == 200
    assert sorted(i["slug"] for i in response.json()["interests"]) == sorted(slugs)


@pytest.mark.parametrize("legacy", [False, True])
def test_card_topics_add_to_booth_matching_but_never_change_the_profile_interests(
    attendee: Attendee, emails: Emails, monkeypatch: pytest.MonkeyPatch, legacy: bool
) -> None:
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "booth_match_profile_interests", legacy)
    email, client = attendee(emails(), interest_slugs=["generative-ai", "data-analytics"])
    before = _profile(client)

    def aiworks_matched() -> set[str]:
        recs = client.get("/api/attendees/me/recommendations").json()
        match = next(r["match"] for r in recs if r["booth"]["slug"] == "aiworks")
        return {i["slug"] for i in (match or {}).get("matched_interests", [])}

    assert aiworks_matched() == ({"generative-ai", "data-analytics"} if legacy else set())
    # FULL_CARD topics: data, ai_strategy (-> artificial-intelligence, data-analytics), fintech.
    client.put(PROFILE, json=profile_body(before, interest_card=FULL_CARD))
    after = _profile(client)
    assert [i["slug"] for i in after["interests"]] == [i["slug"] for i in before["interests"]]
    expected = {"data-analytics", "artificial-intelligence"}
    assert aiworks_matched() == (expected | {"generative-ai"} if legacy else expected)


# --- nothing new leaks -----------------------------------------------------------------------


def test_the_public_card_and_leaderboard_show_no_company_or_card(
    attendee: Attendee, emails: Emails
) -> None:
    email, client = attendee(emails(), company="Secret Co", interest_card=FULL_CARD)
    token = fetch_one(
        "SELECT c.public_token FROM contact_cards c JOIN users u ON u.id = c.user_id "
        "WHERE u.email = :e",
        e=email,
    )[0]
    public = make_client().get(f"/api/cards/{token}")
    assert public.status_code == 200
    board = client.get("/api/leaderboard")
    me = client.get("/api/attendees/me")
    for response in (public, board, me):
        text = response.text
        assert "Secret Co" not in text
        assert "company" not in text
        for identifier in ("bank_finance", "ai_strategy", "sharing_experience", "interest_card"):
            assert identifier not in text


def test_no_export_carries_the_company_or_the_card() -> None:
    for kind, spec in exports.SPECS.items():
        names = [name for name, _ in spec.columns]
        assert not any("company" in n or n.startswith("card_") for n in names), kind


def test_card_values_are_never_logged(
    client: TestClient, unique_email: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG")
    client.post(
        REGISTER,
        json=register_payload(unique_email, company="LogCheck Co", interest_card=FULL_CARD),
    )
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "LogCheck Co" not in logged
    assert "ai_strategy" not in logged


# --- a choice retired from the catalogue after it was saved --------------------------------


def _retire(email: str, *, sector: str | None, topics: list[str]) -> None:
    """Stands in for a catalogue change: the stored card keeps identifiers the
    current catalogue no longer has (the database holds no catalogue)."""
    execute(
        "UPDATE users SET card_sector = :s, card_topics = :t WHERE email = :e",
        s=sector,
        t=topics,
        e=email.lower(),
    )


def test_a_retired_choice_never_blocks_editing_the_rest_of_the_profile(
    attendee: Attendee, emails: Emails
) -> None:
    email, client = attendee(emails())
    _retire(email, sector="retired_sector", topics=["data", "retired_topic"])
    before = _profile(client)
    assert before["interest_card"]["topics"] == ["data", "retired_topic"]
    response = client.put(
        PROFILE,
        json=profile_body(before, profession="New role", interest_card=before["interest_card"]),
    )
    assert response.status_code == 200, response.text
    after = response.json()
    assert after["profession"] == "New role"
    assert after["interest_card"]["sector"] == "retired_sector"
    assert after["interest_card"]["topics"] == ["data", "retired_topic"]


def test_a_retired_choice_can_be_removed_but_never_picked_again(
    attendee: Attendee, emails: Emails
) -> None:
    email, client = attendee(emails())
    _retire(email, sector="retired_sector", topics=["data", "retired_topic"])
    before = _profile(client)
    removed = client.put(
        PROFILE,
        json=profile_body(before, interest_card={"sector": "energy", "topics": ["data", "erp"]}),
    )
    assert removed.status_code == 200, removed.text
    after = removed.json()
    assert after["interest_card"]["sector"] == "energy"
    assert after["interest_card"]["topics"] == ["data", "erp"]
    for card in ({"sector": "retired_sector"}, {"topics": ["data", "retired_topic"]}):
        again = client.put(PROFILE, json=profile_body(after, interest_card=card))
        assert error_of(again) == (422, "VALIDATION_ERROR")
    assert _profile(client)["version"] == after["version"]


def test_a_retired_choice_is_no_way_round_the_limits_or_the_catalogue(
    attendee: Attendee, emails: Emails
) -> None:
    email, client = attendee(emails())
    _retire(email, sector=None, topics=["data", "erp", "retired_topic"])
    before = _profile(client)
    for card in (
        # a fourth topic: the retired one counts towards the maximum
        {"topics": ["data", "erp", "retired_topic", "hr"]},
        # a new unknown choice next to the kept one
        {"topics": ["data", "retired_topic", "quantum"]},
        # the retired topic moved into another group
        {"topics": ["data"], "seeking": ["retired_topic"]},
        # repeated
        {"topics": ["retired_topic", "retired_topic"]},
    ):
        response = client.put(PROFILE, json=profile_body(before, interest_card=card))
        assert error_of(response) == (422, "VALIDATION_ERROR"), card
    row = _stored(email)
    assert row.card_topics == ["data", "erp", "retired_topic"]
    assert row.card_seeking == []
    # Swapping a known topic while keeping the retired one stays within the rules.
    swapped = client.put(
        PROFILE, json=profile_body(before, interest_card={"topics": ["hr", "erp", "retired_topic"]})
    )
    assert swapped.status_code == 200, swapped.text


# --- the database holds the limits too ---------------------------------------------------------


def test_the_database_refuses_a_card_over_the_limits(attendee: Attendee, emails: Emails) -> None:
    email, _ = attendee(emails())
    with pytest.raises(Exception, match="card_topics_max"):
        execute("UPDATE users SET card_topics = ARRAY['a','b','c','d'] WHERE email = :e", e=email)


def test_every_topic_names_booth_interests_of_the_catalogue() -> None:
    """Each card topic feeds booth recommendations through catalogue slugs."""
    from app.modules.attendees.interest_card import TOPIC_BOOTH_INTERESTS
    from app.seed import demo_fixtures as fx

    assert set(TOPIC_BOOTH_INTERESTS) == set(TOPICS)
    catalogue = {i.slug for i in fx.INTERESTS}
    for topic, slugs in TOPIC_BOOTH_INTERESTS.items():
        assert slugs, topic
        assert set(slugs) <= catalogue, topic
