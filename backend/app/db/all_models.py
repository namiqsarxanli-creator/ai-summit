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
from app.modules.contact_cards.models import ContactCard
from app.modules.feedback.models import Feedback
from app.modules.interests.models import Interest
from app.modules.points.models import IdempotencyRecord, PointsLog
from app.modules.staff.models import StaffAccount
