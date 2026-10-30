"""A folder in Google Drive: an rclone remote the app sets up itself.

restic has no Drive backend of its own, so this is an RcloneStorage whose
remote isn't in any config file: it's described by RCLONE_CONFIG_*
variables in restic's environment, with the token from the keyring (see
core.google), and RCLONE_CONFIG points at /dev/null so rclone keeps
refreshed access tokens in memory only. The token comes from rclone's own
OAuth client, which rclone uses whenever the remote names no other.
"""

import json
import os

from ..core import google
from .rclone import RcloneStorage, rclone_available

REMOTE = "jungbackupgdrive"
PREFIX = f"rclone:{REMOTE}:"


def rclone_env(acct):
    """Environment for restic that defines the Drive remote for this account."""
    var = f"RCLONE_CONFIG_{REMOTE.upper()}"
    env = {
        "RCLONE_CONFIG": os.devnull,
        f"{var}_TYPE": "drive",
        f"{var}_SCOPE": google.SCOPE,
        # Calls go through rclone's OAuth client, whose quota is shared by
        # every rclone user, so Drive answers with rateLimitExceeded well
        # below its documented limits. Pace calls more gently than rclone's
        # default (100ms, bursts of 100) to run into that less.
        f"{var}_PACER_MIN_SLEEP": "200ms",
        f"{var}_PACER_BURST": "10",
    }
    if acct:
        env[f"{var}_TOKEN"] = json.dumps(acct["token"])
        for key, value in google.client_of(acct).items():
            env[f"{var}_{key.upper()}"] = value
    return env


class GoogleDriveStorage(RcloneStorage):
    kind = "gdrive"
    name = "Google Drive"
    ACCOUNT = True
    FIELDS = (
        ("path", "Folder in Google Drive", "Created if it doesn't exist"),
    )
    HINT = ("Jung Backup only sees the files it creates in your Drive. Uploads go through rclone, "
            "which Google limits to about 750 GB a day.")

    @classmethod
    def handles(cls, repo):
        return repo.startswith(PREFIX)

    @classmethod
    def from_fields(cls, values):
        path = values.get("path", "").strip().strip("/")
        return cls(PREFIX + path) if path else None

    def fields(self):
        return {"path": self.repo[len(PREFIX):]}

    @property
    def label(self):
        acct = google.account()
        where = f"{acct['email']} › " if acct else ""
        return f"{where}{self.fields()['path']}"

    def restic_env(self):
        return rclone_env(google.account())

    def reachable(self):
        return google.account() is not None

    def missing(self):
        if google.account() is None:
            return "Sign in to Google to continue."
        if not rclone_available():
            return "Google Drive needs rclone, which isn't installed."
        return None

    def explain(self, error):
        text = f"{error}\n{getattr(error, 'details', '')}"
        if "invalid_grant" in text or "Token has been expired or revoked" in text:
            return "Google ended the sign-in. Sign in again under Settings › Storage Location › Change…."
        if "userRateLimitExceeded" in text or "rateLimitExceeded" in text:
            return "Google Drive is limiting uploads right now. The next backup will continue."
        if "storageQuotaExceeded" in text:
            return "Your Google Drive is full."
        if any(s in text for s in ("no such host", "dial tcp", "i/o timeout", "network is unreachable")):
            return "Couldn't reach Google Drive. Are you online?"
        return super().explain(error)
