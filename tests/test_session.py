import pytest

from factor import config
from factor.auth.session import create_session_token, decode_session_token


@pytest.fixture(autouse=True)
def _session_secret(monkeypatch):
    monkeypatch.setattr(config, "FACTOR_SESSION_SECRET", "test-session-secret-xyz")


def test_session_roundtrip():
    tok = create_session_token("user-abc")
    assert decode_session_token(tok) == "user-abc"


def test_session_tamper_fails():
    tok = create_session_token("user-abc")
    bad = tok[:-4] + "xxxx"
    assert decode_session_token(bad) is None
