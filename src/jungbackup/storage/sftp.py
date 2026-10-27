"""A folder on an SSH server, through restic's built-in SFTP backend.

restic starts the system's `ssh`, so ~/.ssh/config, keys and the SSH agent
all apply. There's no terminal to type a login password into, so the server
has to accept a key.
"""

from urllib.parse import urlsplit

from .base import Storage

# BatchMode: fail instead of waiting for a password prompt nobody can see.
# accept-new: trust a server on first contact, like Files does, but refuse a changed key.
SSH_ARGS = "-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -o ServerAliveInterval=60"


def _bracket(host):
    return f"[{host}]" if ":" in host else host


def build_repository(host, path, user="", port=""):
    """restic's repository string for an SFTP location.

    Without a port restic takes sftp:user@host:path, where a relative path
    starts in the user's home folder. With a port it needs the URL form,
    whose path is relative unless it starts with a second slash.
    """
    who = f"{user}@" if user else ""
    if port:
        return f"sftp://{who}{_bracket(host)}:{port}/{path}"
    return f"sftp:{who}{_bracket(host)}:{path}"


def parse_repository(repo):
    """Inverse of build_repository: dict with host, user, port and path."""
    rest = repo[len("sftp:"):]
    if rest.startswith("//"):
        url = urlsplit(repo)
        path = url.path[1:] if url.path.startswith("/") else url.path
        return {"host": url.hostname or "", "user": url.username or "",
                "port": str(url.port or ""), "path": path}
    user, _, hostpath = rest.rpartition("@") if "@" in rest.split(":", 1)[0] else ("", "", rest)
    if hostpath.startswith("["):
        host, _, path = hostpath[1:].partition("]:")
    else:
        host, _, path = hostpath.partition(":")
    return {"host": host, "user": user, "port": "", "path": path}


class SftpStorage(Storage):
    kind = "sftp"
    name = "SFTP Server"
    icon = "network-server-symbolic"
    FIELDS = (
        ("host", "Server", "Host name, IP address or a Host from ~/.ssh/config"),
        ("user", "User Name", "Optional; defaults to your ~/.ssh/config or local user name"),
        ("port", "Port", "Optional; defaults to 22"),
        ("path", "Folder on Server", "Relative to your home folder there, or absolute like /srv/backups"),
    )
    HINT = ("restic connects with ssh, using your SSH keys, agent and ~/.ssh/config. "
            "The server has to accept key logins.")

    @classmethod
    def handles(cls, repo):
        return repo.startswith("sftp:")

    @classmethod
    def from_fields(cls, values):
        host, path = values.get("host", "").strip(), values.get("path", "").strip()
        port = values.get("port", "").strip()
        if not host or not path or (port and not port.isdigit()):
            return None
        return cls(build_repository(host, path, values.get("user", "").strip(), port))

    def fields(self):
        return parse_repository(self.repo)

    @property
    def label(self):
        f = self.fields()
        who = f"{f['user']}@" if f["user"] else ""
        port = f":{f['port']}" if f["port"] else ""
        return f"{who}{f['host']}{port} › {f['path']}"

    def restic_options(self):
        return ["-o", f"sftp.args={SSH_ARGS}"]

    def explain(self, error):
        text = f"{error}\n{getattr(error, 'details', '')}"
        host = self.fields()["host"]
        if "Permission denied" in text:
            return (f"{host} didn't accept your SSH key. Copy it to the server with ssh-copy-id, "
                    "or unlock it in your SSH agent. Password logins aren't supported.")
        if "REMOTE HOST IDENTIFICATION HAS CHANGED" in text or "Host key verification failed" in text:
            return (f"The identity of {host} changed since the last connection. If that's expected, "
                    "remove its old entry from ~/.ssh/known_hosts.")
        if "Could not resolve hostname" in text:
            return f"The server “{host}” wasn't found."
        if any(s in text for s in ("Connection refused", "timed out", "No route to host", "Network is unreachable")):
            return f"Couldn't connect to {host}. Is it online and running SSH?"
        if "subsystem request failed" in text:
            return f"{host} doesn't offer SFTP."
        return str(error)
