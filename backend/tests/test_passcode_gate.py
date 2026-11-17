"""Pure tests for the optional passcode gate (issue #8).

Same philosophy as test_github_provider / test_demo_mode: no network, no DB.
TestClient drives the real middleware stack and only hits paths that never
touch the database (docs, health, the passcode page itself). The gate is
toggled by flipping settings.passcode, restored in finally.
"""

import pytest
from collections import deque
from datetime import datetime, timedelta
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.dependencies import PASSCODE_COOKIE, _FAILS, passcode_locked, register_passcode_failure
from app.main import app


# Fresh client per test: response cookies persist in the shared jar, which
# would leak the unlocked cookie from the form test into the websocket test.
@pytest.fixture()
def client():
    return TestClient(app)


# The limiter is module state — empty it so lockouts don't leak between tests.
@pytest.fixture(autouse=True)
def clean_limiter():
    _FAILS.clear()
    yield
    _FAILS.clear()


def test_gate_off_by_default_is_invisible(client):
    settings.passcode = ""
    assert client.get("/api/v1/health").status_code == 200
    assert client.get("/docs").status_code == 200


def test_gate_on_requires_cookie_or_header(client):
    settings.passcode = "sesame"
    try:
        assert client.get("/docs").status_code == 401
        assert client.get("/api/v1/health").status_code == 200  # probes stay up
        assert client.get("/docs", headers={"X-Passcode": "wrong"}).status_code == 401
        assert client.get("/docs", headers={"X-Passcode": "sesame"}).status_code == 200
        assert client.get("/docs", cookies={PASSCODE_COOKIE: "sesame"}).status_code == 200
        assert client.options("/docs").status_code != 401  # CORS preflight passes
    finally:
        settings.passcode = ""


def test_unlock_page_flow_sets_cookie(client):
    settings.passcode = "sesame"
    try:
        page = client.get("/api/v1/passcode")  # ungated
        assert page.status_code == 200
        assert 'name="passcode"' in page.text  # form field matches the parser
        bad = client.post("/api/v1/passcode", data={"passcode": "nope"})
        assert bad.status_code == 401
        assert PASSCODE_COOKIE not in bad.headers.get("set-cookie", "")
        ok = client.post(
            "/api/v1/passcode", data={"passcode": "sesame"}, follow_redirects=False
        )
        assert ok.status_code == 303
        cookie = ok.headers.get("set-cookie", "")
        assert PASSCODE_COOKIE in cookie and "HttpOnly" in cookie
        assert client.get("/docs", cookies={PASSCODE_COOKIE: "sesame"}).status_code == 200
    finally:
        settings.passcode = ""


def test_websocket_is_gated_by_the_same_cookie(client):
    settings.passcode = "sesame"
    try:
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect("/api/v1/ws/updates"),
        ):
            pass
        with client.websocket_connect(
            "/api/v1/ws/updates", cookies={PASSCODE_COOKIE: "sesame"}
        ) as ws:
            assert ws.receive_json()["type"] == "connected"
    finally:
        settings.passcode = ""


# --- Rate limit for failed unlocks (issue #25) -------------------------------


def test_lockout_after_five_failures(client):
    settings.passcode = "sesame"
    try:
        for _ in range(5):
            assert client.get("/docs", headers={"X-Passcode": "wrong"}).status_code == 401
        # Locked now — even the CORRECT passcode gets 429.
        assert client.get("/docs", headers={"X-Passcode": "sesame"}).status_code == 429
    finally:
        settings.passcode = ""


def test_success_resets_the_counter(client):
    settings.passcode = "sesame"
    try:
        for _ in range(4):
            client.get("/docs", headers={"X-Passcode": "wrong"})
        assert client.get("/docs", headers={"X-Passcode": "sesame"}).status_code == 200
        # Counter was cleared: 4 more wrongs don't lock (5th would).
        for _ in range(4):
            client.get("/docs", headers={"X-Passcode": "wrong"})
        assert client.get("/docs", headers={"X-Passcode": "sesame"}).status_code == 200
    finally:
        settings.passcode = ""


def test_unlock_form_is_also_limited(client):
    settings.passcode = "sesame"
    try:
        for _ in range(5):
            assert client.post("/api/v1/passcode", data={"passcode": "nope"}).status_code == 401
        assert (
            client.post(
                "/api/v1/passcode", data={"passcode": "sesame"}, follow_redirects=False
            ).status_code
            == 429
        )
    finally:
        settings.passcode = ""


def test_lockout_is_per_ip():
    for _ in range(5):
        register_passcode_failure("1.1.1.1")
    assert passcode_locked("1.1.1.1")
    assert not passcode_locked("2.2.2.2")


def test_window_expires():
    _FAILS["9.9.9.9"] = deque([datetime.now() - timedelta(minutes=16)] * 5)
    assert not passcode_locked("9.9.9.9")  # all entries aged out


def test_websocket_guesses_are_also_limited(client):
    settings.passcode = "sesame"
    try:
        for _ in range(5):
            with (
                pytest.raises(WebSocketDisconnect),
                client.websocket_connect("/api/v1/ws/updates"),
            ):
                pass
        # Locked: even the CORRECT cookie is refused.
        with (
            pytest.raises(WebSocketDisconnect),
            client.websocket_connect(
                "/api/v1/ws/updates", cookies={PASSCODE_COOKIE: "sesame"}
            ),
        ):
            pass
    finally:
        settings.passcode = ""
