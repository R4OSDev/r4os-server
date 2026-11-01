"""Persistent per-connection checkout paths and restartable legacy migration."""
import json
from pathlib import PurePosixPath

from .store import Conflict, identifier


def path(store, relative):
    parts = PurePosixPath(relative).parts
    if not parts or len(parts) > 2 or PurePosixPath(relative).is_absolute():
        raise ValueError("Invalid repository directory.")
    base = (store.root / "Repositories").resolve()
    target = base
    for part in parts:
        target = target / identifier(part)
        if target.is_symlink():
            raise ValueError("Unexpected symbolic link in repository directory: " + relative)
    return target


def allocate(store, db, connection, repo):
    """Reserve a readable path; existing checkouts keep their assigned directory."""
    slug = identifier(repo.get("display_name", repo["name"]))
    if slug == ".r4os-connection" or slug.startswith(".staging-"):
        slug = "repo-" + slug.lstrip(".")
    stem = connection["directory"] + "/" + slug
    relative = stem
    for attempt in range(1000):
        other = db.execute("SELECT 1 FROM repositories WHERE directory=? AND name<>?", (relative, repo["name"])).fetchone()
        occupied = path(store, relative).exists() and not repo.get("local", {}).get("migration_from")
        if not other and not occupied:
            return relative
        # Never adopt a pre-existing untracked checkout, even when an earlier
        # ID-based fallback also happens to be occupied.
        relative = stem + "-" + str(repo["id"]) + ("-" + str(attempt + 1) if attempt else "")
    raise Conflict("No unoccupied directory is available for this repository.")


def directory(store, repo):
    from .connections import get
    return repo.get("directory") or get(store, repo["connection_id"])["directory"] + "/" + identifier(repo["display_name"])


def repository_path(store, repo):
    target = path(store, directory(store, repo))
    previous = repo["local"].get("migration_from")
    if not previous and not repo.get("directory"):
        previous = repo["local"].get("directory", repo["name"])
    if previous and not target.exists():
        old = path(store, previous)
        if old.exists():
            return old
    return target


def ensure_folder(store, connection):
    folder = path(store, identifier(connection["directory"]))
    folder.mkdir(parents=True, exist_ok=True)
    marker = folder / ".r4os-connection"
    if marker.is_symlink():
        raise ValueError("The connection directory marker must not be a symbolic link.")
    if marker.exists():
        if marker.read_text().strip() != connection["id"]:
            raise Conflict("This directory belongs to a different connection.")
    else:
        if any(folder.iterdir()):
            raise Conflict("The connection directory contains unmanaged files: " + folder.name)
        marker.write_text(connection["id"] + "\n")
    return folder


def migrate(store):
    """Run only in the worker, before claiming jobs and with its exclusive lock."""
    from .connections import get
    status = store.setting("repository_layout", {})
    with store.connect() as db:
        pending = db.execute("SELECT 1 FROM repositories WHERE directory='' OR json_extract(local,'$.migration_from') IS NOT NULL LIMIT 1").fetchone()
        if not pending and status.get("status") == "ready":
            return
        if db.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone():
            raise Conflict("Finish running jobs before migrating repository directories.")
    store.set("repository_layout", {"version": 1, "status": "migrating"})
    try:
        for repo in store.repositories(enabled_only=False):
            connection = get(store, repo["connection_id"])
            local = repo["local"]
            if not repo["directory"]:
                previous = local.get("directory", repo["name"])
                local["migration_from"] = previous
                with store.connect() as db:
                    db.execute("BEGIN IMMEDIATE")
                    relative = allocate(store, db, connection, dict(repo, local=local))
                    db.execute("UPDATE repositories SET directory=?,local=? WHERE name=?", (relative, json.dumps(local), repo["name"]))
                repo["directory"] = relative
            previous = local.get("migration_from")
            if not previous:
                continue
            old, target = path(store, previous), path(store, repo["directory"])
            # Record the reservation before renaming. A crash after rename can be
            # resumed from the target, while a genuine collision stops migration.
            folder = target.parent
            if not folder.exists() or not (folder / ".r4os-connection").exists():
                ensure_folder(store, connection)
            if old != target and old.exists():
                if target.exists():
                    raise Conflict("Both the legacy checkout and its target exist: " + repo["directory"])
                old.rename(target)
            with store.connect() as db:
                jobs = db.execute("SELECT id,kind,subject,payload FROM jobs WHERE kind IN ('download','download_all')").fetchall()
            for job in jobs:
                if job["kind"] == "download" and job["subject"] != repo["name"]:
                    continue
                if job["kind"] == "download_all" and json.loads(job["payload"]).get("connection_id") not in (None, "", connection["id"]):
                    continue
                staging = ".staging-" + identifier(job["id"]) + "-" + identifier(repo["name"])
                legacy_stage = (store.root / "Repositories").resolve() / staging
                new_stage = folder / staging
                if legacy_stage.is_symlink():
                    raise ValueError("Unexpected symbolic link in a legacy download directory.")
                if legacy_stage.exists():
                    if new_stage.exists():
                        raise Conflict("Both the legacy download and its target exist: " + staging)
                    legacy_stage.rename(new_stage)
            local.pop("migration_from", None)
            local.update(directory=repo["directory"], path=store.project_root + "/Repositories/" + repo["directory"])
            store.local(repo["name"], local)
        store.set("repository_layout", {"version": 1, "status": "ready"})
    except Exception as error:
        from .process import scrub
        store.set("repository_layout", {"version": 1, "status": "failed", "error": scrub(str(error))})
        raise
