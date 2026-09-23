"""M05 unit tests: trusted-proxy IP resolution (§21.7.1), the
fixed-window rate limiter (§21.7), and CSRF origin normalisation
(§8.3.1). Pure functions and an injected clock — no database, no
timing, fully deterministic."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.datastructures import Headers

from app.core.config import Settings
from app.core.csrf import normalise_origin, request_origin
from app.core.errors import RateLimitedError
from app.core.proxy import parse_trusted_cidrs, resolve_client_ip
from app.core.ratelimit import (
    OTP_REQUEST_PER_EMAIL,
    OTP_REQUEST_PER_IP,
    InMemoryFixedWindowStore,
    RateLimit,
    RateLimiter,
)

PROXIES = parse_trusted_cidrs("10.0.0.0/8, fd00::/8")

# --------------------------------------------------------------------------
# §21.7.1 trusted-proxy resolution
# --------------------------------------------------------------------------


def test_default_configuration_trusts_no_proxy() -> None:
    assert parse_trusted_cidrs("") == ()
    assert parse_trusted_cidrs("  ,  ") == ()


def test_untrusted_peer_ignores_forwarding_headers_entirely() -> None:
    """§21.7.1 rule 1 — and with the default empty trust list, every
    peer is untrusted."""
    assert resolve_client_ip("198.51.100.7", "1.2.3.4", PROXIES) == "198.51.100.7"
    assert resolve_client_ip("10.0.0.5", "1.2.3.4", ()) == "10.0.0.5"


def test_trusted_peer_walks_forwarded_for_from_the_right() -> None:
    """§21.7.1 rule 2: the rightmost untrusted entry is the client."""
    assert resolve_client_ip("10.0.0.5", "203.0.113.9", PROXIES) == "203.0.113.9"
    assert resolve_client_ip("10.0.0.5", "203.0.113.9, 10.0.0.7", PROXIES) == "203.0.113.9"


def test_client_written_forwarded_for_cannot_choose_its_own_identity() -> None:
    """The attacker controls everything LEFT of what our proxy appends.
    Prepending fake entries changes nothing — the leftmost entry (the
    "common mistake") is never taken."""
    honest = resolve_client_ip("10.0.0.5", "198.51.100.7", PROXIES)
    spoofed = resolve_client_ip("10.0.0.5", "6.6.6.6, 7.7.7.7, 198.51.100.7", PROXIES)
    assert honest == spoofed == "198.51.100.7"


def test_all_hops_trusted_falls_back_to_the_peer() -> None:
    assert resolve_client_ip("10.0.0.5", "10.0.0.9, 10.0.0.8", PROXIES) == "10.0.0.5"
    assert resolve_client_ip("10.0.0.5", None, PROXIES) == "10.0.0.5"


def test_garbage_from_a_trusted_hop_fails_safe_onto_the_peer() -> None:
    """Walking past an unparseable entry would reach client-written
    data, so resolution stops at the proxy instead."""
    assert resolve_client_ip("10.0.0.5", "1.2.3.4, not-an-ip", PROXIES) == "10.0.0.5"


def test_ipv6_and_non_ip_peers() -> None:
    assert resolve_client_ip("fd00::1", "2001:db8::42", PROXIES) == "2001:db8::42"
    assert resolve_client_ip("testclient", "1.2.3.4", PROXIES) == "testclient"
    assert resolve_client_ip(None, "1.2.3.4", PROXIES) == "unknown"


def test_malformed_trusted_proxy_cidrs_fail_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRUSTED_PROXY_CIDRS", "10.0.0.0/8, not-a-cidr")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


# --------------------------------------------------------------------------
# §21.7 fixed-window limiter
# --------------------------------------------------------------------------


class FakeClock:
    def __init__(self, now: float = 9_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _limiter(clock: FakeClock) -> RateLimiter:
    return RateLimiter(InMemoryFixedWindowStore(), clock=clock)


def test_per_identity_limit_is_independent_per_identity() -> None:
    """3 / 15 min per normalised email (§21.7): one address hitting its
    limit leaves every other address untouched."""
    limiter = _limiter(FakeClock())
    for _ in range(3):
        limiter.check([(OTP_REQUEST_PER_EMAIL, "a@x.demo")])
    with pytest.raises(RateLimitedError):
        limiter.check([(OTP_REQUEST_PER_EMAIL, "a@x.demo")])
    limiter.check([(OTP_REQUEST_PER_EMAIL, "b@x.demo")])  # unaffected


def test_per_ip_ceiling_is_independent_of_per_identity_limits() -> None:
    """600 / 15 min per IP: a venue NAT address can carry 600 distinct
    attendees' requests before the blunt ceiling engages."""
    limiter = _limiter(FakeClock())
    for i in range(600):
        limiter.check([(OTP_REQUEST_PER_EMAIL, f"u{i}@x.demo"), (OTP_REQUEST_PER_IP, "1.1.1.1")])
    with pytest.raises(RateLimitedError):
        limiter.check([(OTP_REQUEST_PER_EMAIL, "fresh@x.demo"), (OTP_REQUEST_PER_IP, "1.1.1.1")])
    # A different source address is a different bucket.
    limiter.check([(OTP_REQUEST_PER_EMAIL, "fresh2@x.demo"), (OTP_REQUEST_PER_IP, "2.2.2.2")])


