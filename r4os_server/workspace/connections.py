"""GitHub connections, private credentials and queued checkout removal."""
import json
import errno
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import uuid
from urllib.parse import urlsplit

from .store import Conflict, identifier, now, REPOSITORY_FIELDS

MAIN = "main"


class CredentialError(Exception):
    """A local storage failure, containing no credential or raw OS error text."""

    def __init__(self, operation):
        super().__init__(f"The server could not {operation} the token. Check credential storage permissions and try again.")


def credential_failure(key, operation, error=None):
    logging.error("GitHub credential storage: %s failed for connection %s (errno=%s)",
                  operation, key, getattr(error, "errno", None))
    raise CredentialError(operation) from None


def prepare_credentials(state, owner, group):
    """Provision/repair credentials before hardened services start (installer only)."""
    folder = Path(state) / "secrets"
    folder.mkdir(mode=0o700, exist_ok=True)
    directory = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fchown(directory, owner, group)
        os.fchmod(directory, 0o2770)
        for name in os.listdir(directory):
            if not name.endswith(".token"):
                continue
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("The credential path must be a regular file.")
                os.fchown(fd, owner, group)
                os.fchmod(fd, 0o660)
            finally:
                os.close(fd)
    finally:
        os.close(directory)


def initialize(db, store):
    db.execute("""CREATE TABLE IF NOT EXISTS connections(
        id TEXT PRIMARY KEY, name TEXT NOT NULL, owner TEXT NOT NULL,
        account_type TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
        status TEXT NOT NULL DEFAULT 'ready', error TEXT NOT NULL DEFAULT '',
        catalog TEXT NOT NULL DEFAULT '{}', created TEXT NOT NULL)""")
    db.execute("INSERT OR IGNORE INTO connections(id,name,owner,account_type,created) VALUES(?,?,?,?,?)",
               (MAIN, "R4OS Organisation", "R4OSDev", "organisation", now()))
    if "directory" not in {row["name"] for row in db.execute("PRAGMA table_info(connections)")}:
        db.execute("ALTER TABLE connections ADD COLUMN directory TEXT NOT NULL DEFAULT ''")
    if "has_token" not in {row["name"] for row in db.execute("PRAGMA table_info(connections)")}:
        db.execute("ALTER TABLE connections ADD COLUMN has_token INTEGER")
    for row in db.execute("SELECT id FROM connections WHERE has_token IS NULL").fetchall():
        try:
            info = secret_path(store, row["id"]).lstat()
            if not stat.S_ISREG(info.st_mode):
                continue
            stored = True
        except FileNotFoundError:
            stored = False
        except OSError:
            continue  # Unknown is distinct from public access; retry after repair.
        db.execute("UPDATE connections SET has_token=? WHERE id=?", (int(stored), row["id"]))
    for row in db.execute("SELECT id,owner FROM connections WHERE directory=''").fetchall():
        folder = "Main" if row["id"] == MAIN else row["owner"].lower() + "-" + row["id"][-8:]
        if db.execute("SELECT 1 FROM connections WHERE directory=?", (folder,)).fetchone():
            folder += "-" + row["id"]
        db.execute("UPDATE connections SET directory=? WHERE id=?", (folder, row["id"]))
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS connections_directory ON connections(directory)")
    columns = {row["name"] for row in db.execute("PRAGMA table_info(repositories)")}
    if "connection_id" not in columns:
        db.execute("""CREATE TABLE repositories_v2(
            name TEXT PRIMARY KEY, github_id INTEGER NOT NULL,
            metadata TEXT NOT NULL, local TEXT NOT NULL DEFAULT '{}',
            present INTEGER NOT NULL DEFAULT 1,
            connection_id TEXT NOT NULL DEFAULT 'main',
            enabled INTEGER NOT NULL DEFAULT 1,
            UNIQUE(connection_id,github_id))""")
        db.execute("INSERT INTO repositories_v2(name,github_id,metadata,local,present) SELECT name,github_id,metadata,local,present FROM repositories")
        db.execute("DROP TABLE repositories")
        db.execute("ALTER TABLE repositories_v2 RENAME TO repositories")
        old = db.execute("SELECT value FROM kv WHERE key='catalog'").fetchone()
        if old:
            db.execute("UPDATE connections SET catalog=? WHERE id=?", (old[0], MAIN))
    if "directory" not in {row["name"] for row in db.execute("PRAGMA table_info(repositories)")}:
        db.execute("ALTER TABLE repositories ADD COLUMN directory TEXT NOT NULL DEFAULT ''")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS repositories_directory ON repositories(directory) WHERE directory<>''")


