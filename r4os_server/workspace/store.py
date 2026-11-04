"""Shared, transactional state for the private WebApp and its separate worker."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import urlsplit

ACTIVE = ("queued", "running")
SDK_MUTATIONS = ('sdk_select', 'sdk_import', 'sdk_cleanup', 'tools_install', 'tools_update', 'tools_activate')
FOLDERS = ("Repositories", "BuildTools", "Artifacts", "Release")
REPOSITORY_ACTIONS = ('refresh','refresh_all','catalog','check','download','update','download_all','update_all','build','build_publish','app_publish','connection_delete')
REPOSITORY_FIELDS = ("r.*,c.status AS connection_status,c.revision AS connection_revision,c.has_token AS connection_has_token,c.catalog AS connection_catalog,"
                     "COALESCE((SELECT published FROM apps WHERE repository_name=r.name),0) AS app_published,"
                     "EXISTS(SELECT 1 FROM jobs WHERE kind='app_publish' AND subject=r.name AND status IN ('queued','running') AND cancel=0) AS app_publishing,"
                     "(SELECT CASE WHEN status='failed' THEN error ELSE '' END FROM jobs WHERE kind='app_publish' AND subject=r.name ORDER BY created DESC,rowid DESC LIMIT 1) AS app_publish_error,"
                     "(SELECT json_object('id',id,'status',status,'phase',phase,'error',error,'finished',finished) FROM jobs WHERE kind='refresh' AND subject=r.name ORDER BY created DESC,rowid DESC LIMIT 1) AS refresh_job")


def update_candidate(repo):
    local = repo["local"]
    return (repo["enabled"] and repo["available"] and local.get("status") == "outdated"
            and bool(local.get("commit")) and not local.get("dirty"))


class Conflict(ValueError):
    """A stale edit or an operation blocked by an existing reference."""


def affects_repository(job, repo):
    if job['kind']=='refresh_all':
        return any(item['id']==repo['connection_id'] for item in job['payload'].get('connections',[]))
    if job['subject'] == repo['name']:
        return True
    if job['kind'] == 'connection_delete':
        return job['subject'] == repo['connection_id']
    if job['kind'] == 'update_all' and isinstance(job['payload'].get('targets'), list):
        return any(target.get('name') == repo['name'] for target in job['payload']['targets'])
    if job['kind'] in ('catalog','check','download_all','update_all') and not job['subject']:
        return job['payload'].get('connection_id') in (None, '', repo['connection_id'])
    return False


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.][A-Za-z0-9_.-]{0,127}", value) or value in (".", ".."):
        raise ValueError("Invalid identifier.")
    return value


def under(root, relative):
    root = Path(root).resolve()
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError("The path is outside the designated directory.")
    return path


class Store:
    def __init__(self, root, project_root=None):
        self.root = Path(root).resolve()
        self.project_root = str(project_root or root)
        self.state = self.root / "state"
        self.state.mkdir(parents=True, exist_ok=True)
        self.database = self.state / "workspace.sqlite3"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS repositories(
                    name TEXT PRIMARY KEY, github_id INTEGER UNIQUE NOT NULL,
                    metadata TEXT NOT NULL, local TEXT NOT NULL DEFAULT '{}',
                    present INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY, kind TEXT NOT NULL, subject TEXT NOT NULL,
                    payload TEXT NOT NULL, status TEXT NOT NULL, created TEXT NOT NULL,
                    started TEXT, finished TEXT, phase TEXT NOT NULL DEFAULT '',
                    result TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '',
                    cancel INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created);
                CREATE TABLE IF NOT EXISTS release_types(
                    id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    description TEXT NOT NULL, base TEXT NOT NULL,
                    revision INTEGER NOT NULL, members TEXT NOT NULL,
                    created TEXT NOT NULL, updated TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS imports(name TEXT PRIMARY KEY, commit_id TEXT NOT NULL);
            """)
            db.execute("BEGIN IMMEDIATE")
            from .connections import initialize as initialize_connections
            initialize_connections(db, self)
            from .repo_metadata import initialize as initialize_metadata
            initialize_metadata(db)
            from .apps import initialize as initialize_apps
            initialize_apps(db)
            from .github_build import initialize as initialize_github_builds
            initialize_github_builds(db)
            from .sdk_packages import initialize as initialize_sdks
            initialize_sdks(db)
            from .sdk_projects import initialize as initialize_sdk_projects
            initialize_sdk_projects(db)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(release_types)")}
            migrate_tests = "test_configuration" not in columns
            if migrate_tests:
                db.execute("ALTER TABLE release_types ADD COLUMN test_configuration TEXT")
            if not db.execute("SELECT 1 FROM kv WHERE key='release-types-initialized'").fetchone():
                for key, label in (("slim", "Slim"), ("full", "Full"), ("test", "Test")):
                    db.execute("INSERT OR IGNORE INTO release_types(id,name,description,base,revision,members,created,updated) VALUES(?,?,?,?,?,?,?,?)",
                               (key, label, "", key, 1, "{}", now(), now()))
                db.execute("INSERT INTO kv VALUES('release-types-initialized','true')")
            from .testconfigs import initialize
            initialize(db)
            if migrate_tests:
                db.execute("UPDATE release_types SET test_configuration='standard-tests' WHERE base='test'")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.database, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA busy_timeout=30000")
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def setting(self, key, default=None):
        with self.connect() as db:
            row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def repository_record(row):
        from .profiles import english_cache
        value = json.loads(row["metadata"])
        value.setdefault("id", row["github_id"])
        value.setdefault("display_name", value["name"])
        url = urlsplit(value.get("url", ""))
        value.setdefault("owner", url.path.strip("/").split("/")[0] if url.hostname == "github.com" else "")
        value.update(name=row["name"], connection_id=row["connection_id"],
                     directory=row["directory"],
                     enabled=bool(row["enabled"]), local=english_cache(json.loads(row["local"])), listed=bool(row["present"]))
        value["server_metadata"] = json.loads(row["server_metadata"])
        value["published"] = bool(row["app_published"])
        value["publishing"] = bool(row["app_publishing"])
        value["publish_error"] = row["app_publish_error"] or ""
        value['refresh_job'] = json.loads(row['refresh_job']) if row['refresh_job'] else None
        value["private"] = bool(value.get("private"))
        reason = ""
        if row["connection_status"] != "ready":
            reason = "This connection is being deleted."
        elif not value["listed"] or value.get("disabled"):
            reason = "This repository is no longer available to this connection."
        elif value["private"]:
            if not row["connection_has_token"]:
                reason = "A token with read access is required. Edit the connection."
            elif value.get("access_revision") != row["connection_revision"] or not value.get("read_access"):
                reason = value.get("access_error") or "Refresh the connection to verify private repository access."
            elif json.loads(row["connection_catalog"]).get("error"):
                reason = "Private access could not be verified. Refresh the connection."
        value.update(available=not bool(reason), unavailable_reason=reason)
        return value

    def repositories(self, connection_id=None, *, enabled_only=True):
        clauses, args = [], []
        if connection_id is not None:
            clauses.append("r.connection_id=?")
            args.append(identifier(connection_id))
        if enabled_only:
            clauses.append("r.enabled=1 AND c.status='ready'")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as db:
            rows = db.execute("SELECT " + REPOSITORY_FIELDS + " FROM repositories r JOIN connections c ON c.id=r.connection_id" + where + " ORDER BY r.name COLLATE NOCASE", args).fetchall()
        return [self.repository_record(row) for row in rows]

    def repository(self, name):
        identifier(name)
        with self.connect() as db:
            row = db.execute("SELECT " + REPOSITORY_FIELDS + " FROM repositories r JOIN connections c ON c.id=r.connection_id WHERE r.name=?", (name,)).fetchone()
        if not row:
            raise ValueError("Repository not found. Its connection may have been removed.")
        return self.repository_record(row)

    def require_repository(self, name):
        from .connections import get
        repo = self.repository(name)
        connection = get(self, repo["connection_id"])
        if connection["status"] != "ready":
            raise Conflict("This repository's connection is being deleted.")
        if not repo["enabled"] or not repo["listed"]:
            raise ValueError(repo["display_name"] + ": enable this repository in Connections before using it.")
        if not repo["available"]:
            raise ValueError(repo["unavailable_reason"])
        return repo

    def source_repository(self, name, connection_id="main"):
        """Resolve declared owner names inside an explicit connection, never globally."""
        matches = [repo for repo in self.repositories(connection_id) if repo["display_name"] == name and repo["listed"]]
        if not matches and connection_id != "main":
            return self.source_repository(name, "main")
        if len(matches) != 1:
            raise ValueError(name + ": enable the required source repository in Connections.")
        return self.require_repository(matches[0]["name"])

    def local(self, name, value):
        with self.connect() as db:
            db.execute("UPDATE repositories SET local=? WHERE name=?", (json.dumps(value, ensure_ascii=False), name))

    def enqueue(self, kind, subject="", payload=None, *, connection=None):
        payload = payload or {}
        if kind == "update_all":
            if subject:
                raise ValueError("Update all does not accept an individual repository.")
            # Targets are always selected by the server, never accepted from a client.
            payload = {"connection_id": identifier(payload["connection_id"])} if payload.get("connection_id") else {}
        if connection is None:
            with self.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                return self.enqueue(kind, subject, payload, connection=db)
        db = connection
        if kind in SDK_MUTATIONS:
            placeholders = ','.join('?' for _ in SDK_MUTATIONS)
            pending = db.execute("SELECT * FROM jobs WHERE kind IN ("+placeholders+") AND status IN ('queued','running') ORDER BY created,rowid LIMIT 1", SDK_MUTATIONS).fetchone()
            if pending:
                if pending['kind'] == kind and pending['subject'] == subject and json.loads(pending['payload']) == payload and not pending['cancel']:
                    return self.job_record(pending)
                raise Conflict('Another SDK preparation or activation is already queued or running: '+pending['id'])
        layout = db.execute("SELECT value FROM kv WHERE key='repository_layout'").fetchone()
        if layout and json.loads(layout[0]).get("status") != "ready":
            raise Conflict("Repository directory migration is incomplete. Check the worker before starting new jobs.")
        source = db.execute("SELECT name,connection_id,metadata FROM repositories WHERE name=?", (subject,)).fetchone()
        parent=db.execute("SELECT * FROM jobs WHERE id=? AND kind='refresh_all' AND status='running' AND cancel=0",(payload.get('batch_id',''),)).fetchone() if kind=='refresh' else None
        batch_child=bool(parent and source and any(item['id']==source['connection_id'] and item['revision']==payload.get('connection_revision') for item in json.loads(parent['payload']).get('connections',[])))
        if source:
            metadata = json.loads(source['metadata'])
            if metadata.get("private") and not batch_child:
                self.require_repository(subject)
            payload = dict(payload, repository_label=(metadata.get("owner", "") + "/" if metadata.get("owner") else "") + metadata.get("display_name", subject))
        if kind == 'refresh':
            if source is None:
                raise ValueError('A managed repository is required for Refresh.')
            if not batch_child:self.require_repository(subject)
            for row in db.execute("SELECT * FROM jobs WHERE status IN ('queued','running')"):
                job = self.job_record(row)
                if batch_child and job['id']==payload['batch_id']:continue
                if job['kind'] not in REPOSITORY_ACTIONS or not affects_repository(job, source):
                    continue
                inputs = {k:v for k,v in job['payload'].items() if k not in ('repository_label','batch_id','catalog_at')}
                wanted = {k:v for k,v in payload.items() if k not in ('repository_label','batch_id','catalog_at')}
                if job['kind'] == 'refresh' and inputs == wanted and not job['cancel']:
                    return job
                raise Conflict('Another action already uses this repository: '+job['id'])
        elif kind in REPOSITORY_ACTIONS and kind not in ('connection_delete','refresh_all'):
            for row in db.execute("SELECT j.*,r.connection_id AS refresh_connection FROM jobs j JOIN repositories r ON r.name=j.subject WHERE j.kind='refresh' AND j.status IN ('queued','running')"):
                target = {'name':row['subject'], 'connection_id':row['refresh_connection']}
                if affects_repository({'kind':kind,'subject':subject,'payload':payload}, target):
                    raise Conflict('This repository is being refreshed. Retry after the refresh finishes.')
            for row in db.execute("SELECT * FROM jobs WHERE kind='refresh_all' AND status IN ('queued','running')"):
                job=self.job_record(row)
                overlap=source is not None and affects_repository(job,source)
                if kind in ('catalog','check','download_all','update_all') and not subject:
                    overlap=any(not payload.get('connection_id') or payload['connection_id']==item['id'] for item in job['payload']['connections'])
                if overlap:raise Conflict('Refresh all already uses this repository. Wait for it to finish.')
        if kind != "connection_delete":
            from .connections import referenced
            candidate = {"kind": kind, "subject": subject, "payload": payload}
            for source in db.execute("SELECT id FROM connections WHERE status<>'ready'"):
                if kind in ("catalog", "download_all", "update_all", "check") and not subject and not payload.get("connection_id"):
                    continue  # Aggregate operations select ready connections at execution.
                names = {row[0] for row in db.execute("SELECT name FROM repositories WHERE connection_id=?", (source[0],))}
                if referenced(candidate, names, source[0]):
                    raise Conflict("This connection is being deleted. New jobs cannot use its repositories.")
        query = "SELECT * FROM jobs WHERE kind=? AND subject=? AND status IN ('queued','running')"
        arguments = [kind, subject]
        if kind in ("app_publish", "github_status", "build_publish"):
            query += " AND cancel=0"
        if kind in ("catalog", "download_all", "update_all", "check"):
            query += " AND COALESCE(json_extract(payload,'$.connection_id'),'')=?"
            arguments.append(payload.get("connection_id") or "")
        if kind == "catalog":
            query += " AND COALESCE(json_extract(payload,'$.revision'),0)=?"
            arguments.append(payload.get("revision") or 0)
        if kind.startswith("tools_"):
            query += " AND COALESCE(CASE WHEN json_extract(payload,'$.component') IN ('boot','image') THEN json_extract(payload,'$.component') END,'')=?"
            arguments.append(payload.get("component") if payload.get("component") in ("boot", "image") else "")
        row = db.execute(query, arguments).fetchone()
        if row:
            if kind == "update_all":
                return self.job_record(row)  # Keep the already queued snapshot immutable.
            if {k: v for k, v in json.loads(row["payload"]).items() if k != "repository_label"} != {k: v for k, v in payload.items() if k != "repository_label"}:
                raise ValueError("A job with different inputs already exists for this target: " + row["id"])
            return self.job_record(row)
        if kind == "update_all":
            query = "SELECT " + REPOSITORY_FIELDS + " FROM repositories r JOIN connections c ON c.id=r.connection_id WHERE r.enabled=1 AND r.present=1 AND c.status='ready'"
            args = []
            if payload.get("connection_id"):
                query += " AND r.connection_id=?"
                args.append(payload["connection_id"])
            targets = []
            for row in db.execute(query + " ORDER BY r.name COLLATE NOCASE", args):
                repo = self.repository_record(row)
                if update_candidate(repo):
                    targets.append({"name": repo["name"], "connection_id": repo["connection_id"], "github_id": row["github_id"]})
            if not targets:
                raise ValueError("No known updates are available. Use Refresh to check repository status.")
            payload["targets"] = targets
        key = uuid.uuid4().hex
        db.execute("INSERT INTO jobs(id,kind,subject,payload,status,created) VALUES(?,?,?,?,?,?)",
                   (key, kind, subject, json.dumps(payload, ensure_ascii=False), "queued", now()))
        return self.job_record(db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone())

    @staticmethod
    def job_record(row):
        value = dict(row)
        for name in ("payload", "result"):
            value[name] = json.loads(value[name])
        legacy_phases = {
            "Abgeschlossen": "Completed", "Auftrag gestartet": "Job started",
            "Release-Image zusammensetzen, prüfen und lokal paketieren": "Assembling, validating and packaging the release image locally",
            "SDK-HostTools und passende Runtimeobjekte bauen": "Building SDK HostTools and matching runtime objects",
            "Release-Konfiguration und Quellsnapshots vorbereiten": "Preparing release configuration and source snapshots",
        }
        phase = value["phase"]
        value["phase"] = legacy_phases.get(phase, phase)
        if phase.startswith("Komponente "):
            value["phase"] = "Component " + phase.removeprefix("Komponente ")
        match = re.fullmatch(r"(.+) bauen \((Zig|C|Rust)\)", phase)
        if match:
            label = {"SDK-Werkzeuge": "SDK tools", "Contract-Werkzeuge": "Contract tools", "Image-Werkzeuge": "Image tools"}.get(match[1], match[1])
            value["phase"] = label + " build (" + match[2] + ")"
        return value

    def job(self, key):
        identifier(key)
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
        if not row:
            raise ValueError("Job not found.")
        return self.job_record(row)

    @staticmethod
    def job_filter(subject=None, category="all"):
        clauses, args = [], []
        if subject is not None:
            clauses.append("subject=?")
            args.append(subject)
        categories = {"all": "", "repositories": "kind IN ('refresh','refresh_all','catalog','download','update','download_all','update_all','check','connection_delete')",
                      "tools": "(kind GLOB 'tools_*' OR kind GLOB 'sdk_*')", "builds": "kind IN ('build','build_publish')", "releases": "kind='release'"}
        if category not in categories:
            raise ValueError("Unknown log category.")
        if categories[category]:
            clauses.append(categories[category])
        return (" WHERE " + " AND ".join(clauses) if clauses else ""), args

    def jobs(self, subject=None, limit=30, *, category="all", offset=0):
        where, args = self.job_filter(subject, category)
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs" + where + " ORDER BY created DESC,rowid DESC LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        return [self.job_record(row) for row in rows]

    def active_jobs(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE status IN ('queued','running') ORDER BY created,rowid").fetchall()
        return [self.job_record(row) for row in rows]

    def job_count(self, subject=None, *, category="all"):
        where, args = self.job_filter(subject, category)
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM jobs" + where, args).fetchone()[0]

    def last_success(self, subject, kind):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE subject=? AND kind=? AND status='completed' ORDER BY finished DESC,rowid DESC LIMIT 1", (subject, kind)).fetchone()
        return self.job_record(row) if row else None

    def cancel_job(self, key):
        identifier(key)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone()
            if row is None:
                raise ValueError("Job not found.")
            if row['kind']=='refresh_all':
                db.execute("UPDATE jobs SET cancel=1,status=CASE WHEN status='queued' THEN 'cancelled' ELSE status END,finished=CASE WHEN status='queued' THEN ? ELSE finished END WHERE json_extract(payload,'$.batch_id')=? AND status IN ('queued','running')",(now(),key))
            if row["status"] == "queued":
                db.execute("UPDATE jobs SET status='cancelled',cancel=1,finished=? WHERE id=?", (now(), key))
            elif row["status"] == "running" and not json.loads(row['result']).get('activation_committed'):
                db.execute("UPDATE jobs SET cancel=1 WHERE id=?", (key,))
            return self.job_record(db.execute("SELECT * FROM jobs WHERE id=?", (key,)).fetchone())

    def change_job(self, key, **fields):
        allowed = {"status", "started", "finished", "phase", "result", "error", "cancel"}
        if set(fields) - allowed:
            raise ValueError("Invalid job field.")
        if "result" in fields:
            fields["result"] = json.dumps(fields["result"], ensure_ascii=False)
        with self.connect() as db:
            db.execute("UPDATE jobs SET " + ",".join(name + "=?" for name in fields) + " WHERE id=?",
                       list(fields.values()) + [key])

    def claim(self):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE jobs SET status='cancelled',finished=? WHERE status='queued' AND cancel=1", (now(),))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
            if not row:
                return None
            db.execute("UPDATE jobs SET status='running',started=? WHERE id=?", (now(), row["id"]))
        return self.job(row["id"])

    def recover(self):
        with self.connect() as db:
            db.execute("UPDATE jobs SET status='completed',finished=?,phase='Completed' WHERE status='running' AND json_extract(result,'$.activation_committed')=1", (now(),))
            db.execute("UPDATE jobs SET status='completed',finished=?,phase='Completed' WHERE status='running' AND kind='build_publish' AND json_extract(result,'$.github_release.published')=1", (now(),))
            db.execute("UPDATE jobs SET status='interrupted',finished=?,error=? WHERE status='running'",
                       (now(), "The worker stopped. The job can be started again."))