def test_window_rolls_over_and_reports_retry_after() -> None:
    clock = FakeClock(now=900 * 10 + 100)  # 100 s into a 900 s window
    limiter = _limiter(clock)
    limit = RateLimit("t", 1, 900)
    limiter.check([(limit, "k")])
    with pytest.raises(RateLimitedError) as exc:
        limiter.check([(limit, "k")])
    assert exc.value.retry_after == 800
    assert exc.value.code == "RATE_LIMITED" and exc.value.status_code == 429

    clock.now = 900 * 11  # next window
    limiter.check([(limit, "k")])


def test_every_bucket_is_counted_even_when_one_rejects() -> None:
    """A request cannot dodge one bucket by tripping another first."""
    limiter = _limiter(FakeClock())
    tight = RateLimit("tight", 1, 60)
    loose = RateLimit("loose", 5, 60)
    limiter.check([(tight, "k"), (loose, "k")])
    for _ in range(4):
        with pytest.raises(RateLimitedError):
            limiter.check([(tight, "k"), (loose, "k")])
    with pytest.raises(RateLimitedError):  # loose is now spent too
        limiter.check([(loose, "k")])


def test_store_prunes_windows_that_have_ended() -> None:
    store = InMemoryFixedWindowStore()
    store.hit("k", 60, now=1_000.0)
    store.hit("k", 60, now=10_000.0)  # >60 s later: the first window is pruned
    assert len(store._counts) == 1


# --------------------------------------------------------------------------
# §8.3.1 origin normalisation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:3000", "http://localhost:3000"),
        ("HTTP://LocalHost:3000/", "http://localhost:3000"),
        ("https://summit.example.org:443", "https://summit.example.org"),
        ("http://summit.example.org:80", "http://summit.example.org"),
        ("http://localhost:3000/register?x=1", "http://localhost:3000"),
        ("null", None),
        ("ftp://x.example", None),
        ("", None),
    ],
)
def test_origin_normalisation(raw: str, expected: str | None) -> None:
    assert normalise_origin(raw) == expected


def test_origin_header_wins_over_referer_even_when_null() -> None:
    assert request_origin(Headers({"referer": "http://localhost:3000/x"})) == (
        "http://localhost:3000"
    )
    assert request_origin(Headers({"origin": "null", "referer": "http://localhost:3000/x"})) is None
    assert request_origin(Headers({})) is None


@pytest.mark.parametrize(
    "bad", ["localhost:3000", "http://localhost:3000/app", "ftp://x.example", "https://"]
)
def test_app_origin_must_be_a_bare_http_origin(monkeypatch: pytest.MonkeyPatch, bad: str) -> None:
    monkeypatch.setenv("APP_ORIGIN", bad)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_app_origin_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_ORIGIN", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_secure_cookie_follows_the_public_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_ORIGIN", "https://summit.example.org")
    assert Settings(_env_file=None).serves_https is True
    monkeypatch.setenv("APP_ORIGIN", "http://localhost:3000")
    assert Settings(_env_file=None).serves_https is False
