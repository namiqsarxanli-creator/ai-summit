"""M07 acceptance: booths, interests and matching — ARCHITECTURE.md §10,
§11, IMPLEMENTATION_PLAN.md M07.

Two layers:
- Pure `core.py` unit tests: no database, literals only (§11.2).
- `service.py` / HTTP integration tests: real PostgreSQL, the seeded
  demo fixtures as golden vectors (§19.9), and the M04/M05 session
  machinery for the "personalized, from the session only" requirement.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from hypothesis import given
from hypothesis import strategies as st

from app.modules.matching import core
from app.modules.matching.service import recommendations_for
from app.seed import demo_fixtures as fx
from tests.m04_support import (
    captured_otp,
    cleanup_email,
    execute,
    fetch_one,
    make_client,
    new_email,
    register_payload,
)

REGISTER = "/api/attendees/register"
VERIFY = "/api/auth/otp/verify"
RECOMMENDATIONS = "/api/attendees/me/recommendations"
BOOTHS = "/api/booths"

# Fake, stable, NAMED ids for pure-core tests — only relative identity
# matters here, never real database ids. Named (not positional) so a
# test can never accidentally index into the weight instead of the id.
AI, GENAI, DA, AUTO = 1, 2, 3, 4
AIWORKS_WEIGHTS: dict[int, Decimal] = {
    AI: Decimal("1.00"),
    GENAI: Decimal("1.00"),
    DA: Decimal("0.80"),
    AUTO: Decimal("0.70"),
}

# --------------------------------------------------------------------------
# Unit — the pure core (§11.2, §11.3): literals only, no I/O
# --------------------------------------------------------------------------


def test_the_canonical_fixture_is_exactly_78() -> None:
    """ARCHITECTURE.md §11.3's worked example, verbatim: AIWorks (AI
    1.0, GenAI 1.0, DA 0.8, Auto 0.7), attendee {GenAI, DA} ->
    raw 1.8, coverage 0.900, focus 0.514..., score01 0.784..., 78."""
    from fractions import Fraction

    result = core.score(frozenset({GENAI, DA}), AIWORKS_WEIGHTS)
    assert result.score == 78
    assert result.raw == Fraction(18, 10)
    assert result.coverage == Fraction(9, 10)
    assert result.focus == Fraction(18, 35)  # 1.8 / 3.5
    assert result.score01 == Fraction(7, 10) * Fraction(9, 10) + Fraction(3, 10) * Fraction(18, 35)
    assert result.matched == {GENAI, DA}


def test_half_up_rounding_at_exact_halves_not_bankers_rounding() -> None:
    """§11.3's named trap: round(78.5) is 78 under banker's rounding but
    MUST be 79 here; round(0.5) is 0 under banker's rounding but MUST
    be 1 here."""
    from fractions import Fraction

    assert core.round_half_up(Fraction(785, 10)) == 79  # 78.5 -> 79 (round() gives 78)
    assert core.round_half_up(Fraction(795, 10)) == 80  # 79.5 -> 80
    assert core.round_half_up(Fraction(500, 1000)) == 1  # 0.5 -> 1 (round() gives 0)
    assert core.round_half_up(Fraction(1500, 1000)) == 2  # 1.5 -> 2 (round() also gives 2)
    assert (
        core.round_half_up(Fraction(-500, 1000)) == 0
    )  # -0.5 -> 0, half-UP not half-away-from-zero

    # A real formula input whose score01 lands on an exact half: one
    # selected interest, matched at weight 0.785, alpha=1 so score01 IS
    # coverage (0.785/1) -> *100 = 78.5 exactly. round() would give 78;
    # this must give 79.
    weights = {1: Decimal("0.785")}
    result = core.score(frozenset({1}), weights, core.MatchConfig(alpha=Decimal("1")))
    assert result.score01 == Fraction(785, 1000)
    assert result.score == 79


_SMALL_WEIGHT = st.integers(1, 100).map(lambda n: Decimal(n) / 100)  # (0.01, 1.00], 2dp
_SMALL_ID = st.integers(1, 20)


@given(
    st.dictionaries(_SMALL_ID, _SMALL_WEIGHT, min_size=1, max_size=8),
    st.sets(_SMALL_ID, min_size=1, max_size=8),
)
def test_score_never_exceeds_100_and_never_goes_below_0(
    booth_weights: dict[int, Decimal], attendee_ids: set[int]
) -> None:
    """§11.3: "score ∈ [0, 100] with no clamping needed" — every w(i) in
    (0, 1], so raw ≤ |M| ≤ |A| (coverage ≤ 1) and raw ≤ Σ w(i) over B
    (focus ≤ 1); this is a proof obligation, not an assumption, so it is
    checked with generated inputs rather than asserted by inspection."""
    result = core.score(frozenset(attendee_ids), booth_weights)
    assert 0 <= result.score <= 100
    assert 0 <= result.coverage <= 1
    assert 0 <= result.focus <= 1


def test_determinism_same_input_same_output_every_time() -> None:
    weights = {1: Decimal("1.00"), 2: Decimal("0.80"), 3: Decimal("0.30")}
    attendee = frozenset({1, 2, 3})
    results = [core.score(attendee, weights) for _ in range(50)]
    assert len({(r.score, r.matched, r.raw, r.coverage, r.focus) for r in results}) == 1


def test_explanation_names_only_matched_interests_ordered_by_weight_then_name() -> None:
    weights = {1: Decimal("0.50"), 2: Decimal("0.90"), 3: Decimal("0.90"), 4: Decimal("0.10")}
    interests = {
        1: core.InterestRef("mid", "Mid"),
        2: core.InterestRef("zeta", "Zeta"),  # ties with 3 on weight; "Alpha" < "Zeta"
        3: core.InterestRef("alpha", "Alpha"),
        4: core.InterestRef("low", "Low"),  # weight 0.10, but only top 3 are ever named
    }
    matched = frozenset({1, 2, 3, 4})
    ordered = core.order_matched(matched, weights, interests)
    assert [i.name for i in ordered] == ["Alpha", "Zeta", "Mid", "Low"]
    text = core.explanation(70, ordered)
    assert text == "Good match based on your interests in Alpha, Zeta and Mid."
    assert "Low" not in text  # at most three named


@pytest.mark.parametrize(
    "score_value,expected_band",
    [
        (100, "Strong match"),
        (80, "Strong match"),
        (79, "Good match"),
        (60, "Good match"),
        (59, "Partial match"),
        (40, "Partial match"),
        (39, "Some overlap"),
        (1, "Some overlap"),
    ],
)
def test_band_thresholds(score_value: int, expected_band: str) -> None:
    assert core.band(score_value) == expected_band


def test_explanation_with_a_single_matched_interest_has_no_stray_and() -> None:
    ref = core.InterestRef("ai", "Artificial Intelligence")
    assert core.explanation(85, [ref]) == (
        "Strong match based on your interests in Artificial Intelligence."
    )


def test_explanation_raises_for_an_empty_match_no_explanation_for_m_empty() -> None:
    with pytest.raises(ValueError, match="no matched interests"):
        core.explanation(0, [])


def test_score_raises_when_no_interests_are_selected() -> None:
    """§11.4: routed by the caller into `match: null`, never a 0 score —
    the pure core refuses to manufacture a number for "no data"."""
    with pytest.raises(ValueError, match="no interests selected"):
        core.score(frozenset(), {1: Decimal("1.00")})


def test_score_raises_when_the_booth_has_no_interest_profile() -> None:
    """§11.4 / §10: a booth with no rows is excluded entirely, never
    scored at 0 — enforced here so a caller cannot skip that routing."""
    with pytest.raises(ValueError, match="no interest profile"):
        core.score(frozenset({1}), {})


def test_m_empty_scores_zero_without_raising_and_has_no_explanation() -> None:
    """§11.4: `M = ∅` -> score 0 (a real, computable case, unlike the
    two above) — no explanation is produced for it, and the CALLER
    (service.py) is responsible for excluding it from recommendations."""
    result = core.score(frozenset({99}), {1: Decimal("1.00")})
    assert result.score == 0
    assert result.matched == frozenset()
    with pytest.raises(ValueError):
        core.explanation(result.score, core.order_matched(result.matched, {1: Decimal("1.00")}, {}))


def test_rejects_a_weight_outside_the_valid_half_open_range() -> None:
    with pytest.raises(ValueError, match="in \\(0, 1\\]"):
        core.score(frozenset({1}), {1: Decimal("0.00")})
    with pytest.raises(ValueError, match="in \\(0, 1\\]"):
        core.score(frozenset({1}), {1: Decimal("1.01")})


def test_no_random_source_and_no_llm_anywhere_in_the_matching_module() -> None:
    """Acceptance criterion #2, verified by inspection of the actual
    shipped source (not just behaviourally): the matching module imports
    nothing from `random`, `secrets`, or any HTTP/LLM client."""
    import inspect

    from app.modules.matching import core as core_module
    from app.modules.matching import service as service_module

    for module in (core_module, service_module):
        source = inspect.getsource(module)
        for forbidden in (
            "import random",
            "secrets.",
            "openai",
            "anthropic",
            "requests.",
            "httpx.",
        ):
            assert forbidden not in source, (module.__name__, forbidden)


# --------------------------------------------------------------------------
# Fixtures for the DB/HTTP layer
# --------------------------------------------------------------------------


@pytest.fixture
def emails() -> Iterator[Callable[[], str]]:
    issued: list[str] = []

    def issue() -> str:
        email = new_email("m07")
        issued.append(email)
        return email

    yield issue
    for email in issued:
        cleanup_email(email)


@pytest.fixture
def attendee(
    otp_log: pytest.LogCaptureFixture,
) -> Callable[..., tuple[str, TestClient]]:
    """Registers and activates an attendee with the given interest
    slugs (default: none), returning (email, a client holding their
    session)."""

    def make(
        email: str, interest_slugs: list[str] | None = None, **overrides: Any
    ) -> tuple[str, TestClient]:
        c = make_client()
        payload = register_payload(email, interest_slugs=interest_slugs or [], **overrides)
        assert c.post(REGISTER, json=payload).status_code == 200
        code = captured_otp(otp_log.records, email)
        assert c.post(VERIFY, json={"email": email, "code": code}).status_code == 200
        return email, c

    return make


def _error(response: Response) -> tuple[int, str]:
    return response.status_code, response.json()["error"]["code"]


def _login_seeded(client: TestClient, email: str, otp_log: pytest.LogCaptureFixture) -> None:
    import re

    from tests.m04_support import OTP_LOGGER

    assert client.post("/api/auth/otp/request", json={"email": email}).status_code == 200
    code = None
    for record in reversed(otp_log.records):
        args = record.args
        if (
            record.name == OTP_LOGGER
            and isinstance(args, tuple)
            and len(args) == 3
            and args[0] == email.lower()
            and args[1] == "LOGIN"
        ):
            code = str(args[2])
            break
    assert code and re.fullmatch(r"\d{6}", code)
    assert client.post(VERIFY, json={"email": email, "code": code}).status_code == 200


@pytest.fixture
def seeded_login(
    otp_log: pytest.LogCaptureFixture,
) -> Iterator[Callable[[TestClient, str], None]]:
    """Logs a CLIENT in as an already-seeded demo attendee (Marcus,
    Elena, Priya, Sofia) and, at teardown, removes only the session and
    OTP-challenge rows that login created — never the seeded user or
    their interests/card, which are permanent fixture data other tests
    (and the demo itself) depend on. Without this, every test run would
    leave a growing pile of session/otp_challenge rows behind."""
    touched: list[str] = []

    def login(client: TestClient, email: str) -> None:
        touched.append(email)
        _login_seeded(client, email, otp_log)

    yield login
    for email in touched:
        execute(
            "DELETE FROM sessions WHERE user_id = (SELECT id FROM users WHERE email = :e)",
            e=email.lower(),
        )
        execute("DELETE FROM otp_challenges WHERE email = :e", e=email.lower())


# --------------------------------------------------------------------------
# GET /api/booths, GET /api/booths/{slug}
# --------------------------------------------------------------------------


def test_booth_list_matches_the_seeded_catalogue(client: TestClient) -> None:
    response = client.get(BOOTHS)
    assert response.status_code == 200
    body = response.json()
    assert [b["slug"] for b in body] == [b.slug for b in fx.BOOTHS]
    aiworks = body[0]
    assert set(aiworks) == {"slug", "name", "location_label"}
    assert aiworks == {"slug": "aiworks", "name": "AIWorks", "location_label": "Hall B · 14"}


def test_booth_detail_includes_description_and_no_internal_fields(client: TestClient) -> None:
    response = client.get(f"{BOOTHS}/aiworks")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"slug", "name", "description", "location_label"}
    for leaked in ("id", "is_active", "allow_repeat_task_points"):
        assert leaked not in body


def test_unknown_booth_slug_is_404(client: TestClient) -> None:
    assert _error(client.get(f"{BOOTHS}/does-not-exist")) == (404, "BOOTH_NOT_FOUND")


def test_inactive_booth_is_hidden_from_list_and_detail(client: TestClient) -> None:
    execute(
        "INSERT INTO booths (slug, name, is_active) VALUES "
        "('m07-inactive-booth', 'M07 Inactive Booth', false)"
    )
    try:
        # Proves the row genuinely exists (catching the class of bug
        # where an uncommitted setup insert makes this test pass
        # vacuously), and that it is excluded ONLY because is_active.
        assert fetch_one("SELECT id FROM booths WHERE slug = 'm07-inactive-booth'") is not None
        assert "m07-inactive-booth" not in [b["slug"] for b in client.get(BOOTHS).json()]
        assert _error(client.get(f"{BOOTHS}/m07-inactive-booth")) == (404, "BOOTH_NOT_FOUND")
    finally:
        execute("DELETE FROM booths WHERE slug = 'm07-inactive-booth'")


# --------------------------------------------------------------------------
# Personalized matching — session-only identity
# --------------------------------------------------------------------------


def test_recommendations_require_an_attendee_session(client: TestClient) -> None:
    assert _error(client.get(RECOMMENDATIONS)) == (401, "UNAUTHENTICATED")


def test_a_provisional_attendee_cannot_reach_recommendations(
    emails: Callable[[], str], otp_log: pytest.LogCaptureFixture
) -> None:
    email = emails()
    with make_client() as c:
        assert c.post(REGISTER, json=register_payload(email)).status_code == 200
        # Never activated: still PROVISIONAL, and has no session at all
        # yet (M04: sessions are created at OTP verification), so this
        # is UNAUTHENTICATED — proving §25's "no capability before
        # ACTIVE" without needing to fabricate a provisional session.
        assert _error(c.get(RECOMMENDATIONS)) == (401, "UNAUTHENTICATED")


def test_a_provisional_attendee_with_a_planted_session_still_gets_403(
    emails: Callable[[], str],
) -> None:
    """The precise §8.1 rule this endpoint depends on: a live, valid
    session for a NOT-YET-ACTIVE attendee is REGISTRATION_NOT_VERIFIED,
    never UNAUTHENTICATED and never a (possibly empty) match list. A
    session cannot normally exist pre-activation (M04 creates it only at
    the moment `ACTIVATION` succeeds), so this plants one directly —
    the same technique M06 uses for a staff session — to prove the
    dependency itself, not just its usual trigger."""
    from app.core.security import generate_session_token, hash_session_token

    email = emails()
    with make_client() as c:
        assert c.post(REGISTER, json=register_payload(email)).status_code == 200
    user_id = fetch_one("SELECT id FROM users WHERE email = :e", e=email.lower())[0]
    raw = generate_session_token()
    execute(
        "INSERT INTO sessions (token_hash, principal_type, user_id, issued_at, expires_at) "
        "VALUES (:h, 'ATTENDEE', :u, now(), now() + interval '12 hours')",
        h=hash_session_token(raw),
        u=user_id,
    )
    try:
        with make_client() as c:
            c.cookies.set("session", raw)
            assert _error(c.get(RECOMMENDATIONS)) == (403, "REGISTRATION_NOT_VERIFIED")
    finally:
        execute("DELETE FROM sessions WHERE token_hash = :h", h=hash_session_token(raw))


def test_recommendations_come_from_the_session_not_a_client_supplied_id(
    emails: Callable[[], str], attendee: Callable[..., Any]
) -> None:
    """No user id anywhere in the request can select someone else's
    matches — there is nowhere in the contract to put one."""
    email_a, client_a = attendee(emails(), interest_slugs=["generative-ai", "data-analytics"])
    # {fintech, sustainability} shares no rows with AIWorks at all — the
    # same zero-overlap pair the Sofia fixture uses (§11.4's M=∅ case).
    email_b, client_b = attendee(emails(), interest_slugs=["fintech", "sustainability"])

    a_recs = {r["booth"]["slug"]: r["match"]["score"] for r in client_a.get(RECOMMENDATIONS).json()}
    b_recs = {r["booth"]["slug"]: r["match"]["score"] for r in client_b.get(RECOMMENDATIONS).json()}
    assert a_recs["aiworks"] == 78
    assert "aiworks" not in b_recs  # M=∅ for {fintech, sustainability} vs AIWorks

    victim_user_id = fetch_one("SELECT id FROM users WHERE email = :e", e=email_b.lower())[0]
    tampered = client_a.get(f"{RECOMMENDATIONS}?user_id={victim_user_id}")
    assert {r["booth"]["slug"]: r["match"]["score"] for r in tampered.json()} == a_recs


def test_recommendation_response_leaks_no_internal_ids(
    emails: Callable[[], str], attendee: Callable[..., Any]
) -> None:
    _, client = attendee(emails(), interest_slugs=["generative-ai", "data-analytics"])
    response = client.get(RECOMMENDATIONS)
    for leaked in ('"id"', '"booth_id"', '"user_id"', '"is_active"'):
        assert leaked not in response.text
    body = response.json()
    assert body  # AIWorks matches, so this is non-empty
    for r in body:
        assert set(r["booth"]) == {"slug", "name", "location_label"}
        if r["match"] is not None:
            assert r["match"]["algorithm_version"] == "weighted-v1"


# --------------------------------------------------------------------------
# Golden fixture vectors (§19.9) — the M03 seeded demo attendees
# --------------------------------------------------------------------------


def test_marcus_scores_exactly_78_against_aiworks(
    seeded_login: Callable[[TestClient, str], None],
) -> None:
    with make_client() as c:
        seeded_login(c, "marcus.lindqvist@attendee.facemark.demo")
        recs = {r["booth"]["slug"]: r for r in c.get(RECOMMENDATIONS).json()}
    assert recs["aiworks"]["match"]["score"] == 78
    assert {i["slug"] for i in recs["aiworks"]["match"]["matched_interests"]} == {
        "generative-ai",
        "data-analytics",
    }
    assert recs["aiworks"]["match"]["explanation"] == (
        "Good match based on your interests in Generative AI and Data Analytics."
    )


def test_elena_is_a_strong_match_for_aiworks(
    seeded_login: Callable[[TestClient, str], None],
) -> None:
    with make_client() as c:
        seeded_login(c, "elena.marchetti@attendee.facemark.demo")
        recs = {r["booth"]["slug"]: r for r in c.get(RECOMMENDATIONS).json()}
    assert recs["aiworks"]["match"]["score"] == 87
    assert recs["aiworks"]["match"]["explanation"].startswith("Strong match")


def test_priya_is_low_on_aiworks_and_strong_on_quantumleap(
    seeded_login: Callable[[TestClient, str], None],
) -> None:
    with make_client() as c:
        seeded_login(c, "priya.chandrasekaran@attendee.facemark.demo")
        recs = c.get(RECOMMENDATIONS).json()
    by_slug = {r["booth"]["slug"]: r["match"]["score"] for r in recs}
    assert by_slug["aiworks"] == 31
    assert by_slug["quantumleap-robotics"] == 89
    # §11.6 ordering: her strongest match leads.
    assert recs[0]["booth"]["slug"] == "quantumleap-robotics"


def test_sofia_has_zero_overlap_with_aiworks_and_it_is_excluded(
    seeded_login: Callable[[TestClient, str], None],
) -> None:
    """§11.4: M=∅ -> excluded from recommendations, but still visible on
    the plain booth list."""
    with make_client() as c:
        seeded_login(c, "sofia.nakamura@attendee.facemark.demo")
        recs = c.get(RECOMMENDATIONS).json()
    assert "aiworks" not in {r["booth"]["slug"] for r in recs}
    for r in recs:
        assert r["match"]["score"] > 0  # every returned recommendation is scored


# --------------------------------------------------------------------------
# Edge cases via HTTP
# --------------------------------------------------------------------------


def test_no_interests_returns_null_match_never_a_fake_zero(
    emails: Callable[[], str], attendee: Callable[..., Any]
) -> None:
    _, client = attendee(emails(), interest_slugs=[])
    body = client.get(RECOMMENDATIONS).json()
    assert body  # every eligible booth is present
    for r in body:
        assert r["match"] is None
    assert [r["booth"]["slug"] for r in body] == [
        b.slug
        for b in fx.BOOTHS  # curated (seed insertion) order
    ]


def test_recommendations_recompute_on_read_after_an_interest_change(
    emails: Callable[[], str], attendee: Callable[..., Any]
) -> None:
    """§11.4: "interest changes recompute on read" — nothing cached."""
    email, client = attendee(emails(), interest_slugs=[])
    assert all(r["match"] is None for r in client.get(RECOMMENDATIONS).json())

    user_id = fetch_one("SELECT id FROM users WHERE email = :e", e=email.lower())[0]
    ai_id = fetch_one("SELECT id FROM interests WHERE slug = 'generative-ai'")[0]
    da_id = fetch_one("SELECT id FROM interests WHERE slug = 'data-analytics'")[0]
    execute(
        "INSERT INTO user_interests (user_id, interest_id) VALUES (:u, :a), (:u, :d)",
        u=user_id,
        a=ai_id,
        d=da_id,
    )
    recs = {r["booth"]["slug"]: r["match"]["score"] for r in client.get(RECOMMENDATIONS).json()}
    assert recs["aiworks"] == 78


def test_ordering_is_byte_stable_across_repeated_requests(
    seeded_login: Callable[[TestClient, str], None],
) -> None:
    with make_client() as c:
        seeded_login(c, "elena.marchetti@attendee.facemark.demo")
        first = c.get(RECOMMENDATIONS).text
        second = c.get(RECOMMENDATIONS).text
        third = c.get(RECOMMENDATIONS).text
    assert first == second == third


def test_tie_break_order_is_name_then_id(db: Any) -> None:
    """§11.6: `score DESC, booth.name ASC, booth.id ASC`. Two fresh
    booths with IDENTICAL weight profiles must tie on score and then
    sort by name — proven directly against the service with a
    controlled, uncommitted fixture (the rollback `db` fixture)."""
    from sqlalchemy import text as sa_text

    interest_id = db.execute(
        sa_text("INSERT INTO interests (slug, name) VALUES ('m07-tie', 'M07 Tie') RETURNING id")
    ).scalar_one()
    booth_z = db.execute(
        sa_text("INSERT INTO booths (slug, name) VALUES ('m07-z-booth', 'Zeta Booth') RETURNING id")
    ).scalar_one()
    booth_a = db.execute(
        sa_text(
            "INSERT INTO booths (slug, name) VALUES ('m07-a-booth', 'Alpha Booth') RETURNING id"
        )
    ).scalar_one()
    for booth_id in (booth_z, booth_a):
        db.execute(
            sa_text(
                "INSERT INTO booth_interests (booth_id, interest_id, weight) VALUES (:b, :i, 1.00)"
            ),
            {"b": booth_id, "i": interest_id},
        )
    user_id = db.execute(
        sa_text(
            "INSERT INTO users (first_name, last_name, email, phone, profession, "
            "sponsor_consent, status, activated_at) VALUES "
            "('Tie', 'Breaker', 'm07-tie@example.com', '+1', 'Eng', false, 'ACTIVE', now()) "
            "RETURNING id"
        )
    ).scalar_one()
    db.execute(
        sa_text("INSERT INTO user_interests (user_id, interest_id) VALUES (:u, :i)"),
        {"u": user_id, "i": interest_id},
    )
    from app.modules.attendees.models import User

    user = db.get(User, user_id)
    assert user is not None
    recs = recommendations_for(db, user)
    tied = [r for r in recs if r.booth.slug in ("m07-z-booth", "m07-a-booth")]
    assert [r.match.score for r in tied if r.match is not None] == [100, 100]
    assert [r.booth.slug for r in tied] == ["m07-a-booth", "m07-z-booth"]  # name ASC


def test_a_booth_with_no_interest_rows_is_excluded_from_recommendations_but_visible_on_the_list(
    client: TestClient, emails: Callable[[], str], attendee: Callable[..., Any]
) -> None:
    execute("INSERT INTO booths (slug, name) VALUES ('m07-no-profile', 'M07 No Profile')")
    try:
        assert fetch_one("SELECT id FROM booths WHERE slug = 'm07-no-profile'") is not None
        assert "m07-no-profile" in [b["slug"] for b in client.get(BOOTHS).json()]
        _, matched_client = attendee(emails(), interest_slugs=["generative-ai"])
        recs = matched_client.get(RECOMMENDATIONS).json()
        assert "m07-no-profile" not in {r["booth"]["slug"] for r in recs}
    finally:
        execute("DELETE FROM booths WHERE slug = 'm07-no-profile'")


def test_database_weight_changes_are_reflected_without_frontend_hardcoding(
    seeded_login: Callable[[TestClient, str], None],
) -> None:
    """No cached score anywhere — a weight change must be visible on the
    very next read, and the seed weight must be restored afterwards."""
    original = fetch_one(
        "SELECT bi.weight FROM booth_interests bi "
        "JOIN booths b ON b.id = bi.booth_id JOIN interests i ON i.id = bi.interest_id "
        "WHERE b.slug = 'aiworks' AND i.slug = 'generative-ai'"
    )[0]
    assert Decimal(str(original)) == Decimal("1.00")
    try:
        execute(
            "UPDATE booth_interests SET weight = 0.20 "
            "WHERE booth_id = (SELECT id FROM booths WHERE slug = 'aiworks') "
            "AND interest_id = (SELECT id FROM interests WHERE slug = 'generative-ai')"
        )
        with make_client() as c:
            seeded_login(c, "marcus.lindqvist@attendee.facemark.demo")
            recs = {r["booth"]["slug"]: r["match"]["score"] for r in c.get(RECOMMENDATIONS).json()}
        assert recs["aiworks"] != 78
    finally:
        execute(
            "UPDATE booth_interests SET weight = 1.00 "
            "WHERE booth_id = (SELECT id FROM booths WHERE slug = 'aiworks') "
            "AND interest_id = (SELECT id FROM interests WHERE slug = 'generative-ai')"
        )
    # Restored: the canonical fixture is exact again.
    with make_client() as c:
        seeded_login(c, "marcus.lindqvist@attendee.facemark.demo")
        recs = {r["booth"]["slug"]: r["match"]["score"] for r in c.get(RECOMMENDATIONS).json()}
    assert recs["aiworks"] == 78


# --------------------------------------------------------------------------
# M04-M06 regression: registration interests feed matching; sessions,
# CSRF, rate limiting and contact cards are untouched by M07.
# --------------------------------------------------------------------------


def test_registration_interest_selection_feeds_matching_end_to_end(
    emails: Callable[[], str], otp_log: pytest.LogCaptureFixture
) -> None:
    email = emails()
    with make_client() as c:
        payload = register_payload(email, interest_slugs=["robotics", "automation"])
        assert c.post(REGISTER, json=payload).status_code == 200
        code = captured_otp(otp_log.records, email)
        assert c.post(VERIFY, json={"email": email, "code": code}).status_code == 200
        recs = {r["booth"]["slug"]: r["match"]["score"] for r in c.get(RECOMMENDATIONS).json()}
    assert recs["quantumleap-robotics"] == 89
    assert recs.get("aiworks") is None or recs["aiworks"] == 31


def test_get_routes_work_with_no_origin_header_unlike_a_mutating_request() -> None:
    """M07 adds only GET routes — the CSRF-protected mutating-route
    inventory (test_auth_session.py) is unaffected, and these routes
    must work with NO Origin header at all, unlike a mutating request."""
    with make_client() as c:
        c.headers.pop("Origin", None)
        assert c.get(BOOTHS).status_code == 200
        assert c.get(RECOMMENDATIONS).status_code == 401  # UNAUTHENTICATED, not CSRF_REJECTED


def test_my_card_and_public_card_still_work_after_m07(
    emails: Callable[[], str], attendee: Callable[..., Any]
) -> None:
    _, client = attendee(emails(), interest_slugs=["generative-ai"])
    my_card = client.get("/api/attendees/me/card")
    assert my_card.status_code == 200
    token = my_card.json()["card_url"].rsplit("/", 1)[-1]
    with make_client() as anon:
        assert anon.get(f"/api/cards/{token}").status_code == 200