def address(value):
    if not isinstance(value, str):
        raise ValueError("Enter a GitHub account address.")
    value = value.strip()
    if "://" not in value:
        value = "https://github.com/" + value
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or parsed.netloc.lower() not in ("github.com", "www.github.com")
            or parsed.query or parsed.fragment):
        raise ValueError("Use an HTTPS GitHub account address, for example https://github.com/R4OSDev.")
    owner = parsed.path.strip("/")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", owner):
        raise ValueError("The address must identify a GitHub account or organisation.")
    return owner


def secret_path(store, key):
    return store.state / "secrets" / (identifier(key) + ".token")


def token(store, key):
    stored = get(store, key)["has_token"]
    if stored is False:
        return ""
    if stored is None:
        credential_failure(key, "read")
    path = secret_path(store, key)
    try:
        if path.parent.is_symlink():
            raise OSError(errno.ELOOP, "Invalid credential directory")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError(errno.EINVAL, "Invalid credential file")
            value = stream.read(4097)
        if not re.fullmatch(r"[!-~]{8,4096}", value):
            raise OSError(errno.EINVAL, "Invalid stored credential")
        return value
    except (OSError, UnicodeError) as error:
        credential_failure(key, "read", error)


def write_token(store, key, value):
    path = secret_path(store, key)
    scratch = None
    operation = "save" if value else "remove"
    try:
        # The installer prepares SGID/group ownership; RestrictSUIDSGID stays on.
        if not stat.S_ISDIR(path.parent.lstat().st_mode):
            raise OSError(errno.ENOTDIR, "Invalid credential directory")
        if not value:
            path.unlink(missing_ok=True)
            return
        scratch = path.with_name(path.name + "." + uuid.uuid4().hex)
        fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o660)  # Restore group access masked by UMask=0077.
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        scratch.replace(path)
    except OSError as error:
        credential_failure(key, operation, error)
    finally:
        if scratch is not None:
            try:
                scratch.unlink(missing_ok=True)
            except OSError as error:
                logging.error("GitHub credential storage: temporary file cleanup failed for connection %s (errno=%s)", key, error.errno)


def record(store, row):
    value = dict(row)
    value.update(url="https://github.com/" + value["owner"], is_main=value["id"] == MAIN,
                 has_token=bool(value["has_token"]) if value["has_token"] is not None else None,
                 catalog=json.loads(value["catalog"]))
    value["credential_error"] = "The server could not determine the saved token status. Check credential storage permissions." if value["has_token"] is None else ""
    return value


def get(store, key, *, db=None):
    identifier(key)
    if db is None:
        with store.connect() as connection:
            return get(store, key, db=connection)
    row = db.execute("SELECT * FROM connections WHERE id=?", (key,)).fetchone()
    if not row:
        raise ValueError("Connection not found. Its repositories may have been removed.")
    return record(store, row)


def list_connections(store):
    with store.connect() as db:
        return [record(store, row) for row in db.execute("SELECT * FROM connections ORDER BY CASE id WHEN 'main' THEN 0 ELSE 1 END,created,id")]


