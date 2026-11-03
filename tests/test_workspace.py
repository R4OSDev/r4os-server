"""Focused workspace contracts: real Git preservation, imports and private routes."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from email.message import Message
import json
import hashlib
import os
from pathlib import Path
import subprocess
import signal
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from r4os_server.workspace import catalog, releases, testconfigs, connections, layout, repo_metadata
from r4os_server.workspace.process import Context
from r4os_server.workspace.store import Store, under
from r4os_server.workspace.builds import private_libraries
from r4os_server.workspace.toolsets import clone_snapshot


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root / "workspace")
        connections.prepare_credentials(self.store.state, os.getuid(), os.getgid())
        (self.store.root / "Repositories").mkdir()

    def context(self, kind="check", subject=""):
        job = self.store.enqueue(kind, subject)
        self.store.change_job(job["id"], status="running")
        return Context(self.store, self.store.job(job["id"]))

    def seed(self, name, url=None):
        metadata = {"id": 10, "name": name, "description": "Example", "url": "https://github.com/R4OSDev/" + name,
                    "clone_url": url or "https://github.com/R4OSDev/" + name + ".git", "default_branch": "main"}
        with self.store.connect() as db:
            db.execute("INSERT INTO repositories(name,github_id,metadata) VALUES(?,?,?)", (name, 10, json.dumps(metadata)))

    def connection_catalog(self, owner, key, entries):
        items = [{"name": name, "id": number, "owner": {"login": owner}, "private": False, "default_branch": "main"} for name, number in entries]
        with patch.object(catalog, "public_json", return_value=(items, Message())):
            context = self.context("catalog")
            result = catalog.refresh(context, key)
            self.store.change_job(context.job["id"], status="completed")
            return result

    def test_connection_migration_preserves_checkouts_and_credentials_are_private(self):
        legacy = self.root / "legacy/state"
        legacy.mkdir(parents=True)
        with sqlite3.connect(legacy / "workspace.sqlite3") as db:
            db.execute("CREATE TABLE repositories(name TEXT PRIMARY KEY,github_id INTEGER UNIQUE NOT NULL,metadata TEXT NOT NULL,local TEXT NOT NULL DEFAULT '{}',present INTEGER NOT NULL DEFAULT 1)")
            db.execute("INSERT INTO repositories VALUES(?,?,?,?,1)", ("example", 1, json.dumps({"name": "example", "id": 1}), json.dumps({"directory": "existing", "commit": "a" * 40})))
        migrated = Store(legacy.parent)
        connections.prepare_credentials(migrated.state, os.getuid(), os.getgid())
        repo = migrated.repository("example")
        self.assertEqual((repo["connection_id"], repo["local"]["directory"], repo["enabled"]), ("main", "existing", True))
        self.assertEqual(len(connections.list_connections(Store(legacy.parent))), 1)
        old = legacy.parent / "Repositories/existing"
        old.mkdir(parents=True)
        (old / "local-change.txt").write_text("preserve local work")
        layout.migrate(migrated)
        layout.migrate(Store(legacy.parent))
        self.assertFalse(old.exists())
        self.assertEqual((legacy.parent / "Repositories/Main/example/local-change.txt").read_text(), "preserve local work")
        self.assertEqual(migrated.repository("example")["local"]["directory"], "Main/example")
        saved = connections.save(migrated, {"url": "https://github.com/Example/", "token": "test-credential-12345"})
        self.assertTrue(saved["has_token"])
        self.assertNotIn("test-credential-12345", json.dumps(saved))
        self.assertEqual(connections.secret_path(migrated, saved["id"]).stat().st_mode & 0o007, 0)
        # A failed replacement keeps both the old token and the saved revision.
        with self.assertLogs(level="ERROR") as logs, patch.object(Path, "replace", side_effect=PermissionError("test-credential-12345")):
            with self.assertRaises(connections.CredentialError):
                connections.save(migrated, {"url": saved["url"], "revision": saved["revision"], "token": "replacement-credential"}, saved["id"])
        self.assertNotIn("test-credential-12345", " ".join(logs.output))
        self.assertEqual(connections.token(migrated, saved["id"]), "test-credential-12345")
        self.assertEqual(connections.get(migrated, saved["id"])["revision"], saved["revision"])
        self.assertEqual(len(list((migrated.state / "secrets").iterdir())), 1)
        with patch.object(connections.os, "open", side_effect=PermissionError("private OS details")), self.assertLogs(level="ERROR"):
            self.assertTrue(connections.get(migrated, saved["id"])["has_token"])
            self.assertEqual(connections.token(migrated, "main"), "")
            with self.assertRaises(connections.CredentialError):
                connections.token(migrated, saved["id"])
        saved = connections.save(migrated, {"url": saved["url"], "revision": saved["revision"]}, saved["id"])
        self.assertTrue(saved["has_token"])
        saved = connections.save(migrated, {"url": saved["url"], "revision": saved["revision"], "remove_token": True}, saved["id"])
        self.assertFalse(saved["has_token"])
        for url in ("https://github.com.evil/account", "https://name@github.com/account", "https://github.com/account/repo", "https://github.com/account?token=bad"):
            with self.assertRaises(ValueError):
                connections.save(migrated, {"url": url})
        collision = Store(self.root / "collision")
        with collision.connect() as db:
            db.execute("INSERT INTO repositories(name,github_id,metadata) VALUES(?,?,?)", ("example", 2, json.dumps({"name": "example", "id": 2})))
        original = collision.root / "Repositories/example"
        original.mkdir(parents=True)
        (original / "keep").write_text("original")
        target = collision.root / "Repositories/Main/example"
        target.mkdir(parents=True)
        (target / "keep").write_text("unmanaged target")
        with self.assertRaises(ValueError):
            layout.migrate(collision)
        self.assertEqual((original / "keep").read_text(), "original")
        self.assertEqual((target / "keep").read_text(), "unmanaged target")
        (target / "keep").unlink()
        target.rmdir()
        layout.migrate(Store(collision.root))
        self.assertEqual((target / "keep").read_text(), "original")

    def test_connection_catalogues_selection_and_repository_names_are_isolated(self):
        extra = connections.save(self.store, {"url": "https://github.com/Example"})
        self.connection_catalog("R4OSDev", "main", [("same-name", 1)])
        self.connection_catalog("Example", extra["id"], [("same-name", 1)])
        main = self.store.repositories("main")[0]
        other = self.store.repositories(extra["id"], enabled_only=False)[0]
        self.assertNotEqual(main["name"], other["name"])
        self.assertTrue(main["enabled"])
        self.assertFalse(other["enabled"])
        self.assertEqual(main["directory"], "Main/same-name")
        self.assertEqual(other["directory"], extra["directory"] + "/same-name")
        with self.assertRaises(ValueError):
            self.store.require_repository(other["name"])
        connections.select(self.store, extra["id"], {"enabled": True})
        self.assertTrue(self.store.require_repository(other["name"])["enabled"])
        folder = layout.ensure_folder(self.store, extra)
        for occupied in ("next", "next-3"):
            (folder / occupied).mkdir()
            (folder / occupied / "keep").write_text("unmanaged")
        self.connection_catalog("Example", extra["id"], [("same-name", 1), ("next", 3), (".r4os-connection", 4)])
        added = next(repo for repo in self.store.repositories(extra["id"], enabled_only=False) if repo["id"] == 3)
        self.assertNotIn(Path(added["directory"]).name, ("next", "next-3"))
        self.assertEqual((folder / "next-3/keep").read_text(), "unmanaged")
        reserved = next(repo for repo in self.store.repositories(extra["id"], enabled_only=False) if repo["id"] == 4)
        self.assertNotEqual(reserved["directory"], extra["directory"] + "/.r4os-connection")
        self.assertEqual((folder / ".r4os-connection").read_text().strip(), extra["id"])
        connections.select(self.store, "main", {"enabled": False})
        self.connection_catalog("R4OSDev", "main", [("renamed", 1), ("new", 2)])
        self.assertFalse(self.store.repository(main["name"])["enabled"])
        self.assertEqual(self.store.repository(main["name"])["display_name"], "renamed")
        self.assertTrue(self.store.repository("new")["enabled"])
        self.connection_catalog("Example", extra["id"], [])
        self.assertFalse(self.store.repository(other["name"])["listed"])
        self.assertTrue(self.store.repository("new")["listed"])
        with self.assertRaises(ValueError):
            connections.select(self.store, extra["id"], {"repositories": ["new"], "enabled": True})

    def test_connection_deletion_waits_cleans_owned_files_and_can_be_retried(self):
        extra = connections.save(self.store, {"url": "https://github.com/Example", "token": "test-credential-12345"})
        self.connection_catalog("Example", extra["id"], [("example", 1)])
        name = self.store.repositories(extra["id"], enabled_only=False)[0]["name"]
        connections.select(self.store, extra["id"], {"enabled": True})
        layout.migrate(self.store)
        layout.ensure_folder(self.store, extra)
        checkout = catalog.repository_path(self.store, self.store.repository(name))
        checkout.mkdir()
        (checkout / "uncommitted.txt").write_text("local")
        unrelated = self.store.root / "Repositories/keep"
        unrelated.mkdir()
        (unrelated / "keep.txt").write_text("keep")
        queued = self.store.enqueue("download", name)
        running = self.store.enqueue("build", name, {"commit": "a" * 40})
        self.store.change_job(running["id"], status="running")
        removal = connections.request_delete(self.store, extra["id"], True)
        self.assertEqual(self.store.job(queued["id"])["status"], "cancelled")
        with self.assertRaises(ValueError):
            self.store.enqueue("download", name)
        context = Context(self.store, removal)
        with self.assertRaises(ValueError):
            connections.remove(context)
        self.assertTrue(checkout.exists())
        self.store.change_job(running["id"], status="completed")
        with patch.object(connections.shutil, "rmtree", side_effect=OSError("temporary failure")):
            with self.assertRaises(OSError):
                connections.remove(context)
        self.assertEqual(connections.get(self.store, extra["id"])["status"], "delete_failed")
        self.assertTrue(connections.get(self.store, extra["id"])["has_token"])
        connections.remove(context)
        self.assertFalse(checkout.exists())
        self.assertTrue((unrelated / "keep.txt").exists())
        self.assertFalse(connections.secret_path(self.store, extra["id"]).exists())
        with self.assertRaises(ValueError):
            self.store.repository(name)
        with self.assertRaises(ValueError):
            connections.request_delete(self.store, "main", True)

    def test_main_account_change_keeps_existing_references_and_blocks_name_rebinding(self):
        self.connection_catalog("R4OSDev", "main", [("r4os-sdk", 1)])
        release = releases.save_type(self.store, {"name": "Pinned", "selected": ["r4os-sdk"]})
        with self.store.connect() as db:
            db.execute("UPDATE jobs SET status='completed'")
        main = connections.get(self.store, "main")
        connections.save(self.store, {"url": "https://github.com/NewOrg", "revision": main["revision"]}, "main")
        self.connection_catalog("NewOrg", "main", [("r4os-sdk", 2)])
        source = self.store.source_repository("r4os-sdk")
        self.assertNotEqual(source["name"], "r4os-sdk")
        self.assertTrue(releases.get_type(self.store, release["id"])["members"]["r4os-sdk"]["selected"])
        with self.assertRaises(ValueError):
            self.store.require_repository("r4os-sdk")

    def test_real_git_download_fast_forward_and_local_changes(self):
        source = self.root / "upstream"
        source.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True, check=True).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.name", "Workspace Test")
        git("config", "user.email", "test@example.invalid")
        (source / "README.md").write_text("one\n")
        (source / "module.R4MF").write_text("R4OS_MODULE_MANIFEST=2\nNAME=EXAMPLE\nKIND=R4X\nLANGUAGE=Zig\nVERSION=1.0.0\nIMAGE_SCOPE=slim\nTARGET=/EXAMPLE.R4X\n")
        git("add", ".")
        git("commit", "-m", "first")
        self.seed("r4os-app-example", source.as_uri())
        context = self.context("download", "r4os-app-example")
        first = catalog.download(context, "r4os-app-example")
        self.assertEqual(first["status"], "current")
        with self.assertRaisesRegex(ValueError, "no saved targets"):
            catalog.batch(context, "update")
        (source / "README.md").write_text("two\n")
        git("commit", "-am", "second")
        self.assertEqual(catalog.inspect(context, "r4os-app-example")["status"], "outdated")
        catalog.update(context, "r4os-app-example")
        target = catalog.repository_path(self.store, self.store.repository("r4os-app-example")) / "README.md"
        self.assertEqual(target.read_text(), "two\n")
        target.write_text("keep my work\n")
        with self.assertRaises(ValueError):
            catalog.update(context, "r4os-app-example")
        self.assertEqual(target.read_text(), "keep my work\n")

    def test_bulk_updates_pin_targets_and_preserve_changes_while_waiting(self):
        from r4os_server.workspace.worker import execute
        source = self.root / "bulk-upstream"
        source.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True, text=True).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.name", "Workspace Test")
        git("config", "user.email", "test@example.invalid")
        (source / "README.md").write_text("one\n")
        git("add", ".")
        git("commit", "-m", "first")
        context = self.context()
        names = ["current", "empty", "modified", "missing", "disabled", "removed", "late", *["queued-" + str(i) for i in range(1, 5)]]
        for number, name in enumerate(names, 1):
            metadata = {"id": number, "name": name, "display_name": name, "owner": "R4OSDev",
                        "url": "https://github.com/R4OSDev/" + name, "clone_url": source.as_uri(), "default_branch": "main"}
            with self.store.connect() as db:
                db.execute("INSERT INTO repositories(name,github_id,metadata) VALUES(?,?,?)", (name, number, json.dumps(metadata)))
            if name not in ("missing", "empty"):
                catalog.download(context, name)
            else:
                self.store.local(name, {"status": name})
        (catalog.repository_path(self.store, self.store.repository("modified")) / "README.md").write_text("local work\n")
        catalog.inspect(context, "modified")
        (source / "README.md").write_text("two\n")
        git("commit", "-am", "second")
        for name in names:
            if name.startswith("queued-") or name in ("disabled", "removed"):
                catalog.inspect(context, name)
        with self.store.connect() as db:
            db.execute("UPDATE repositories SET present=0 WHERE name='removed'")
            row = self.store.repository("disabled")
            db.execute("UPDATE repositories SET metadata=? WHERE name='disabled'", (json.dumps(dict(row, disabled=True)),))
        other = connections.save(self.store, {"url": "https://github.com/Other"})
        self.connection_catalog("Other", other["id"], [("other", 99)])
        connections.select(self.store, other["id"], {"enabled": True})
        other_repo = self.store.repositories(other["id"])[0]
        self.store.local(other_repo["name"], {"status": "outdated", "commit": "a" * 40})
        job = self.store.enqueue("update_all", payload={"connection_id": "main", "targets": [{"name": "current"}]})
        self.assertEqual([item["name"] for item in job["payload"]["targets"]], ["queued-" + str(i) for i in range(1, 5)])
        catalog.inspect(context, "late")  # An update discovered after queueing is not added.
        self.assertEqual(self.store.enqueue("update_all", payload={"connection_id": "main"})["payload"], job["payload"])
        dirty = catalog.repository_path(self.store, self.store.repository("queued-1")) / "README.md"
        dirty.write_text("preserve changes made while queued\n")
        with self.store.connect() as db:
            db.execute("UPDATE repositories SET enabled=0 WHERE name='queued-2'")
            db.execute("DELETE FROM repositories WHERE name='queued-3'")
        self.store.change_job(job["id"], status="running")
        with patch.object(catalog, "inspect", wraps=catalog.inspect) as inspected:
            result = execute(Context(self.store, job))
        self.assertEqual({call.args[1] for call in inspected.call_args_list}, {"queued-1", "queued-4"})
        self.assertEqual(result["target_count"], 4)
        self.assertEqual([item["status"] for item in result["repositories"]], ["failed", "skipped", "skipped", "current"])
        self.assertEqual(result["failed"], 1)
        self.assertEqual(dirty.read_text(), "preserve changes made while queued\n")
        for name in ("current", "late", "queued-2", "queued-4"):
            path = catalog.repository_path(self.store, self.store.repository(name)) / "README.md"
            self.assertEqual(path.read_text(), "two\n" if name == "queued-4" else "one\n")
        self.store.change_job(job["id"], status="completed")
        with self.store.connect() as db:
            db.execute("UPDATE repositories SET enabled=0 WHERE name IN ('queued-1','late')")
        with self.assertRaisesRegex(ValueError, "No known updates"):
            self.store.enqueue("update_all", payload={"connection_id": "main"})
        with patch.object(catalog, "inspect", side_effect=AssertionError("Legacy jobs must not scan")):
            with self.assertRaisesRegex(ValueError, "no saved targets"):
                execute(Context(self.store, dict(job, payload={})))

    def test_private_catalogues_selection_visibility_and_access_changes(self):
        extra = connections.save(self.store, {"url": "https://github.com/Example", "token": "isolated-private-catalog-token"})
        phase = {"new": False, "denied": False, "privatized": False, "partial": False}
        def item(name, number, owner, private=False):
            return {"name": name, "id": number, "owner": {"login": owner}, "private": private, "default_branch": "main"}
        def github(url, credential=""):
            if "/branches?" in url:
                if "/blocked/" in url or (phase["denied"] and "/renamed/" in url):
                    raise catalog.GitHubError(403, "Contents permission denied")
                return [], Message()
            owner = "R4OSDev" if "/orgs/" in url else "Example"
            if not credential:
                return ([] if phase["privatized"] else [item("public", 1, owner)]), Message()
            if "page=2" in url:
                if phase["partial"]:
                    raise RuntimeError("Interrupted private catalogue")
                return ([item("later", 4, owner, True)] if phase["new"] else []), Message()
            result = [item("renamed" if phase["new"] else "secret", 2, owner, True), item("blocked", 3, owner, True), item("foreign", 66, "OtherOwner", True)]
            if phase["privatized"]:
                result.append(item("public", 1, owner, True))
            headers = Message();headers['Link'] = '<https://api.github.com/user/repos?page=2>; rel="next"'
            return result, headers
        context = self.context()
        with patch.object(catalog, "public_json", side_effect=github):
            catalog.refresh(context, extra["id"])
            repos = self.store.repositories(extra["id"], enabled_only=False)
            self.assertEqual(len(repos), 3)
            self.assertTrue(all(not repo["enabled"] for repo in repos))
            self.assertNotIn(66, [repo["id"] for repo in repos])
            secret = next(repo for repo in repos if repo["id"] == 2)
            blocked = next(repo for repo in repos if repo["id"] == 3)
            self.assertFalse(blocked["available"])
            with self.assertRaises(ValueError):
                connections.select(self.store, extra["id"], {"enabled": True, "repositories": [blocked["name"]]})
            connections.select(self.store, extra["id"], {"enabled": True})
            repo_metadata.save(self.store, secret["name"], {"category": "Core"})
            phase["new"] = True
            catalog.refresh(context, extra["id"])
            renamed = self.store.repository(secret["name"])
            self.assertEqual((renamed["display_name"], renamed["directory"], renamed["server_metadata"]["category"]), ("renamed", secret["directory"], "Core"))
            self.assertTrue(renamed["enabled"])
            self.assertFalse(next(repo for repo in self.store.repositories(extra["id"], enabled_only=False) if repo["id"] == 4)["enabled"])
            main = connections.get(self.store, "main")
            connections.save(self.store, {"url": main["url"], "revision": main["revision"], "token": "isolated-main-catalog-token"}, "main")
            catalog.refresh(context, "main")
            self.assertTrue(self.store.repository("public")["enabled"])
            self.assertFalse(self.store.repository("renamed")["enabled"])
            phase["privatized"] = True
            catalog.refresh(context, "main")
            self.assertFalse(self.store.repository("public")["enabled"])
            phase["privatized"] = False
            current = connections.get(self.store, extra["id"])
            connections.save(self.store, {"url": current["url"], "revision": current["revision"], "remove_token": True}, extra["id"])
            self.assertFalse(self.store.repository(secret["name"])["available"])
            with self.assertRaises(ValueError):
                self.store.enqueue("download", secret["name"])
            catalog.refresh(context, extra["id"])
            self.assertTrue(self.store.repository(secret["name"])["enabled"])
            self.assertFalse(self.store.repository(secret["name"])["listed"])
            current = connections.get(self.store, extra["id"])
            connections.save(self.store, {"url": current["url"], "revision": current["revision"], "token": "isolated-replacement-token"}, extra["id"])
            catalog.refresh(context, extra["id"])
            self.assertTrue(self.store.require_repository(secret["name"])["available"])
            phase["denied"] = True
            catalog.refresh(context, extra["id"])
            self.assertFalse(self.store.repository(secret["name"])["available"])
            self.assertTrue(self.store.repository(secret["name"])["enabled"])
            before = self.store.repositories(extra["id"], enabled_only=False)
            phase["partial"] = True
            with self.assertRaisesRegex(RuntimeError, "Interrupted private"):
                catalog.refresh(context, extra["id"])
            self.assertEqual(self.store.repositories(extra["id"], enabled_only=False), before)

    def test_repository_categories_survive_refresh_and_are_connection_local(self):
        extra = connections.save(self.store, {"url": "https://github.com/Example"})
        self.connection_catalog("R4OSDev", "main", [("same-name", 1)])
        self.connection_catalog("Example", extra["id"], [("same-name", 1)])
        main = self.store.repositories("main")[0]
        other = self.store.repositories(extra["id"], enabled_only=False)[0]
        self.assertEqual(main["server_metadata"], {"category": "Undefined"})
        repo_metadata.save(self.store, main["name"], {"category": "Core"})
        repo_metadata.save(self.store, other["name"], {"category": "Game"})
        self.connection_catalog("R4OSDev", "main", [("renamed", 1)])
        self.store.local(main["name"], {"status": "current", "commit": "a" * 40})
        self.assertEqual(Store(self.store.root).repository(main["name"])["server_metadata"], {"category": "Core"})
        self.assertEqual(self.store.repository(other["name"])["server_metadata"], {"category": "Game"})
        for value in ({"category": "Unknown"}, {"category": "Core", "private": False}, {}):
            with self.assertRaises(ValueError):
                repo_metadata.save(self.store, main["name"], value)
        self.assertEqual(len(repo_metadata.CATEGORIES), len(set(repo_metadata.CATEGORIES)))
        self.assertEqual(repo_metadata.save(self.store, main["name"], {"category": "Undefined"})["server_metadata"], {"category": "Undefined"})

    def test_private_git_credentials_are_scoped_and_not_persisted(self):
        from r4os_server.workspace.git_askpass import answer
        upstream = self.root / "private-upstream"
        upstream.mkdir()
        def git(*args):
            return subprocess.run(["/usr/bin/git", "-C", str(upstream), *args], capture_output=True, text=True, check=True).stdout.strip()
        git("init", "-b", "main");git("config", "user.name", "Fixture");git("config", "user.email", "fixture@example.invalid")
        (upstream / "README.md").write_text("one\n");git("add", ".");git("commit", "-m", "first")
        self.seed("private-source")
        main = connections.get(self.store, "main")
        main = connections.save(self.store, {"url": main["url"], "revision": main["revision"], "token": "isolated-git-fixture-token"}, "main")
        with self.store.connect() as db:
            row = self.store.repository("private-source")
            metadata = dict(row, private=True, read_access=True, access_revision=main["revision"], display_name="private-source", owner="R4OSDev")
            db.execute("UPDATE repositories SET metadata=? WHERE name='private-source'", (json.dumps(metadata),))
        # Replace transport only: real Git credential fill invokes the real helper,
        # then real Git clone/fetch operates on a local upstream instead of GitHub.
        shim = self.root / "git-bin";shim.mkdir()
        script = shim / "git"
        script.write_text('''#!/usr/bin/python3
import os,subprocess,sys
a=sys.argv[1:];opts=[]
while a and a[0]=='-c':opts.extend(a[:2]);a=a[2:]
network=bool(os.environ.get('R4OS_GIT_URL'))
if network:
    filled=subprocess.run(['/usr/bin/git',*opts,'credential','fill'],input='protocol=https\\nhost=github.com\\npath=R4OSDev/private-source.git\\n\\n',capture_output=True,text=True,check=True)
    fields=dict(line.split('=',1) for line in filled.stdout.splitlines() if '=' in line)
    assert fields['username']=='x-access-token' and fields['password']=='isolated-git-fixture-token'
    assert all('isolated-git-fixture-token' not in value for value in os.environ.values())
    source=os.environ['FIXTURE_UPSTREAM']
    url=os.environ['R4OS_GIT_URL']
    a=[source if v==url else v for v in a]
    opts+=['-c','protocol.file.allow=always','-c','url.'+source+'.insteadOf='+url]
result=subprocess.run(['/usr/bin/git',*opts,*a])
if result.returncode==0 and network and a[0]=='clone':
    subprocess.run(['/usr/bin/git','-C',a[-1],'remote','set-url','origin',os.environ['R4OS_GIT_URL']],check=True)
sys.exit(result.returncode)
''')
        script.chmod(0o755)
        context = self.context("download", "private-source")
        original_run = context.run
        def run(*args, **kwargs):
            kwargs['env'] = dict(kwargs.get('env') or {}, FIXTURE_UPSTREAM=upstream.as_uri())
            return original_run(*args, **kwargs)
        with patch.dict(os.environ, {"PATH": str(shim) + os.pathsep + os.environ["PATH"]}), patch.object(context, "run", side_effect=run):
            self.assertEqual(catalog.download(context, "private-source")["status"], "current")
            (upstream / "README.md").write_text("two\n");git("commit", "-am", "second")
            self.assertEqual(catalog.inspect(context, "private-source")["status"], "outdated")
            self.assertEqual(catalog.update(context, "private-source")["status"], "current")
        path = catalog.repository_path(self.store, self.store.repository("private-source"))
        self.assertEqual((path / "README.md").read_text(), "two\n")
        for text in (context.log_path.read_text(), (path / ".git/config").read_text(), json.dumps(self.store.jobs())):
            self.assertNotIn("isolated-git-fixture-token", text)
        self.assertFalse(list(self.store.state.glob('.git-auth-*')))
        expected = self.store.repository("private-source")["clone_url"]
        with self.assertRaises(ValueError):
            answer("Password for 'https://x-access-token@evil.example/R4OSDev/private-source.git': ", expected, connections.secret_path(self.store, "main"))
        with self.assertRaises(ValueError):
            answer("Password for 'https://x-access-token@github.com/Other/private-source.git': ", expected, connections.secret_path(self.store, "main"))

    def test_catalog_follows_pages_and_keeps_previous_complete_inventory(self):
        def page(name, key, more):
            headers = Message()
            if more:
                headers["Link"] = '<https://api.github.com/page2>; rel="next"'
            return [{"name": name, "id": key, "owner": {"login": "R4OSDev"}, "private": False, "default_branch": "main"}], headers
        with patch.object(catalog, "public_json", side_effect=[page("first", 1, True), page("second", 2, False)]) as fetch:
            result = catalog.refresh(self.context("catalog"))
        self.assertEqual(result["count"], 2)
        self.assertEqual(fetch.call_count, 2)
        with patch.object(catalog, "public_json", side_effect=RuntimeError("offline")):
            with self.assertRaises(RuntimeError):
                catalog.refresh(self.context("catalog"))
        self.assertEqual(len(self.store.repositories()), 2)

    def test_import_defaults_preserve_manual_selection_and_detect_stale_edits(self):
        self.seed("example")
        local = {"commit": "a" * 40, "modules": [{"id": "MOD", "scope": "slim", "kind": "R4X"}]}
        releases.import_defaults(self.store, "example", local)
        full = releases.get_type(self.store, "full")
        self.assertTrue(full["members"]["example"]["selected"])
        changed = releases.save_type(self.store, {"name": "Full", "base": "full", "revision": full["revision"], "selected": []}, "full")
        releases.import_defaults(self.store, "example", dict(local, commit="b" * 40))
        self.assertFalse(releases.get_type(self.store, "full")["members"]["example"]["selected"])
        with self.assertRaises(releases.Conflict):
            releases.save_type(self.store, {"name": "Full", "base": "full", "revision": full["revision"]}, "full")
        custom = releases.save_type(self.store, {"name": "Development", "base": "full", "template": "full"})
        self.assertNotIn(custom["id"], ("slim", "full", "test"))
        self.assertEqual(custom["members"], changed["members"])

    def test_atomic_duplicate_queue_and_path_boundaries(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            keys = list(pool.map(lambda _: self.store.enqueue("download", "example")["id"], range(12)))
        self.assertEqual(len(set(keys)), 1)
        for path in ("../outside", "/etc/passwd", "."):
            with self.assertRaises(ValueError):
                under(self.store.root, path)

    def test_test_sets_release_defaults_and_deletion_preserve_history(self):
        config = testconfigs.save(self.store, {"name": "Example tests", "definition": {"startup": "CHECK\n"}})
        child = testconfigs.save(self.store, {"name": "Extended tests", "definition": {"extends": config["id"]}})
        with self.assertRaisesRegex(releases.Conflict, "Test set Extended tests"):
            testconfigs.delete(self.store, config["id"], config["revision"])
        release = releases.save_type(self.store, {"name": "Custom", "test_configuration": child["id"]})
        copied = releases.save_type(self.store, {"name": "Copied", "template": release["id"]})
        self.assertIsNone(copied["test_configuration"])
        with self.assertRaisesRegex(releases.Conflict, "Release type Custom"):
            testconfigs.delete(self.store, child["id"], child["revision"])
        frozen = testconfigs.snapshot(self.store, child["id"])
        job = self.store.enqueue("release", "release:" + release["id"], {"release": release, "test_configuration": frozen})
        with self.assertRaises(releases.Conflict):
            releases.delete_type(self.store, release["id"], release["revision"])
        release = releases.save_type(self.store, {"name": "Custom", "revision": release["revision"], "test_configuration": None}, release["id"])
        with self.assertRaisesRegex(releases.Conflict, "Job"):
            testconfigs.delete(self.store, child["id"], child["revision"])
        self.store.change_job(job["id"], status="completed", result={"files": []})
        releases.delete_type(self.store, release["id"], release["revision"])
        testconfigs.delete(self.store, child["id"], child["revision"])
        testconfigs.delete(self.store, config["id"], config["revision"])
        self.assertEqual(self.store.job(job["id"])["payload"]["test_configuration"], frozen)
        original_test = releases.get_type(self.store, "test")
        releases.delete_type(self.store, "test", original_test["revision"])
        for preset in ("browser-tests", "standard-tests"):
            entry = testconfigs.get(self.store, preset)
            testconfigs.delete(self.store, preset, entry["revision"])
        restarted = Store(self.store.root)
        self.assertNotIn("test", [item["id"] for item in releases.list_types(restarted)])
        self.assertEqual(testconfigs.list_sets(restarted), [])
        queued = restarted.enqueue("release", "release:" + copied["id"], {"release": copied})
        self.assertEqual(restarted.cancel_job(queued["id"])["status"], "cancelled")
        releases.delete_type(restarted, copied["id"], copied["revision"])

    def test_pinned_test_files_inheritance_and_empty_selection(self):
        self.seed("r4os-distribution")
        source = self.store.root / "Repositories/r4os-distribution"
        source.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", str(source), *args], capture_output=True, text=True, check=True).stdout.strip()
        git("init", "-b", "main")
        git("config", "user.name", "Workspace Test")
        git("config", "user.email", "test@example.invalid")
        (source / "TestInjection").mkdir()
        (source / "TestInjection/CONFIG.R4S").write_text("LOG=verbose\n")
        (source / "TestInjection/AUTOEXEC.BAT").write_text("OLD\n")
        git("add", ".")
        git("commit", "-m", "first")
        commit = git("rev-parse", "HEAD")
        self.store.local("r4os-distribution", {"commit": commit})
        parent = testconfigs.save(self.store, {"name": "Pinned", "definition": {"files": [
            {"repository": "r4os-distribution", "source": "TestInjection", "target": "/"}]}})
        child = testconfigs.save(self.store, {"name": "Child", "definition": {"extends": parent["id"], "startup": "CUSTOM\n"}})
        frozen = testconfigs.snapshot(self.store, child["id"])
        (source / "TestInjection/CONFIG.R4S").write_text("CHANGED\n")
        git("commit", "-am", "second")
        testconfigs.save(self.store, {"name": "Child", "revision": child["revision"], "definition": {"startup": "LATER\n"}}, child["id"])
        output = self.root / "overlay"
        testconfigs.materialize(self.context(), frozen, output)
        self.assertEqual((output / "CONFIG.R4S").read_text(), "LOG=verbose\n")
        self.assertEqual((output / "AUTOEXEC.BAT").read_text(), "CUSTOM\n")
        self.assertIsNone(testconfigs.materialize(self.context(), None, self.root / "none"))
        self.assertFalse((self.root / "none").exists())
        with self.assertRaises(ValueError):
            testconfigs.save(self.store, {"name": "Escape", "definition": {"files": [
                {"repository": "r4os-distribution", "source": "TestInjection", "target": "/../escape"}]}})
        with self.assertRaises(ValueError):
            testconfigs.save(self.store, {"name": "Cycle", "revision": parent["revision"], "definition": {"extends": parent["id"]}}, parent["id"])

    def test_logs_filter_before_limit_and_configuration_assignment_is_atomic(self):
        old = self.store.enqueue("tools_check", "tools")
        for index in range(55):
            self.store.enqueue("check", str(index))
        self.assertEqual([job["id"] for job in self.store.jobs(category="tools")], [old["id"]])
        self.assertEqual(self.store.job_count(category="repositories"), 55)
        self.assertEqual(len(self.store.jobs(category="repositories", limit=50, offset=50)), 5)
        config = testconfigs.save(self.store, {"name": "Concurrent"})
        def attach():
            try:
                return releases.save_type(self.store, {"name": "Concurrent release", "test_configuration": config["id"]})
            except ValueError:
                return None
        def delete():
            try:
                return testconfigs.delete(self.store, config["id"], config["revision"])
            except releases.Conflict:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            operations = [pool.submit(attach), pool.submit(delete)]
            attached, deleted = [operation.result() for operation in operations]
        self.assertNotEqual(attached is None, deleted is None)
        if attached:
            self.assertEqual(testconfigs.get(self.store, attached["test_configuration"])["id"], config["id"])

    def test_git_snapshots_preserve_owner_line_endings_and_checkout(self):
        source = self.root / "upstream"
        source.mkdir()
        def git(*args):
            return subprocess.run(["git", "-C", source, *args], capture_output=True, check=True).stdout
        git("init", "-b", "main")
        git("config", "user.name", "Workspace Test")
        git("config", "user.email", "test@example.invalid")
        (source / ".gitattributes").write_text("*.ps1 text eol=crlf\n")
        canonical = b"Write-Host 'recipe'\n"
        (source / "Recipe.ps1").write_bytes(canonical)
        git("add", ".")
        git("commit", "-m", "recipe with canonical source hash")
        (source / "Recipe.ps1").unlink()
        git("checkout", "--", "Recipe.ps1")
        self.assertIn(b"\r\n", (source / "Recipe.ps1").read_bytes())
        context = self.context()
        destination = self.store.root / "Repositories/Libraries"
        clone_snapshot(context, source, destination, git("rev-parse", "HEAD").decode().strip())
        self.assertEqual((destination / "Recipe.ps1").read_bytes().replace(b"\r\n",b"\n"), canonical)
        self.assertIn(b"\r\n", (source / "Recipe.ps1").read_bytes())
        self.assertFalse((self.store.root/'Generated/library-script-sources.json').exists())

    def test_timeout_stops_orphaned_process_group_and_closed_output(self):
        marker = self.root / "descendant.json"
        child = ("import json,os,signal,time;from pathlib import Path;"
                 "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                 "Path(" + repr(str(marker)) + ").write_text(json.dumps({'pid':os.getpid(),'group':os.getpgrp()}));"
                 "time.sleep(60)")
        parent = ("import subprocess,sys,time;from pathlib import Path;"
                  "subprocess.Popen([sys.executable,'-c'," + repr(child) + "]);"
                  "\nwhile not Path(" + repr(str(marker)) + ").exists():time.sleep(.01)")
        try:
            with self.assertRaisesRegex(RuntimeError, "timed out"):
                self.context("process", "orphan").run([sys.executable, "-c", parent], timeout=.5)
            data = json.loads(marker.read_text())
            state = Path('/proc') / str(data['pid']) / 'stat'
            self.assertTrue(not state.exists() or state.read_text().split()[2] == 'Z',
                            "Descendant survived after its parent exited")
        finally:
            if marker.exists():
                try:
                    os.killpg(json.loads(marker.read_text())['group'], signal.SIGKILL)
                except ProcessLookupError:
                    pass
        with self.assertRaisesRegex(RuntimeError, "timed out"):
            self.context("process", "closed-output").run(
                [sys.executable, "-c", "import os,time;os.close(1);os.close(2);time.sleep(60)"], timeout=.1)


if __name__ == "__main__":
    unittest.main()
