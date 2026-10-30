"""Sign in to Google Drive in the user's own browser, through rclone.

`rclone authorize drive` runs the OAuth flow for installed apps: it
listens on 127.0.0.1 for Google's redirect and trades the code for a
token. We only take the sign-in link it prints, open it in the default
browser (where the user's Google login already lives), and read the
token from its output once the browser comes back. The token and the
account's address go into the keyring; storage.gdrive hands the token to
rclone again for every restic run.

We sign in with Jung Backup's own OAuth client when the build has its
secret (core/_client.py, which isn't in git: write it before building, or
set $JB_GDRIVE_CLIENT_ID_SECRET). Without it, rclone uses its built-in
client, whose quota every rclone user shares, so Drive throttles it often.
A token only works with the client that issued it, so the account records
which one that was. We ask for the drive.file scope: the app sees the
files it created itself, which is all restic needs, and nothing else.
"""

import base64
import json
import os
import re
import shutil
import threading
import urllib.error
import urllib.request

from gi.repository import Gio, GLib

from ..storage.rclone import find_rclone
from . import keyring

try:
    from ._client import CLIENT_SECRET
except ImportError:
    CLIENT_SECRET = os.environ.get("JB_GDRIVE_CLIENT_ID_SECRET", "")

CLIENT_ID = "249497314886-chq9r56uc6nk8v18fq1cb7hm63ag1noj.apps.googleusercontent.com"
SCOPE = "drive.file"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user(emailAddress)"
SERVICE = "google-drive"
TIMEOUT = 30            # seconds, for asking Google whose Drive it is
WAIT_SECONDS = 600      # give up on a sign-in the browser never comes back from

LINK = re.compile(r"http://127\.0\.0\.1:\d+/auth\?state=[\w-]+")
RESULT = re.compile(r"--->\s*(\S+)\s*<---End paste")


# Keeping the account ------------------------------------------------------------

_UNKNOWN = object()
_account = _UNKNOWN


def account():
    """The signed-in account as {"email", "token"}, or None. Read from the keyring once."""
    global _account
    if _account is _UNKNOWN:
        secret = keyring.lookup_account_sync(SERVICE)
        try:
            _account = json.loads(secret) if secret else None
        except ValueError:
            _account = None
    return _account


def _remember(acct, callback):
    global _account
    _account = acct
    keyring.store_account(SERVICE, "Google Drive sign-in" + (f" for {acct['email']}" if acct["email"] else ""),
                          json.dumps(acct), callback)


def sign_out():
    global _account
    _account = None
    keyring.forget_account(SERVICE)


# Talking to rclone ----------------------------------------------------------------

def _encode(options):
    """rclone's config blob: unpadded base64 of a JSON object of strings."""
    return base64.b64encode(json.dumps(options).encode()).decode().rstrip("=")


def _decode(blob):
    return json.loads(base64.b64decode(blob + "=" * (-len(blob) % 4)))


def client():
    """rclone options for our own OAuth client, or {} to use rclone's."""
    return {"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET} if CLIENT_SECRET else {}


def client_of(acct):
    """rclone options for the OAuth client that issued this account's token."""
    ours = client()
    return ours if ours and acct.get("client_id") == CLIENT_ID else {}


def authorize_argv(rclone):
    return [rclone, "authorize", "drive", _encode({"scope": SCOPE, **client()}), "--auth-no-open-browser"]


def parse_token(stdout):
    """The token from `rclone authorize`'s output, or None."""
    match = RESULT.search(stdout or "")
    if not match:
        return None
    try:
        token = json.loads(_decode(match.group(1))["token"])
    except (ValueError, KeyError, TypeError):
        return None
    return token if isinstance(token, dict) and token.get("refresh_token") else None


def account_email(token):
    """Whose Drive the token opens, or "" when Google can't be asked."""
    request = urllib.request.Request(ABOUT_URL, headers={"Authorization": f"Bearer {token['access_token']}"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as r:
            return json.load(r).get("user", {}).get("emailAddress", "")
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return ""


class SignIn:
    """One sign-in in the browser.

    on_link(url) is called with the page to open; then on_done(account, None)
    or on_done(None, message), on the main loop, or not at all after cancel().
    Raises OSError when rclone can't be started.
    """

    def __init__(self, on_link, on_done):
        rclone = find_rclone() or shutil.which("rclone")
        if rclone is None:
            raise OSError("Google Drive needs rclone, which isn't installed.")
        self._on_link = on_link
        self._on_done = on_done
        self._cancelled = False
        self._linked = False
        self._stdout = []
        self._stderr = []
        launcher = Gio.SubprocessLauncher.new(Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_PIPE)
        launcher.setenv("RCLONE_CONFIG", "/dev/null", True)
        try:
            self._proc = launcher.spawnv(authorize_argv(rclone))
        except GLib.Error as e:
            raise OSError(f"Couldn't start rclone: {e.message}") from e
        self._timeout = GLib.timeout_add_seconds(WAIT_SECONDS, self._timed_out)
        self._read(self._proc.get_stdout_pipe(), self._stdout.append)
        self._read(self._proc.get_stderr_pipe(), self._got_log_line)
        self._proc.wait_async(None, self._exited)

    def cancel(self):
        self._cancelled = True
        self._stop_timer()
        self._proc.force_exit()

    def _stop_timer(self):
        if self._timeout:
            GLib.source_remove(self._timeout)
            self._timeout = 0

    def _timed_out(self):
        self._timeout = 0
        self._proc.force_exit()
        self._finish(None, "The sign-in timed out. Try again.")
        return GLib.SOURCE_REMOVE

    @staticmethod
    def _read(pipe, handler):
        stream = Gio.DataInputStream.new(pipe)

        def got_line(stream, result):
            try:
                line, _len = stream.read_line_finish_utf8(result)
            except GLib.Error:
                line = None
            if line is not None:
                handler(line)
                stream.read_line_async(GLib.PRIORITY_DEFAULT, None, got_line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, got_line)

    def _got_log_line(self, line):
        self._stderr.append(line)
        match = LINK.search(line)
        if match and not self._linked:
            self._linked = True
            self._on_link(match.group(0))

    def _exited(self, proc, result):
        try:
            proc.wait_finish(result)
        except GLib.Error:
            pass
        # The pipes may still hold the last lines; give them a moment to arrive.
        GLib.timeout_add(100, self._collect)

    def _collect(self):
        if self._cancelled:
            return GLib.SOURCE_REMOVE
        token = parse_token("\n".join(self._stdout))
        if token is None:
            errors = [line for line in self._stderr if "ERROR" in line or "Failed" in line]
            self._finish(None, "The sign-in didn't complete." + (f" rclone said: {errors[-1]}" if errors else ""))
            return GLib.SOURCE_REMOVE

        def look_up():
            acct = {"email": account_email(token), "token": token}
            if client():
                acct["client_id"] = CLIENT_ID
            GLib.idle_add(self._finish, acct, None)
        threading.Thread(target=look_up, daemon=True).start()
        return GLib.SOURCE_REMOVE

    def _finish(self, acct, message):
        if self._cancelled:
            return GLib.SOURCE_REMOVE
        self._cancelled = True   # report once
        self._stop_timer()
        if acct is None:
            self._on_done(None, message)
        else:
            _remember(acct, lambda ok: self._on_done(acct, None) if ok else
                      self._on_done(None, "Couldn't save the sign-in to your keyring."))
        return GLib.SOURCE_REMOVE
