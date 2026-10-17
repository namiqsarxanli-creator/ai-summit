"""Import every ORM model module for its side effect: registering the
table with ``Base.metadata``.

SQLAlchemy's declarative ``Base.metadata`` only knows about a model
once its module has been imported somewhere. Alembic's autogenerate
(and the drift check / schema-metadata tests) need the FULL set of
models registered, not whichever ones happened to be imported by
another code path first — so this is the one place every domain
module's ``models`` submodule is imported, and everything else
(``alembic/env.py``, tests) imports THIS module rather than repeating
the list.
"""

from __future__ import annotations

# ruff: noqa: F401 — imported for metadata registration side effects only
from app.core.auth_session import AuthSession
from app.modules.admin.models import AdminAudit, EventConfig
from app.modules.attendees.models import ConsentAudit, OtpChallenge, User, UserInterest
from app.modules.booths.models import Booth, BoothInterest, Task, TaskPresetPoint
from app.modules.competition.final_models import FinalResult
from app.modules.competition.models import (
    MasterclassAttendance,
    Panel,
    PanelScan,
    StaffScope,
)
from app.modules.competition.offline_models import OfflineScan
from app.modules.contact_cards.models import ContactCard
from app.modules.feedback.models import Feedback
from app.modules.interests.models import Interest
from app.modules.masterclasses.models import (
    Masterclass,
    MasterclassBlock,
    MasterclassEvent,
    MasterclassReservation,
    MasterclassRoom,
)
from app.modules.mentors.models import MentorBooking, MentorSession
from app.modules.people.models import (
    Connection,
    ConnectionScan,
    MatchTable,
    Meeting,
    MeetingParticipant,
    MeetingRequest,
    UserBlock,
)
from app.modules.people.photo_models import PairPhoto
from app.modules.playground.models import PlaygroundBooking, PlaygroundSlot
from app.modules.points.models import IdempotencyRecord, PointsLog
from app.modules.reminders.models import ReminderDelivery
from app.modules.staff.models import StaffAccount