def save(store, value, key=None):
    owner = address(value.get("url", ""))
    name = value.get("name", "").strip() if isinstance(value.get("name", ""), str) else ""
    if not name:
        name = "R4OS Organisation" if key == MAIN else owner
    if len(name) > 80:
        raise ValueError("The connection name must contain at most 80 characters.")
    kind = "organisation" if key == MAIN else value.get("account_type", "user")
    if kind not in ("user", "organisation"):
        raise ValueError("Select an account or organisation.")
    supplied = value.get("token")
    if supplied is not None and (not isinstance(supplied, str) or not re.fullmatch(r"[!-~]{8,4096}", supplied)):
        raise ValueError("Enter a valid token without spaces or line breaks, or choose Remove token.")
    if not isinstance(value.get("remove_token", False), bool):
        raise ValueError("Invalid token removal option.")
    if supplied is not None and value.get("remove_token"):
        raise ValueError("Choose either a replacement token or Remove token.")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        prior = get(store, key, db=db) if key else None
        if prior and (prior["status"] != "ready" or value.get("revision") != prior["revision"]):
            raise Conflict("The connection has changed or is being deleted. Reload it before editing.")
        key = key or "conn-" + uuid.uuid4().hex[:12]
        changed_source = prior and (prior["owner"].casefold() != owner.casefold() or prior["account_type"] != kind)
        if changed_source and db.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone():
            raise Conflict("Finish or cancel pending jobs before changing the source account.")
        if changed_source:
            # Keep existing identities and explicit configuration references. They must
            # never silently refer to a different owner's equally named repository.
            db.execute("UPDATE repositories SET present=0,enabled=0 WHERE connection_id=?", (key,))
            db.execute("UPDATE connections SET catalog='{}' WHERE id=?", (key,))
            if key == MAIN:
                db.execute("DELETE FROM kv WHERE key IN ('catalog','release_default_version','test_default_targets')")
        if prior:
            db.execute("UPDATE connections SET name=?,owner=?,account_type=?,revision=revision+1 WHERE id=?", (name, owner, kind, key))
        else:
            folder = owner.lower() + "-" + key[-8:]
            if db.execute("SELECT 1 FROM connections WHERE directory=?", (folder,)).fetchone():
                folder += "-" + key
            db.execute("INSERT INTO connections(id,name,owner,account_type,created,directory,has_token) VALUES(?,?,?,?,?,?,0)", (key, name, owner, kind, now(), folder))
        if supplied is not None or value.get("remove_token"):
            write_token(store, key, supplied or "")
            db.execute("UPDATE connections SET has_token=? WHERE id=?", (int(bool(supplied)), key))
        return get(store, key, db=db)


def select(store, key, value):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if get(store, key, db=db)["status"] != "ready":
            raise Conflict("This connection is being deleted.")
        enabled = value.get("enabled")
        if not isinstance(enabled, bool):
            raise ValueError("Enabled must be true or false.")
        names = value.get("repositories")
        rows = db.execute("SELECT " + REPOSITORY_FIELDS + " FROM repositories r JOIN connections c ON c.id=r.connection_id WHERE r.connection_id=?", (key,)).fetchall()
        repos = {row["name"]: store.repository_record(row) for row in rows}
        if names is None:
            names = [name for name, repo in repos.items() if not enabled or repo["available"]]
        else:
            if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
                raise ValueError("Select a list of repositories.")
        for name in names:
            if name not in repos:
                raise ValueError("A selected repository is not in this connection's catalogue.")
            if enabled and not repos[name]["available"]:
                raise ValueError(repos[name]["unavailable_reason"])
            db.execute("UPDATE repositories SET enabled=? WHERE name=?", (int(enabled), name))
    return {"saved": True}


def referenced(job, names, key):
    """Inspect immutable job inputs without storing any credentials in them."""
    if job["kind"] == "update_all" and isinstance(job["payload"].get("targets"), list):
        return any(target.get("connection_id") == key or target.get("name") in names
                   for target in job["payload"]["targets"] if isinstance(target, dict))
    if job["kind"] in ("catalog", "download_all", "update_all", "check") and not job["subject"]:
        scope = job["payload"].get("connection_id")
        if scope in (None, "", key):
            return True
    if job["subject"] in names or job["payload"].get("connection_id") == key:
        return True
    def contains(value):
        if isinstance(value, str):
            return value in names
        if isinstance(value, dict):
            return any(k in names or contains(v) for k, v in value.items())
        return isinstance(value, list) and any(contains(v) for v in value)
    return contains(job["payload"])


def preview_delete(store, key):
    from . import layout
    connection = get(store, key)
    repos = store.repositories(key, enabled_only=False)
    return {"connection": connection, "repositories": [
        {"name": repo["name"], "display_name": repo["display_name"],
         "local": bool(repo["local"].get("commit") or layout.repository_path(store, repo).exists()),
         "modified": bool(repo["local"].get("dirty"))} for repo in repos]}


