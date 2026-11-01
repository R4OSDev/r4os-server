"""Editable release types and one-time, provenance-preserving import defaults."""
import json
from pathlib import Path
import re
import uuid

from .store import Conflict, now, identifier


CORE = {"r4os-kernel", "r4os-sdk", "r4os-contract", "r4os-distribution"}


def ensure_core_defaults(store):
    """Seed technical foundations once; explicit operator choices always win."""
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        known = {json.loads(row["metadata"]).get("display_name", row["name"]): row["name"] for row in db.execute("SELECT name,metadata FROM repositories WHERE connection_id='main' AND present=1")}
        for role in sorted(CORE & known.keys()):
            name = known[role]
            marker = "core-default:" + name
            if db.execute("SELECT 1 FROM kv WHERE key=?", (marker,)).fetchone():
                continue
            for row in db.execute("SELECT id,members FROM release_types WHERE id IN ('slim','full','test')").fetchall():
                members = json.loads(row["members"])
                prior = members.get(name, {})
                if prior.get("origin") != "manual":
                    members[name] = dict(prior, selected=True, origin="base", targets=[])
                    db.execute("UPDATE release_types SET members=?,revision=revision+1,updated=? WHERE id=?", (json.dumps(members), now(), row["id"]))
            db.execute("INSERT INTO kv VALUES(?,?)", (marker, 'true'))


def record(row):
    value = dict(row)
    value["members"] = json.loads(value["members"])
    return value


def list_types(store):
    ensure_core_defaults(store)
    with store.connect() as db:
        rows = db.execute("SELECT * FROM release_types ORDER BY CASE id WHEN 'slim' THEN 0 WHEN 'full' THEN 1 WHEN 'test' THEN 2 ELSE 3 END,created").fetchall()
    return [record(row) for row in rows]


def get_type(store, key, *, connection=None):
    identifier(key)
    if connection is None:
        with store.connect() as db:
            return get_type(store, key, connection=db)
    row = connection.execute("SELECT * FROM release_types WHERE id=?", (key,)).fetchone()
    if not row:
        raise ValueError("Release type not found.")
    return record(row)


def save_type(store, data, key=None):
    if not isinstance(data, dict):
        raise ValueError("Release configuration must be an object.")
    if not isinstance(data.get("name", ""), str) or not isinstance(data.get("targets", {}), dict):
        raise ValueError("Name must be text and target selection must be an object.")
    name = data.get("name", "").strip()
    if not name or len(name) > 80:
        raise ValueError("A name with 1 to 80 characters is required.")
    description = data.get("description", "")
    if not isinstance(description, str) or len(description) > 4000:
        raise ValueError("Description is too long.")
    prior = get_type(store, key) if key else None
    if prior and data.get("revision") != prior["revision"]:
        raise Conflict("The release configuration has changed. Reload it.")
    template = get_type(store, data["template"]) if not key and data.get("template") else None
    base = data.get("base", (prior or template or {}).get("base", "full"))
    if base not in ("slim", "full", "test"):
        raise ValueError("Invalid internal build profile.")
    test_configuration = data.get("test_configuration", prior.get("test_configuration") if prior else None)
    if test_configuration is not None:
        identifier(test_configuration)
    members = (prior or template or {}).get("members", {}).copy()
    if "selected" in data:
        selected = data["selected"]
        if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
            raise ValueError("Repository selection must be a list.")
        known = {item["name"] for item in store.repositories()}
        if set(selected) - known:
            raise ValueError("Repository selection contains unknown entries.")
        for repo in known | set(members):
            old = members.get(repo, {})
            chosen = repo in selected
            if old.get("selected", False) != chosen or (repo in selected and repo not in members):
                members[repo] = {"selected": chosen, "origin": "manual", "targets": [], "updated": now()}
    for repo, targets in data.get("targets", {}).items():
        known_ids = {item["id"] for item in store.require_repository(repo)["local"].get("modules", [])}
        if not isinstance(targets, list) or set(targets) - known_ids:
            raise ValueError("Unknown module target.")
        members[repo] = {"selected": bool(targets), "origin": "manual", "targets": targets, "updated": now()}
    key = key or "release-" + uuid.uuid4().hex[:12]
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if test_configuration:
            from .testconfigs import get
            get(store, test_configuration, connection=db)
        if template:
            get_type(store, template["id"], connection=db)
        if db.execute("SELECT 1 FROM release_types WHERE name=? COLLATE NOCASE AND id<>?", (name, key)).fetchone():
            raise Conflict("A release type with this name already exists.")
        if prior:
            changed = db.execute("UPDATE release_types SET name=?,description=?,base=?,test_configuration=?,revision=revision+1,members=?,updated=? WHERE id=? AND revision=?",
                                 (name, description, base, test_configuration, json.dumps(members), now(), key, prior["revision"]))
            if not changed.rowcount:
                raise Conflict("The release configuration has changed.")
        else:
            db.execute("INSERT INTO release_types(id,name,description,base,revision,members,created,updated,test_configuration) VALUES(?,?,?,?,?,?,?,?,?)",
                       (key, name, description, base, 1, json.dumps(members), now(), now(), test_configuration))
    return get_type(store, key)


