"""M01 acceptance: the health endpoint's exact contract."""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_health_returns_200(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200


def test_health_body_is_exactly_documented_contract(client: TestClient) -> None:
    response = client.get("/api/health")
    # ARCHITECTURE.md / IMPLEMENTATION_PLAN.md M01: the body is EXACTLY
    # {"status": "ok"} — no extra keys, no different casing.
    assert response.json() == {"status": "ok"}


def test_health_route_is_mounted_under_api_prefix(client: TestClient) -> None:
    # The browser-facing contract is /api/health (ARCHITECTURE.md §3.2's
    # rewrite maps /api/:path* to the backend). Asserting the bare path
    # is NOT registered guards against silently dropping the prefix.
    response = client.get("/health")
    assert response.status_code == 404
