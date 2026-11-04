"""Server-owned repository classification, independent of source/Git metadata."""
import json

from .store import Conflict, identifier

CATEGORIES = ("Undefined", "Core", "Driver", "Protocol", "App", "Diagnostic", "Service", "Subsystem", "Game")


def initialize(db):
    if "server_metadata" not in {row["name"] for row in db.execute("PRAGMA table_info(repositories)")}:
        db.execute("ALTER TABLE repositories ADD COLUMN server_metadata TEXT NOT NULL DEFAULT '{\"category\":\"Undefined\"}'")


def update(db, name, value):
    """Apply a category inside the caller's transaction, including app edits."""
    identifier(name)
    if set(value) != {"category"} or value["category"] not in CATEGORIES:
        raise ValueError("Choose a valid repository category.")
    row = db.execute("SELECT r.server_metadata,c.status FROM repositories r JOIN connections c ON c.id=r.connection_id WHERE r.name=?", (name,)).fetchone()
    if not row:
        raise ValueError("Repository not found.")
    if row["status"] != "ready":
        raise Conflict("This connection is being deleted. Its metadata cannot be edited.")
    metadata = json.loads(row["server_metadata"])
    metadata["category"] = value["category"]
    db.execute("UPDATE repositories SET server_metadata=? WHERE name=?", (json.dumps(metadata), name))


def save(store, name, value):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        update(db, name, value)
    return store.repository(name)