def delete_type(store, key, revision):
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        release = get_type(store, key, connection=db)
        if release["revision"] != revision:
            raise Conflict("The release type has changed. Reload it.")
        if db.execute("SELECT 1 FROM jobs WHERE subject=? AND status IN ('queued','running')", ("release:" + key,)).fetchone():
            raise Conflict("This release type has queued or running jobs. Finish or cancel them first.")
        db.execute("DELETE FROM release_types WHERE id=?", (key,))
    return {"deleted": key}


def import_defaults(store, name, local, source=None):
    repo = store.repository(name)
    if repo["connection_id"] != "main" or not repo["listed"] or not repo["enabled"]:
        return
    role = repo["display_name"]
    if role == "r4os-distribution" and source:
        version = source / "Injection/R4OS/CONFIG/VERSION.R4S"
        if version.is_file():
            match = re.search(r"(?m)^RELEASE_VERSION=([0-9.]+)", version.read_text(encoding="utf-8-sig"))
            if match:
                store.set("release_default_version", match[1])
    if role == "r4os-project" and source and not store.setting("test_default_targets"):
        path = source / "Tools/BuildWorkspace.ps1"
        if path.is_file():
            match = re.search(r"\$testImageIncludes\s*=\s*@\((.*?)\n\)", path.read_text(), re.S)
            if match:
                targets = re.findall(r"'(/[^']+)'", match[1])
                store.set("test_default_targets", {"targets": targets, "commit": local.get("commit")})
                with store.connect() as db:
                    row = db.execute("SELECT members,test_configuration FROM release_types WHERE id='test'").fetchone()
                    if row and row["test_configuration"]:
                        members = json.loads(row[0])
                        for repo in store.repositories():
                            member = members.get(repo["name"])
                            if member and member.get("origin") == "manifest":
                                extra = [item["id"] for item in repo["local"].get("modules", []) if item.get("target") in targets]
                                if extra:
                                    member["targets"] = sorted(set(member.get("targets", [])) | set(extra))
                                    member["selected"] = True
                        db.execute("UPDATE release_types SET members=?,revision=revision+1,updated=? WHERE id='test'", (json.dumps(members), now()))
    if not local.get("commit") or not local.get("modules"):
        return
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        previous = db.execute("SELECT commit_id FROM imports WHERE name=?", (name,)).fetchone()
        canonical = all(module.get("validated") or "manifest" not in module for module in local["modules"])
        marker = "canonical-import:" + name
        repair = bool(previous and previous[0] == local["commit"] and canonical and not db.execute("SELECT 1 FROM kv WHERE key=?", (marker,)).fetchone())
        if previous and not repair:
            return
        for release in db.execute("SELECT * FROM release_types WHERE id IN ('slim','full','test')").fetchall():
            members = json.loads(release["members"])
            if name in members and (not repair or members[name].get("origin") != "manifest"):
                continue
            scopes = {"slim"} if release["id"] == "slim" or (release["id"] == "test" and not release["test_configuration"]) else {"slim", release["id"]}
            modules = [item["id"] for item in local["modules"] if item.get("scope") in scopes]
            if release["id"] == "test" and release["test_configuration"]:
                extras = store.setting("test_default_targets", {}).get("targets", [])
                modules = sorted(set(modules) | {item["id"] for item in local["modules"] if item.get("target") in extras})
            if role == "r4os-kernel":
                modules = ["kernel"]
            members[name] = {"selected": bool(modules), "targets": modules, "origin": "manifest", "commit": local["commit"]}
            db.execute("UPDATE release_types SET members=?,revision=revision+1,updated=? WHERE id=?",
                       (json.dumps(members), now(), release["id"]))
        if not previous:
            db.execute("INSERT INTO imports VALUES(?,?)", (name, local["commit"]))
        if canonical:
            db.execute("INSERT OR IGNORE INTO kv VALUES(?,?)", (marker, 'true'))