def request_delete(store, key, confirmed):
    if key == MAIN:
        raise ValueError("R4OS Organisation is a fixed system connection and cannot be deleted.")
    if confirmed is not True:
        raise ValueError("Confirm deletion of the connection and all its local repositories.")
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        get(store, key, db=db)
        names = {row[0] for row in db.execute("SELECT name FROM repositories WHERE connection_id=?", (key,))}
        for row in db.execute("SELECT * FROM jobs WHERE status='queued' AND kind<>'connection_delete'").fetchall():
            if referenced(store.job_record(row), names, key):
                db.execute("UPDATE jobs SET status='cancelled',cancel=1,finished=? WHERE id=?", (now(), row["id"]))
        db.execute("UPDATE connections SET status='deleting',error='',revision=revision+1 WHERE id=?", (key,))
        return store.enqueue("connection_delete", key, {"connection_id": key}, connection=db)


def remove(context):
    store, key = context.store, context.job["subject"]
    if key == MAIN:
        raise ValueError("The main organisation cannot be deleted.")
    connection = get(store, key)
    repos = store.repositories(key, enabled_only=False)
    names = {repo["name"] for repo in repos}
    try:
        with store.connect() as db:
            running = [store.job_record(row) for row in db.execute("SELECT * FROM jobs WHERE status='running' AND id<>?", (context.job["id"],))]
        if any(referenced(job, names, key) for job in running):
            raise Conflict("A running job still uses this connection. Retry deletion after it finishes.")
        from . import layout
        folder = layout.path(store, connection["directory"])
        marker = folder / ".r4os-connection"
        if marker.is_symlink() or (marker.exists() and marker.read_text().strip() != key):
            raise ValueError("The repository directory does not belong to this connection.")
        if folder.exists() and not marker.exists() and any(folder.iterdir()):
            raise ValueError("The connection directory contains unmanaged files.")
        others = {layout.directory(store, repo) for repo in store.repositories(enabled_only=False) if repo["connection_id"] != key}
        paths = []
        for repo in repos:
            directory = layout.directory(store, repo)
            if directory in others:
                raise ValueError("A checkout directory is shared with another connection; removal has been stopped.")
            target = layout.path(store, directory)
            if target.parent != folder:
                raise ValueError("A repository is outside its connection directory; removal has been stopped.")
            paths.append(target)
        with store.connect() as db:
            for row in db.execute("SELECT id,subject FROM jobs WHERE kind='download'"):
                if row["subject"] in names:
                    paths.append(folder / (".staging-" + identifier(row["id"]) + "-" + identifier(row["subject"])))
            for row in db.execute("SELECT id,payload FROM jobs WHERE kind='download_all'"):
                scope = json.loads(row["payload"]).get("connection_id")
                if scope in (None, "", key):
                    paths.extend(folder / (".staging-" + identifier(row["id"]) + "-" + identifier(name)) for name in names)
        for path in paths:
            context.phase("Removing local checkout: " + path.name)
            if path.is_symlink():
                raise ValueError("Unexpected symbolic link in the repository removal path: " + path.name)
            if path.exists():
                if not path.is_dir():
                    raise ValueError("Expected a managed checkout directory: " + path.name)
                shutil.rmtree(path)
        if folder.exists():
            if any(item.name != ".r4os-connection" for item in folder.iterdir()):
                raise ValueError("The connection folder still contains unmanaged files. Remove or move them before retrying.")
            marker.unlink(missing_ok=True)
            folder.rmdir()
        context.phase("Removing connection and credentials")
        write_token(store, key, "")
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            for name in names:
                db.execute("DELETE FROM imports WHERE name=?", (name,))
            db.execute("DELETE FROM repositories WHERE connection_id=?", (key,))
            db.execute("DELETE FROM connections WHERE id=?", (key,))
        # Release/test references intentionally remain explicit missing sources;
        # neither historical inputs nor produced artefacts are rewritten.
        return {"deleted": key, "repositories_removed": len(repos)}
    except Exception as error:
        from .process import scrub
        with store.connect() as db:
            db.execute("UPDATE connections SET status='delete_failed',error=? WHERE id=?", (scrub(str(error)), key))
        raise
