"""Google sign-in through `rclone authorize`, with rclone and Google faked."""

import base64
import json

import pytest

from conftest import wait_for
from jungbackup.core import google

TOKEN = {"access_token": "a", "token_type": "Bearer", "refresh_token": "r", "expiry": "2026-10-27T13:00:00Z"}
LINK = "http://127.0.0.1:53682/auth?state=abc-123_x"


def _blob(options):
    return base64.b64encode(json.dumps(options).encode()).decode().rstrip("=")


def _output(token=TOKEN):
    return f"Paste the following into your remote machine --->\n{_blob({'token': json.dumps(token)})}\n<---End paste\n"


@pytest.fixture(autouse=True)
def no_client_secret(monkeypatch):
    """Builds without the secret use rclone's client; tests that need ours set it."""
    monkeypatch.setattr(google, "CLIENT_SECRET", "")


def _options(argv):
    return json.loads(base64.b64decode(argv[3] + "=" * (-len(argv[3]) % 4)))


def test_authorize_asks_for_drive_file_only():
    argv = google.authorize_argv("rclone")
    assert argv[:3] == ["rclone", "authorize", "drive"] and "--auth-no-open-browser" in argv
    assert _options(argv) == {"scope": "drive.file"}


def test_authorize_with_our_client(monkeypatch):
    monkeypatch.setattr(google, "CLIENT_SECRET", "s")
    assert _options(google.authorize_argv("rclone")) == {"scope": "drive.file", "client_id": google.CLIENT_ID,
                                                         "client_secret": "s"}


def test_tokens_keep_the_client_that_issued_them(monkeypatch):
    monkeypatch.setattr(google, "CLIENT_SECRET", "s")
    assert google.client_of({"token": TOKEN}) == {}   # signed in through rclone's client
    assert google.client_of({"token": TOKEN, "client_id": google.CLIENT_ID})["client_secret"] == "s"


def test_parse_token():
    assert google.parse_token(_output()) == TOKEN
    assert google.parse_token("") is None
    assert google.parse_token(_output({"access_token": "a"})) is None   # no refresh token


@pytest.fixture
def fake_rclone(tmp_path, monkeypatch):
    """An `rclone` that prints the sign-in link, then (when told to) the token."""
    def make(stdout, exit_code=0):
        script = tmp_path / "rclone"
        script.write_text("#!/bin/sh\n"
                          f"echo 'NOTICE: go to the following link in your browser: {LINK}' >&2\n"
                          f"printf '%s' '{stdout}'\n"
                          f"exit {exit_code}\n")
        script.chmod(0o755)
        monkeypatch.setenv("JUNGBACKUP_RCLONE", str(script))

    stored = {}
    monkeypatch.setattr(google, "_account", None)
    monkeypatch.setattr(google, "account_email", lambda token: "me@example.com")
    monkeypatch.setattr(google.keyring, "store_account",
                        lambda service, label, secret, callback: (stored.update({service: secret}), callback(True)))
    return make, stored


def _run_sign_in():
    links = []
    result = wait_for(lambda done: google.SignIn(links.append, done), timeout=10)
    return links, [result]


def test_sign_in_round_trip(fake_rclone):
    make, stored = fake_rclone
    make(_output())
    links, results = _run_sign_in()
    assert links == [LINK]
    assert results == [({"email": "me@example.com", "token": TOKEN}, None)]
    assert google.account()["token"] == TOKEN
    assert json.loads(stored["google-drive"])["email"] == "me@example.com"


def test_sign_in_records_our_client(fake_rclone, monkeypatch):
    monkeypatch.setattr(google, "CLIENT_SECRET", "s")
    make, _stored = fake_rclone
    make(_output())
    _links, [(acct, _message)] = _run_sign_in()
    assert acct["client_id"] == google.CLIENT_ID


def test_sign_in_failure(fake_rclone):
    make, _stored = fake_rclone
    make("", exit_code=1)
    _links, results = _run_sign_in()
    [(acct, message)] = results
    assert acct is None and "didn't complete" in message
    assert google.account() is None
