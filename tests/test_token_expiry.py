"""Expiry and explicit single-attempt reauthentication without a DocuWare tenant."""

from unittest.mock import Mock

import pytest
import requests

from docuware.client import DocuwareClient
from docuware.conn import Connection, OAuth2Authenticator
from docuware.errors import AccountError, ResourceError


def make_auth(monkeypatch, state=None):
    """Use a deterministic clock and mock only identity-service transport."""
    monkeypatch.setattr("docuware.conn.time.time", lambda: 1000)
    auth = OAuth2Authenticator("user", "secret-password", saved_state=state)
    auth._get = Mock(side_effect=lambda _conn, path: (
        {"IdentityServiceUrl": "https://identity.test"} if "IdentityServiceInfo" in path
        else {"token_endpoint": "https://identity.test/token"}
    ))
    auth._post = Mock(return_value={"access_token": "new-token", "expires_in": 60})
    connection = Connection("https://example.test", authenticator=auth)
    return auth, connection


def test_token_acquisition_expiry_relogin_and_retry(monkeypatch):
    """An application detects expiry, relogs once, then retries successfully."""
    auth, connection = make_auth(monkeypatch)
    state = auth.login(connection)
    assert state == {"access_token": "new-token", "acquired_at": 1000, "expires_in": 60}
    assert not connection.is_token_expired()
    monkeypatch.setattr("docuware.conn.time.time", lambda: 1060)
    assert connection.is_token_expired()
    auth._post.return_value = {"access_token": "renewed-token", "expires_in": 60}
    renewed = connection.relogin()
    assert renewed["acquired_at"] == 1060
    assert not connection.is_token_expired()
    connection.session.get = Mock(return_value=Mock(status_code=200, text="ok"))
    assert connection.get_text("/protected") == "ok"
    assert connection.session.headers["Authorization"] == "Bearer renewed-token"
    assert auth._post.call_count == 2
    assert auth._post.call_args.kwargs["data"]["password"] == "secret-password"


def test_restore_valid_token_does_not_extend_expiry_or_reauthenticate(monkeypatch):
    """Restoring state retains the original acquisition time and remaining TTL."""
    state = {"access_token": "saved-token", "acquired_at": 990, "expires_in": 60}
    auth, connection = make_auth(monkeypatch, state)
    assert auth.login(connection) == state
    auth._post.assert_not_called()
    monkeypatch.setattr("docuware.conn.time.time", lambda: 1050)
    assert auth.is_token_expired()


@pytest.mark.parametrize("state", [
    {}, {"access_token": "legacy"},
    {"access_token": "saved", "acquired_at": 900, "expires_in": 60},
    {"access_token": "saved", "acquired_at": 1001, "expires_in": 60},
    {"access_token": "saved", "acquired_at": 1000, "expires_in": "60"},
    {"access_token": "saved", "acquired_at": 1000, "expires_in": float("inf")},
    {"access_token": "saved", "acquired_at": 1000, "expires_in": -1},
    {"access_token": "saved", "acquired_at": True, "expires_in": 60},
    {"access_token": "saved", "acquired_at": float("nan"), "expires_in": 60},
    {"access_token": "saved", "acquired_at": 10 ** 1000, "expires_in": 60},
    {"access_token": "saved", "acquired_at": 1000, "expires_in": 10 ** 1000},
    {"access_token": 1, "acquired_at": 1000, "expires_in": 60},
])
def test_unknown_invalid_and_expired_state_is_conservatively_expired(monkeypatch, state):
    """Bad metadata never turns an unknown token into an indefinitely valid one."""
    auth, _connection = make_auth(monkeypatch, state)
    assert auth.is_token_expired()


@pytest.mark.parametrize("failure", [
    ResourceError("secret-password leaked by provider", status_code=401),
    ValueError("secret-password in invalid JSON"),
    requests.exceptions.JSONDecodeError("Invalid token JSON", "secret-password", 0),
])
def test_relogin_failure_is_visible_sanitized_and_clears_stale_state(monkeypatch, failure):
    """Policy callers see errors, not stale authorization or sensitive diagnostics."""
    auth, connection = make_auth(monkeypatch)
    auth.login(connection)
    auth._post.side_effect = failure
    with pytest.raises(AccountError) as caught:
        connection.relogin()
    assert "secret-password" not in str(caught.value)
    assert "new-token" not in str(caught.value)
    assert caught.value.status_code == getattr(failure, "status_code", None)
    assert caught.value.__suppress_context__
    assert connection.is_token_expired()
    assert "Authorization" not in connection.session.headers
    assert auth.token is None


@pytest.mark.parametrize("failure_type", [
    requests.Timeout, requests.ConnectTimeout, requests.ConnectionError,
    requests.exceptions.SSLError,
])
def test_transport_failure_retains_classification_without_leaking_details(monkeypatch, failure_type):
    """Timeouts and network failures remain distinguishable from bad credentials."""
    auth, connection = make_auth(monkeypatch)
    auth.login(connection)
    auth._post.side_effect = failure_type("secret-password and new-token")
    with pytest.raises(failure_type) as caught:
        connection.relogin()
    assert type(caught.value) is failure_type
    assert str(caught.value) == "Access-token transport failed"
    assert caught.value.__suppress_context__
    assert caught.value.request is None
    assert connection.is_token_expired()
    assert "Authorization" not in connection.session.headers


def test_missing_access_token_is_an_authentication_error(monkeypatch):
    """A nominally successful token response without a token cannot be swallowed."""
    auth, connection = make_auth(monkeypatch)
    auth._post.return_value = {"expires_in": 60}
    with pytest.raises(AccountError, match="missing an access token"):
        connection.relogin()
    assert connection.is_token_expired()


def test_repeated_unauthorized_response_has_one_retry_only(monkeypatch):
    """The legacy one-retry contract cannot become a reauthentication loop."""
    auth, connection = make_auth(monkeypatch)
    unauthorized = Mock(status_code=401)
    connection.session.get = Mock(return_value=unauthorized)
    with pytest.raises(ResourceError):
        connection.get("/protected")
    assert connection.session.get.call_count == 2
    assert auth._post.call_count == 1


def test_client_exposes_expiry_and_explicit_relogin(monkeypatch):
    """Consumers need not reach into private authenticator internals."""
    _auth, connection = make_auth(monkeypatch)
    client = DocuwareClient("https://example.test")
    client.conn = connection
    assert client.is_token_expired()
    assert client.relogin()["access_token"] == "new-token"
    assert not client.is_token_expired()


def test_relogin_without_credentials_fails_visibly():
    """No authenticator is an error, not a silent no-op."""
    connection = Connection("https://example.test")
    with pytest.raises(AccountError, match="No authenticator"):
        connection.relogin()