def preview(store, key):
    ensure_core_defaults(store)
    release = get_type(store, key)
    repos = {item["name"]: item for item in store.repositories()}
    main = {item["display_name"]: item for item in repos.values() if item["connection_id"] == "main" and item["listed"]}
    components, issues, dependencies = [], [], {}
    docs = main.get("r4os-docs")
    if not docs or not docs["local"].get("commit") or docs["local"].get("dirty"):
        issues.append("r4os-docs: a clean local revision is required for the existing document inventory.")
    else:
        dependencies[docs["name"]] = docs["local"]["commit"]
    for required in CORE:
        if required not in main or not release["members"].get(main[required]["name"], {}).get("selected"):
            issues.append(required + ": required as a system/build foundation.")
    if not store.setting("active_toolset"):
        issues.append("Build tools are not configured yet. Open Repositories → Tools.")
    else:
        from .toolsets import active
        try:
            _, tools = active(store)
        except ValueError as error:
            issues.append(str(error))
            tools = None
        for role in ("SDK", "Contract") if tools else ():
            source = tools["sources"][role]
            repo = repos.get(source["repository"])
            if repo and repo["local"].get("commit") != source["commit"]:
                issues.append(role + ": the selected source revision differs from the active toolset. Update Tools or activate the matching toolset.")
    for name, selection in release["members"].items():
        if not selection.get("selected"):
            continue
        repo = repos.get(name)
        if not repo or not repo["listed"] or not repo["local"].get("commit"):
            issues.append(name + ": not downloaded, enabled or available from its connection.")
            continue
        if repo["local"].get("dirty"):
            issues.append(name + ": local changes detected; use a clean source revision for releases.")
        wanted = selection.get("targets", [])
        missing_targets = set(wanted) - {module["id"] for module in repo["local"].get("modules", [])}
        if missing_targets:
            issues.append(name + ": selected targets are no longer available: " + ", ".join(sorted(missing_targets)))
        for module in repo["local"].get("modules", []):
            if wanted and module["id"] not in wanted:
                continue
            if module.get("kind") == "HostTools":
                continue
            if module["id"] == "recovery":
                issues.append("Recovery is included through the validated package from the Distribution pin. Build the recovery kernel individually and deselect it as an image component here.")
            if module.get("supported") is False:
                issues.append(name + ": " + module.get("reason", "Build target not supported."))
            component = dict(module, repository=name, commit=repo["local"]["commit"], source_ids={})
            components.append(component)
            for dependency in module.get("source_repositories", []):
                try:
                    source = store.source_repository(dependency, repo["connection_id"])
                except ValueError:
                    source = None
                if not source or not source["local"].get("commit") or source["local"].get("dirty"):
                    issues.append(dependency + ": a clean local source import is required for " + module["id"] + ".")
                else:
                    dependencies[source["name"]] = source["local"]["commit"]
                    component["source_ids"][dependency] = source["name"]
    if not any(main.get("r4os-kernel", {}).get("name") == item["repository"] for item in components):
        issues.append("The production kernel must be selected.")
    required_boot = {"HIDREPORT", "USBHID", "USBBOT", "USBSCSI", "XHCI", "USBMSC", "AHCI", "NVME", "ATAPIO"}
    missing = required_boot - {item["id"] for item in components}
    if missing:
        issues.append("Required boot components are missing: " + ", ".join(sorted(missing)))
    targets = set()
    providers = {item["id"] for item in components if item["kind"] == "R4L"}
    provider_modules = {item["id"]: item for item in components if item["kind"] == "R4L"}
    if sum(item["kind"] == "R4L" for item in components) != len(providers):
        issues.append("Library names have been selected more than once.")
    all_providers = {module["id"] for repo in repos.values() for module in repo["local"].get("modules", []) if module["kind"] == "R4L"}
    for module in components:
        target = module.get("target", "").casefold()
        if target and target in targets:
            issues.append("Duplicate target path: " + module["target"])
        targets.add(target)
        for value in module.get("imports", []):
            parts = value.split(":")
            if len(parts) >= 4 and parts[-1] == "1":
                continue
            if parts[0] in all_providers and parts[0] not in providers:
                issues.append(module["id"] + " requires library " + parts[0] + ".")
            provider = provider_modules.get(parts[0])
            if provider and "exports" in provider and len(parts) >= 3:
                exports = {item.split(":")[0]: int(item.split(":")[2]) for item in provider["exports"] if len(item.split(":")) >= 3}
                if exports.get(parts[1], 0) < int(parts[2]):
                    issues.append(module["id"] + " requires " + parts[0] + ":" + parts[1] + " mindestens ABI " + parts[2] + ".")
    from . import testconfigs
    frozen_tests = None
    try:
        frozen_tests = testconfigs.snapshot(store, release["test_configuration"])
        if frozen_tests:
            chosen = {module.get("target", "").casefold() for module in components}
            for target in frozen_tests["targets"]:
                if target.casefold() not in chosen:
                    issues.append("The test configuration requires the selected module target " + target + ". Add the required repository/build target to the selection.")
            for config in frozen_tests["sets"]:
                for entry in config["definition"]["files"]:
                    testconfigs.read_mapping(store, entry)
    except (ValueError, OSError) as error:
        issues.append("Test configuration: " + str(error))
    return {"release": release, "components": components, "issues": sorted(set(issues)), "ready": not issues,
            "default_version": store.setting("release_default_version", ""), "build_dependencies": dependencies,
            "test_configuration": frozen_tests, "main_sources": {name: item["name"] for name, item in main.items()}}
