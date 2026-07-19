from unittest.mock import MagicMock, patch

import reelay.plex as plex


def _resp(payload, status=200):
    return MagicMock(status_code=status, raise_for_status=lambda: None, json=lambda: payload)


def test_create_pin_success():
    with patch("reelay.plex.requests.post", return_value=_resp({"id": 123, "code": "ABCD"})):
        pin = plex.createPin()
    assert pin == {"id": 123, "code": "ABCD"}


def test_create_pin_failure():
    with patch("reelay.plex.requests.post", side_effect=Exception("boom")):
        assert plex.createPin() is None


def test_auth_url_contains_client_and_code():
    url = plex.authUrl("ABCD")
    assert "code=ABCD" in url and plex.CLIENT_IDENTIFIER in url


def test_poll_pin_pending_then_authorized():
    with patch("reelay.plex.requests.get", return_value=_resp({"authToken": None})):
        assert plex.pollPin(123) is None
    with patch("reelay.plex.requests.get", return_value=_resp({"authToken": "tok"})):
        assert plex.pollPin(123) == "tok"


def test_poll_pin_failure():
    with patch("reelay.plex.requests.get", side_effect=Exception("boom")):
        assert plex.pollPin(123) is None
