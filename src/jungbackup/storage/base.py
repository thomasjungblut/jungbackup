"""What every storage backend provides.

A backend wraps the restic repository string kept in Settings.repository
(a plain path for local folders, "sftp:…" for SFTP, …), so settings, the
keyring entry and the stats cache don't need to know which backend is used.
"""

# Probe states, shared by the setup dialog and the backup job.
EXISTING = "existing"        # a restic repository is there
NEW = "new"                  # nothing there yet; `restic init` will create it
OCCUPIED = "occupied"        # something else is there
UNREACHABLE = "unreachable"  # couldn't look


class Storage:
    kind = ""                # stable id
    name = ""                # shown in the setup dialog
    icon = "folder-remote-symbolic"
    # Entry rows the setup dialog shows: (key, title, hint). Empty for
    # backends with their own picker (local folders).
    FIELDS = ()
    HINT = ""                # shown under the entry rows
    ACCOUNT = False          # True when the setup dialog should offer to sign in

    def __init__(self, repo):
        self.repo = repo

    @classmethod
    def handles(cls, repo):
        return False

    @classmethod
    def from_fields(cls, values):
        """Build from the setup dialog's entries; None while they're incomplete."""
        return None

    @classmethod
    def default_fields(cls):
        """What the setup dialog's entries start with for a new location."""
        return {}

    def fields(self):
        return {}

    @property
    def label(self):
        return self.repo

    def restic_options(self):
        """Extra global flags for every restic call on this repository."""
        return []

    def restic_env(self):
        """Extra environment variables for every restic call on this repository."""
        return {}

    def reachable(self):
        """Cheap check whether starting restic is worth it. True when it can't be told without connecting."""
        return True

    def has_repository(self):
        """True/False when that's known without restic, None otherwise."""
        return None

    def prepare(self):
        """Get ready for `restic init`; return an error message or None."""
        return None

    def explain(self, error):
        """Turn a ResticError into something a person can act on."""
        return str(error)

    def query_space(self, callback):
        """Call callback((size, free)) or callback(None) when unknown."""
        callback(None)

    def probe(self, restic, callback, cancellable=None):
        """Call callback(state, message) with one of the states above.

        Remote backends ask restic: reading the config with an empty
        password succeeds for unencrypted repositories, fails with "wrong
        password" for encrypted ones and with "does not exist" when there's
        none yet.
        """
        def done(_data, error):
            if error is None or error.wrong_password:
                callback(EXISTING, None)
            elif error.repo_missing:
                callback(NEW, None)
            else:
                callback(UNREACHABLE, self.explain(error))
        restic.run_json(self.repo, "", ["cat", "config"], done, cancellable)
