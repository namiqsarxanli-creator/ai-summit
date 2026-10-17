"""Hot reads keep authentication fresh and return DB connections before serialization."""

import asyncio
import time

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.responses import Response
from starlette.types import Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.errors import UnauthenticatedError
from app.core.reads import attendee_read
from app.core.security import hash_session_token
from app.modules.attendees.models import User
from app.modules.realtime import service
from app.modules.realtime.broker import event_bus
from app.modules.realtime.middleware import DatasetGenerationHeaderMiddleware
from tests.api_rig import Rig


@pytest.mark.parametrize(
    "path", ["/api/leaderboard", "/api/attendees/me/score", "/api/attendees/me/profile"]
)
def test_hot_reads_recheck_revocation_and_keep_dependency_db_isolation(rig: Rig, path: str) -> None:
    uid = rig.make_user("read")
    token = rig.sign_in_attendee(uid)
    assert rig.client.get(path).status_code == 200
    rig.sql("UPDATE sessions SET revoked_at=now() WHERE token_hash=:h", h=hash_session_token(token))
    response = rig.client.get(path)
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_read_releases_its_connection_on_success_and_callback_failure(rig: Rig) -> None:
    uid = rig.make_user("read")
    token = rig.sign_in_attendee(uid)
    with Session(rig.engine) as db:

        def read(session: Session, user: User) -> int:
            assert session is db and user.id == uid
            return int(session.execute(text("SELECT 1")).scalar_one())

        assert attendee_read(db, token, read) == 1
        assert not db.in_transaction()

        def fail(session: Session, user: User) -> int:
            assert session is db and user.id == uid
            raise RuntimeError("read failed")

        with pytest.raises(RuntimeError, match="read failed"):
            attendee_read(db, token, fail)
        assert not db.in_transaction()
        with pytest.raises(UnauthenticatedError):
            attendee_read(db, None, read)
        assert not db.in_transaction()


def test_header_cache_hit_needs_no_worker_and_expiry_or_reset_invalidates_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(get_settings(), "leaderboard_cache_seconds", 5.0)
    now = time.monotonic()
    version = event_bus.leaderboard_version
    monkeypatch.setattr(service, "_generation_cache", (version, 987, now))

    async def must_not_dispatch(*args: object, **kwargs: object) -> int:
        raise AssertionError("a valid cache hit dispatched work")

    monkeypatch.setattr("app.modules.realtime.middleware.run_in_threadpool", must_not_dispatch)

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        await Response()(scope, receive, send)

    async def scenario() -> list[tuple[bytes, bytes]]:
        headers: list[tuple[bytes, bytes]] = []

        async def receive() -> Message:
            return {"type": "http.disconnect"}

        async def send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers.extend(message["headers"])

        await DatasetGenerationHeaderMiddleware(inner)({"type": "http"}, receive, send)
        return headers

    assert (b"x-dataset-generation", b"987") in asyncio.run(scenario())
    monkeypatch.setattr(service, "_generation_cache", (version, 987, now - 2.0))
    assert service.peek_cached_dataset_generation() is None
    monkeypatch.setattr(service, "_generation_cache", (version - 1, 987, now))
    assert service.peek_cached_dataset_generation() is None
