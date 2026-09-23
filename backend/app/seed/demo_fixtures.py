"""The single declarative source for FACEMARK's fictional AI-summit demo
data — IMPLEMENTATION_PLAN.md M03.

Pure data, no database access: every value here is a plain, frozen
dataclass instance. ``app.seed.seeder`` is the only thing that reads
this module and turns it into rows.

Booth/interest weights are ``Decimal`` (not ``float``), matching
ARCHITECTURE.md §11.3: "Floating-point accumulation should also be
avoided ... use Decimal for w(i), matching the numeric(3,2) column
type, so the stored weight and the computed score agree exactly."

``AIWorks`` reproduces ARCHITECTURE.md §10's example booth **exactly**
(same slug, name, location, and all four weights) and one seeded
attendee (Marcus) reproduces §11.3's hand-worked example verbatim
(``Generative AI`` + ``Data Analytics`` → score 78) — so M07's matching
tests can assert that exact, already-verified number instead of
re-deriving it. The rest of the attendee roster is designed per §19.9
("seed data must produce genuinely strong matches") to also cover a
strong (~87-89) and a low/zero case against the same booth — see the
comments beside each ``AttendeeFixture`` below.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.modules.staff.models import StaffRole


@dataclass(frozen=True)
class InterestFixture:
    slug: str
    name: str
    sort_order: int


@dataclass(frozen=True)
class BoothInterestFixture:
    interest_slug: str
    weight: Decimal


@dataclass(frozen=True)
class TaskFixture:
    title: str
    description: str | None
    sort_order: int


@dataclass(frozen=True)
class BoothFixture:
    slug: str
    name: str
    description: str
    location_label: str
    interests: tuple[BoothInterestFixture, ...]
    tasks: tuple[TaskFixture, ...]


@dataclass(frozen=True)
class PresetPointFixture:
    points: int
    label: str
    sort_order: int


@dataclass(frozen=True)
class StaffFixture:
    email: str
    full_name: str
    role: StaffRole
    booth_slug: str | None  # None only for ADMIN


@dataclass(frozen=True)
class AttendeeFixture:
    email: str
    first_name: str
    last_name: str
    phone: str
    profession: str
    interest_slugs: tuple[str, ...]
    # Fixed, opaque, 256-bit (64 hex chars) — ARCHITECTURE.md §19.8:
    # "pre-printed QR cards and slide screenshots must keep working
    # after every reset." Generated once with `secrets.token_hex(32)`
    # and hardcoded here; never regenerated at seed time.
    public_token: str


# ---------------------------------------------------------------------------
# Interests — the controlled vocabulary (ARCHITECTURE.md §7.2)
# ---------------------------------------------------------------------------

INTERESTS: tuple[InterestFixture, ...] = (
    InterestFixture("artificial-intelligence", "Artificial Intelligence", 0),
    InterestFixture("generative-ai", "Generative AI", 1),
    InterestFixture("data-analytics", "Data Analytics", 2),
    InterestFixture("automation", "Automation", 3),
    InterestFixture("robotics", "Robotics", 4),
    InterestFixture("cybersecurity", "Cybersecurity", 5),
    InterestFixture("fintech", "Fintech", 6),
    InterestFixture("healthtech", "HealthTech", 7),
    InterestFixture("cloud-infrastructure", "Cloud Infrastructure", 8),
    InterestFixture("sustainability", "Sustainability", 9),
)

# ---------------------------------------------------------------------------
# Booths — fictional AI-summit startups
# ---------------------------------------------------------------------------

BOOTHS: tuple[BoothFixture, ...] = (
    # Reproduces ARCHITECTURE.md §10's worked-example booth exactly:
    # slug, name, location_label and all four weights are verbatim.
    BoothFixture(
        slug="aiworks",
        name="AIWorks",
        description=(
            "Enterprise generative AI copilots that turn internal documents "
            "and workflows into on-demand answers and automated actions."
        ),
        location_label="Hall B · 14",
        interests=(
            BoothInterestFixture("artificial-intelligence", Decimal("1.00")),
            BoothInterestFixture("generative-ai", Decimal("1.00")),
            BoothInterestFixture("data-analytics", Decimal("0.80")),
            BoothInterestFixture("automation", Decimal("0.70")),
        ),
        tasks=(
            TaskFixture("Watch the live generative AI demo", None, 0),
            TaskFixture("Ask about enterprise deployment", None, 1),
        ),
    ),
    BoothFixture(
        slug="quantumleap-robotics",
        name="QuantumLeap Robotics",
        description=(
            "Autonomous mobile robots and computer-vision tooling for "
            "warehouse and factory automation."
        ),
        location_label="Hall A · 3",
        interests=(
            BoothInterestFixture("robotics", Decimal("1.00")),
            BoothInterestFixture("automation", Decimal("0.90")),
            BoothInterestFixture("artificial-intelligence", Decimal("0.60")),
        ),
        tasks=(
            TaskFixture("See the warehouse robot navigate the course", None, 0),
            TaskFixture("Try the vision-picking demo", None, 1),
        ),
    ),
    BoothFixture(
        slug="securestack",
        name="SecureStack",
        description=(
            "AI-assisted threat detection and cloud security posture "
            "management for fast-growing engineering teams."
        ),
        location_label="Hall C · 8",
        interests=(
            BoothInterestFixture("cybersecurity", Decimal("1.00")),
            BoothInterestFixture("cloud-infrastructure", Decimal("0.80")),
            BoothInterestFixture("data-analytics", Decimal("0.50")),
        ),
        tasks=(
            TaskFixture("Walk through a live threat-detection dashboard", None, 0),
            TaskFixture("Ask about cloud posture scanning", None, 1),
        ),
    ),
    BoothFixture(
        slug="finpulse",
        name="FinPulse",
        description=(
            "Real-time fraud detection and credit-risk scoring models for "
            "digital banks and payment processors."
        ),
        location_label="Hall A · 11",
        interests=(
            BoothInterestFixture("fintech", Decimal("1.00")),
            BoothInterestFixture("data-analytics", Decimal("0.70")),
            BoothInterestFixture("artificial-intelligence", Decimal("0.50")),
        ),
        tasks=(
            TaskFixture("See the real-time fraud-scoring demo", None, 0),
            TaskFixture("Ask about the credit-risk model", None, 1),
        ),
    ),
    BoothFixture(
        slug="medisync-ai",
        name="MediSync AI",
        description=(
            "Clinical documentation and diagnostic-support copilots built "
            "with hospital-grade privacy controls."
        ),
        location_label="Hall B · 6",
        interests=(
            BoothInterestFixture("healthtech", Decimal("1.00")),
            BoothInterestFixture("artificial-intelligence", Decimal("0.80")),
            BoothInterestFixture("data-analytics", Decimal("0.60")),
        ),
        tasks=(
            TaskFixture("Try the clinical documentation assistant", None, 0),
            TaskFixture("Ask about hospital privacy controls", None, 1),
        ),
    ),
    BoothFixture(
        slug="greengrid-systems",
        name="GreenGrid Systems",
        description=(
            "Predictive load-balancing and emissions analytics for "
            "renewable energy grid operators."
        ),
        location_label="Hall C · 2",
        interests=(
            BoothInterestFixture("sustainability", Decimal("1.00")),
            BoothInterestFixture("cloud-infrastructure", Decimal("0.60")),
            BoothInterestFixture("automation", Decimal("0.50")),
        ),
        tasks=(
            TaskFixture("See the grid load-balancing dashboard", None, 0),
            TaskFixture("Ask about emissions analytics", None, 1),
        ),
    ),
)

# ---------------------------------------------------------------------------
# Point presets — global allow-list (ARCHITECTURE.md §7.10, §23.3 item 3:
# "demo uses global"). booth_id is NULL for all rows here.
# ---------------------------------------------------------------------------

PRESET_POINTS: tuple[PresetPointFixture, ...] = (
    PresetPointFixture(5, "Quick interaction", 0),
    PresetPointFixture(10, "Solid engagement", 1),
    PresetPointFixture(15, "Deep engagement", 2),
)

# ---------------------------------------------------------------------------
# Demo staff — one BOOTH_STAFF per booth, plus one ADMIN.
#
# Passwords are NEVER literals here (ARCHITECTURE.md §19.8) — the
# seeder hashes SEED_STAFF_PASSWORD / SEED_ADMIN_PASSWORD (read from
# the environment, no fallback) at seed time. See app/cli.py.
# ---------------------------------------------------------------------------

STAFF: tuple[StaffFixture, ...] = (
    StaffFixture(
        "aiworks@staff.facemark.demo", "Noah Bergström", StaffRole.BOOTH_STAFF, "aiworks"
    ),
    StaffFixture(
        "quantumleap-robotics@staff.facemark.demo",
        "Yuki Tanaka",
        StaffRole.BOOTH_STAFF,
        "quantumleap-robotics",
    ),
    StaffFixture(
        "securestack@staff.facemark.demo", "Omar El-Sayed", StaffRole.BOOTH_STAFF, "securestack"
    ),
    StaffFixture(
        "finpulse@staff.facemark.demo", "Camila Duarte", StaffRole.BOOTH_STAFF, "finpulse"
    ),
    StaffFixture(
        "medisync-ai@staff.facemark.demo", "Grace Okonkwo", StaffRole.BOOTH_STAFF, "medisync-ai"
    ),
    StaffFixture(
        "greengrid-systems@staff.facemark.demo",
        "Lars Andersen",
        StaffRole.BOOTH_STAFF,
        "greengrid-systems",
    ),
    StaffFixture("admin@facemark.demo", "Jordan Blake", StaffRole.ADMIN, None),
)

# ---------------------------------------------------------------------------
# Demo attendees — seeded ACTIVE (§19.8), with a fixed contact-card token
# each. Interest selections are deliberately chosen against AIWorks to
# give M07 a structurally verified strong / mid / low / zero spread
# (§19.9) — see the worked scores in each comment (α=0.7, ARCHITECTURE.md
# §11.3); M07 asserts these numerically, this milestone only asserts the
# structural property (interest sets versus AIWorks' top-weighted rows).
# ---------------------------------------------------------------------------

ATTENDEES: tuple[AttendeeFixture, ...] = (
    # Strong match vs AIWorks: selects exactly AIWorks' two top-weighted
    # interests (both weight 1.00) -> raw=2.0, coverage=1.0, focus=2.0/3.5
    # -> score01=0.7*1.0+0.3*0.5714=0.8714 -> score 87.
    AttendeeFixture(
        email="elena.marchetti@attendee.facemark.demo",
        first_name="Elena",
        last_name="Marchetti",
        phone="+1-555-0101",
        profession="AI Research Lead",
        interest_slugs=("artificial-intelligence", "generative-ai"),
        public_token="a2bcfd25864965ab7c204bdfa6c3d66b05983a26099475d9365246ef358fa55e",
    ),
    # Mid match vs AIWorks: reproduces ARCHITECTURE.md §11.3's hand-worked
    # example verbatim -> raw=1.8, coverage=0.9, focus=1.8/3.5=0.5143 ->
    # score01=0.7*0.9+0.3*0.5143=0.7843 -> score 78 ("Good match").
    AttendeeFixture(
        email="marcus.lindqvist@attendee.facemark.demo",
        first_name="Marcus",
        last_name="Lindqvist",
        phone="+1-555-0102",
        profession="Data Platform Engineer",
        interest_slugs=("generative-ai", "data-analytics"),
        public_token="c7fe5b1f4f2de172e2ebd1873d57565774662f2eddd59b538b22a422a0e4606e",
    ),
    # Low match vs AIWorks (only "automation" overlaps, weight 0.70) ->
    # raw=0.7, coverage=0.35, focus=0.7/3.5=0.2 -> score01=0.7*0.35+
    # 0.3*0.2=0.305 -> score 31 ("Some overlap"). The SAME interest set is
    # a STRONG match vs QuantumLeap Robotics (robotics 1.00 + automation
    # 0.90 -> raw=1.9, coverage=0.95, focus=1.9/2.5=0.76 -> score01=
    # 0.7*0.95+0.3*0.76=0.893 -> score 89) — proving the discrimination is
    # real, not a fixed per-attendee number.
    AttendeeFixture(
        email="priya.chandrasekaran@attendee.facemark.demo",
        first_name="Priya",
        last_name="Chandrasekaran",
        phone="+1-555-0103",
        profession="Robotics Founder",
        interest_slugs=("robotics", "automation"),
        public_token="75b5a2a3b4dba78b141fa32a86d7e7be1deba4744f230e213f6222e43c7a0ca1",
    ),
    # Zero match vs AIWorks: interests share no rows with AIWorks' profile
    # at all (M = ∅ -> score 0, excluded from AIWorks' recommendations per
    # §11.4) while being a strong match for FinPulse and GreenGrid
    # individually — the §11.4 "no overlap" edge case, seeded for real.
    AttendeeFixture(
        email="sofia.nakamura@attendee.facemark.demo",
        first_name="Sofia",
        last_name="Nakamura",
        phone="+1-555-0104",
        profession="Sustainable Fintech Advisor",
        interest_slugs=("fintech", "sustainability"),
        public_token="365d7fac5e3536f1881472397e0d2a6b04888b94c4a66fe6eca50c28f708d109",
    ),
)

# ---------------------------------------------------------------------------
# EventConfig — the singleton row (ARCHITECTURE.md §7.13). id is always 1.
# ---------------------------------------------------------------------------

EVENT_CONFIG_DEFAULTS: dict[str, object] = {
    "id": 1,
    "feedback_points": 5,
    "allow_repeat_task_points_default": False,
    "leaderboard_top_n": 10,
    "dataset_generation": 1,
    "maintenance_mode": False,
}
