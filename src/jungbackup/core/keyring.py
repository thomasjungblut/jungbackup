"""Store the repository password in the user's keyring.

Déjà Dup (as a Flatpak) keeps its password in a keyring that only it can
read, so we ask once and keep our own copy, keyed by repository location.
"""

import gi

gi.require_version("Secret", "1")
from gi.repository import Secret  # noqa: E402

SCHEMA = Secret.Schema.new(
    "io.jungblut.JungBackup.RepoPassword",
    Secret.SchemaFlags.NONE,
    {"repository": Secret.SchemaAttributeType.STRING},
)


def lookup(repo, callback):
    def done(_source, result):
        try:
            callback(Secret.password_lookup_finish(result))
        except Exception:
            callback(None)
    Secret.password_lookup(SCHEMA, {"repository": repo}, None, done)


def store(repo, password, callback=None):
    def done(_source, result):
        try:
            Secret.password_store_finish(result)
            ok = True
        except Exception:
            ok = False
        if callback:
            callback(ok)
    Secret.password_store(SCHEMA, {"repository": repo}, Secret.COLLECTION_DEFAULT,
                          f"Backup password for {repo}", password, None, done)


def forget(repo, callback=None):
    def done(_source, result):
        try:
            Secret.password_clear_finish(result)
        except Exception:
            pass
        if callback:
            callback()
    Secret.password_clear(SCHEMA, {"repository": repo}, None, done)


# Sign-ins to cloud storage (an OAuth refresh token and the account it
# belongs to), one per service, as JSON.
ACCOUNT_SCHEMA = Secret.Schema.new(
    "io.jungblut.JungBackup.Account",
    Secret.SchemaFlags.NONE,
    {"service": Secret.SchemaAttributeType.STRING},
)


def lookup_account_sync(service):
    """Blocking, for building restic's environment; None when there's none or the keyring is unavailable."""
    try:
        return Secret.password_lookup_sync(ACCOUNT_SCHEMA, {"service": service}, None)
    except Exception:
        return None


def store_account(service, label, secret, callback=None):
    def done(_source, result):
        try:
            Secret.password_store_finish(result)
            ok = True
        except Exception:
            ok = False
        if callback:
            callback(ok)
    Secret.password_store(ACCOUNT_SCHEMA, {"service": service}, Secret.COLLECTION_DEFAULT,
                          label, secret, None, done)


def forget_account(service):
    try:
        Secret.password_clear_sync(ACCOUNT_SCHEMA, {"service": service}, None)
    except Exception:
        pass
