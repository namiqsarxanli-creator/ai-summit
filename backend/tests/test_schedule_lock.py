"""Real PostgreSQL compatibility of schedule locks; never timed sleeps."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.core.schedule import lock_schedule
from tests.api_rig import Rig


@pytest.mark.parametrize("shared", [False, True])
def test_schedule_excludes_other_writers_and_releases_on_rollback(rig: Rig, shared: bool) -> None:
    with Session(rig.engine) as holder, Session(rig.engine) as contender:
        lock_schedule(holder, shared=shared)
        assert contender.scalar(text("SELECT pg_try_advisory_xact_lock(21461016, 1)")) is False
        contender.rollback()
        compatible = contender.scalar(text("SELECT pg_try_advisory_xact_lock_shared(21461016, 1)"))
        assert compatible is shared
        contender.rollback()
        holder.rollback()
        assert contender.scalar(text("SELECT pg_try_advisory_xact_lock(21461016, 1)")) is True


def test_schedule_shared_holder_excludes_writers_until_commit(rig: Rig) -> None:
    with Session(rig.engine) as first, Session(rig.engine) as second, Session(rig.engine) as writer:
        lock_schedule(first, shared=True)
        lock_schedule(second, shared=True)
        first.commit()
        assert writer.scalar(text("SELECT pg_try_advisory_xact_lock(21461016, 1)")) is False
        writer.rollback()
        second.commit()
        assert writer.scalar(text("SELECT pg_try_advisory_xact_lock(21461016, 1)")) is True
