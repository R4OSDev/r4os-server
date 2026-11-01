"""Editable test sets, pinned Git inputs and immutable release-build snapshots."""
import json
from pathlib import PurePosixPath
import re
import subprocess
import uuid

from .store import Conflict, identifier, now, under

PRESETS = (("standard-tests", "Standard tests", "standard", None),
           ("browser-tests", "Browser tests", "browser", "standard-tests"))


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS test_configurations(
        id TEXT PRIMARY KEY, name TEXT NOT NULL COLLATE NOCASE UNIQUE,
        description TEXT NOT NULL, revision INTEGER NOT NULL,
        definition TEXT NOT NULL, origin TEXT NOT NULL,
        created TEXT NOT NULL, updated TEXT NOT NULL)""")
    if db.execute("SELECT 1 FROM kv WHERE key='test-configurations-initialized'").fetchone():
        for key, old_name, new_name in (("standard-tests", "Standardtests", "Standard tests"), ("browser-tests", "Browser-Tests", "Browser tests")):
            row = db.execute("SELECT * FROM test_configurations WHERE id=?", (key,)).fetchone()
            if (row and row["name"] == old_name and row["description"] == "Übernommene " + old_name
                    and not json.loads(row["origin"]).get("edited")):
                label = new_name
                if db.execute("SELECT 1 FROM test_configurations WHERE name=? COLLATE NOCASE AND id<>?", (label, key)).fetchone():
                    label += " (" + key + ")"
                if not db.execute("SELECT 1 FROM test_configurations WHERE name=? COLLATE NOCASE AND id<>?", (label, key)).fetchone():
                    db.execute("UPDATE test_configurations SET name=?,description=?,revision=revision+1,updated=? WHERE id=?", (label, "Imported " + new_name, now(), key))
        return
    for key, name, preset, parent in PRESETS:
        definition = {"extends": parent, "targets": [], "files": [], "settings": None, "startup": None}
        db.execute("INSERT INTO test_configurations VALUES(?,?,?,?,?,?,?,?)",
                   (key, name, "Imported " + name, 1, json.dumps(definition),
                    json.dumps({"preset": preset, "pending": True}), now(), now()))
    db.execute("INSERT INTO kv VALUES('test-configurations-initialized','true')")


def record(row):
    value = dict(row)
    for field in ("definition", "origin"):
        value[field] = json.loads(value[field])
    return value


def get(store, key, *, connection=None):
    identifier(key)
    if connection is None:
        with store.connect() as db:
            return get(store, key, connection=db)
    row = connection.execute("SELECT * FROM test_configurations WHERE id=?", (key,)).fetchone()
    if row is None:
        raise ValueError("Test configuration not found.")
    return record(row)


def _git(path, *arguments):
    from .process import clean_env
    result = subprocess.run(["git", "-c", "safe.directory=" + str(path), "-C", str(path), *arguments],
                            capture_output=True, timeout=30, env=clean_env())
    if result.returncode:
        raise ValueError("The saved test file source revision is not available locally.")
    return result.stdout


def source_path(value, *, image=False):
    if not isinstance(value, str) or not value or any(c in value for c in ("\\", ":", "|", "\n", "\r", "\0")):
        raise ValueError("Enter a valid file or directory path.")
    if image != value.startswith("/") or any(part in (".", "..", ".git") for part in value.split("/")):
        raise ValueError("The test file path is outside the designated directory.")
    return str(PurePosixPath(value))


def read_mapping(store, entry):
    """Read regular Git objects only; the checkout and symlinks are never followed."""
    from .catalog import repository_path
    repo = store.require_repository(entry["repository"])
    root = repository_path(store, repo)
    commit = entry["commit"]
    if not isinstance(commit, str) or not re.fullmatch(r"[a-f0-9]{40,64}", commit):
        raise ValueError("A complete Git source revision for the test files is missing.")
    source, target = source_path(entry["source"]), source_path(entry["target"], image=True)
    kind = _git(root, "cat-file", "-t", commit + ":" + source).strip()
    if kind not in (b"blob", b"tree"):
        raise ValueError("The test file source must be a regular file or directory.")
    if kind == b"blob" and target == "/":
        raise ValueError("Provide a complete destination path for a single test file.")
    entries = _git(root, "ls-tree", "-r", "-z", commit, "--", source).split(b"\0")
    if len(entries) > 2001:
        raise ValueError("A test file source contains too many files.")
    files, size = [], 0
    for item in filter(None, entries):
        metadata, name = item.split(b"\t", 1)
        mode, object_kind, object_id = metadata.split()
        if object_kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError("Test file sources must not contain symbolic links or submodules.")
        path = name.decode("utf-8")
        relative = path[len(source):].lstrip("/") if kind == b"tree" else ""
        destination = source_path(target.rstrip("/") + "/" + relative, image=True) if relative else target
        blob_size = int(_git(root, "cat-file", "-s", object_id.decode()))
        size += blob_size
        if size > 16 * 1024 * 1024:
            raise ValueError("Test file sources are limited to 16 MiB per mapping.")
        files.append((destination, _git(root, "cat-file", "blob", object_id.decode())))
    if not files:
        raise ValueError("The test file source contains no files.")
    return files


def import_presets(store):
    """Complete pending presets once; edited/deleted sets are never reimported."""
    repos = {repo["display_name"]: repo for repo in store.repositories("main") if repo["listed"]}
    distribution, project = repos.get("r4os-distribution"), repos.get("r4os-project")
    if not all(repo and repo["local"].get("commit") and not repo["local"].get("dirty") for repo in (distribution, project)):
        return
    from .catalog import repository_path
    dist_root, project_root = repository_path(store, distribution), repository_path(store, project)
    dist_commit, project_commit = distribution["local"]["commit"], project["local"]["commit"]
    with store.connect() as db:
        pending = [record(row) for row in db.execute("SELECT * FROM test_configurations") if json.loads(row["origin"]).get("pending")]
    if not pending:
        return
    try:
        script = _git(project_root, "show", project_commit + ":Tools/BuildWorkspace.ps1").decode("utf-8-sig")
        for config in pending:
            preset = config["origin"].get("preset")
            folder, variable = ("TestInjection", "testImageIncludes") if preset == "standard" else ("BrowserTestInjection", "browserTestImageIncludes")
            match = re.search(r"\$" + variable + r"\s*=\s*@\((.*?)\n\)", script, re.S)
            if match is None:
                continue
            definition = dict(config["definition"], targets=re.findall(r"'(/[^']+)'", match[1]),
                              files=[{"repository": distribution["name"], "source": folder, "target": "/", "commit": dist_commit}],
                              startup=_git(dist_root, "show", dist_commit + ":" + folder + "/AUTOEXEC.BAT").decode("utf-8-sig"))
            if preset == "standard":
                definition["targets"] = sorted(set(definition["targets"]) | {
                    module["target"] for repo in repos.values() for module in repo["local"].get("modules", [])
                    if module.get("scope") == "test" and module.get("target")})
                definition["settings"] = _git(dist_root, "show", dist_commit + ":TestInjection/CONFIG.R4S").decode("utf-8-sig")
            origin = dict(config["origin"], pending=False, sources={distribution["name"]: dist_commit, project["name"]: project_commit})
            with store.connect() as db:
                db.execute("UPDATE test_configurations SET definition=?,origin=?,revision=revision+1,updated=? WHERE id=? AND revision=?",
                           (json.dumps(definition), json.dumps(origin), now(), config["id"], config["revision"]))
    except (ValueError, OSError, UnicodeError, subprocess.TimeoutExpired):
        # A pending preset remains visible and its missing sources block a build.
        return


def list_sets(store):
    import_presets(store)
    with store.connect() as db:
        return [record(row) for row in db.execute("SELECT * FROM test_configurations ORDER BY created,id")]


def _resolve(store, key, db, seen):
    if key in seen or len(seen) >= 20:
        raise ValueError("Test configurations must not contain an inheritance cycle.")
    config = get(store, key, connection=db)
    parent = config["definition"].get("extends")
    return (_resolve(store, parent, db, seen + [key]) if parent else []) + [config]


def snapshot(store, key, *, connection=None):
    if key is None:
        return None
    if connection is None:
        with store.connect() as db:
            db.execute("BEGIN")
            return snapshot(store, key, connection=db)
    sets = _resolve(store, key, connection, [])
    if any(config["origin"].get("pending") for config in sets):
        raise ValueError("Test presets are not available yet. Download and check Distribution and Project.")
    return {"id": sets[-1]["id"], "name": sets[-1]["name"], "revision": sets[-1]["revision"], "sets": sets,
            "targets": sorted({target for config in sets for target in config["definition"]["targets"]})}


def validate_definition(store, value):
    if not isinstance(value, dict) or set(value) - {"extends", "targets", "files", "settings", "startup"}:
        raise ValueError("Valid test settings are required.")
    parent = value.get("extends") or None
    if parent:
        identifier(parent)
    targets, files = value.get("targets", []), value.get("files", [])
    if not isinstance(targets, list) or len(targets) > 512 or not isinstance(files, list) or len(files) > 64:
        raise ValueError("Invalid test target or test file list.")
    definition = {"extends": parent, "targets": sorted({source_path(target, image=True) for target in targets}), "files": []}
    for field in ("settings", "startup"):
        text = value.get(field)
        if text is not None and (not isinstance(text, str) or len(text.encode("utf-8")) > 128 * 1024 or "\0" in text):
            raise ValueError("Test settings and startup commands must be text of at most 128 KiB.")
        definition[field] = text
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("A valid test file mapping is required.")
        repo = store.require_repository(identifier(entry.get("repository")))
        mapped = {"repository": repo["name"], "source": source_path(entry.get("source")),
                  "target": source_path(entry.get("target"), image=True), "commit": entry.get("commit") or repo["local"].get("commit")}
        read_mapping(store, mapped)
        definition["files"].append(mapped)
    return definition


def save(store, value, key=None):
    if not isinstance(value, dict):
        raise ValueError("Test configuration must be an object.")
    name, description = value.get("name", ""), value.get("description", "")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise ValueError("A name with 1 to 80 characters is required.")
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("Description is too long.")
    prior = get(store, key) if key else None
    template = get(store, value["template"]) if not key and value.get("template") else None
    definition = validate_definition(store, value.get("definition", (prior or template or {}).get("definition", {})))
    key = key or "tests-" + uuid.uuid4().hex[:12]
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if prior and (value.get("revision") != prior["revision"] or get(store, key, connection=db)["revision"] != prior["revision"]):
            raise Conflict("The test configuration has changed. Reload it.")
        if definition["extends"]:
            ancestors = _resolve(store, definition["extends"], db, [key])
            if any(item["id"] == key for item in ancestors):
                raise ValueError("A test configuration cannot extend itself.")
        if db.execute("SELECT 1 FROM test_configurations WHERE name=? COLLATE NOCASE AND id<>?", (name.strip(), key)).fetchone():
            raise Conflict("A test configuration with this name already exists.")
        origin = dict((prior or {}).get("origin", {}), pending=False, edited=True)
        if prior:
            db.execute("UPDATE test_configurations SET name=?,description=?,definition=?,origin=?,revision=revision+1,updated=? WHERE id=?",
                       (name.strip(), description, json.dumps(definition), json.dumps(origin), now(), key))
        else:
            db.execute("INSERT INTO test_configurations VALUES(?,?,?,?,?,?,?,?)",
                       (key, name.strip(), description, 1, json.dumps(definition), json.dumps(origin), now(), now()))
    return get(store, key)


def delete(store, key, revision):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        config = get(store, key, connection=db)
        if config["revision"] != revision:
            raise Conflict("The test configuration has changed. Reload it.")
        uses = ["Release type " + row["name"] for row in db.execute("SELECT name FROM release_types WHERE test_configuration=?", (key,))]
        uses += ["Test set " + row["name"] for row in db.execute("SELECT name FROM test_configurations WHERE json_extract(definition,'$.extends')=?", (key,))]
        for row in db.execute("SELECT id,payload FROM jobs WHERE status IN ('queued','running')"):
            payload = json.loads(row["payload"])
            frozen = payload.get("test_configuration") or {}
            release = payload.get("release", {})
            if any(item["id"] == key for item in frozen.get("sets", [])) or release.get("test_configuration") == key or ("test_configuration" not in payload and key == "standard-tests" and release.get("base") == "test"):
                uses.append("Job " + row["id"])
        if uses:
            raise Conflict("Test configuration is in use: " + ", ".join(uses) + ". Change the assignments or finish/cancel the jobs.")
        db.execute("DELETE FROM test_configurations WHERE id=?", (key,))
    return {"deleted": key}


def materialize(context, frozen, destination):
    if frozen is None:
        return None
    destination.mkdir(parents=True)
    total = 0
    for config in frozen["sets"]:
        context.check()
        definition = config["definition"]
        for entry in definition["files"]:
            for target, content in read_mapping(context.store, entry):
                total += len(content)
                if total > 64 * 1024 * 1024:
                    raise ValueError("The combined test configuration exceeds 64 MiB.")
                path = under(destination, target.lstrip("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(content)
        for field, name in (("settings", "CONFIG.R4S"), ("startup", "AUTOEXEC.BAT")):
            if definition[field] is not None:
                (destination / name).write_text(definition[field], encoding="utf-8")
    return destination
