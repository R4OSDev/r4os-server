"""Single local queue worker, deliberately independent of service credentials."""
import argparse
import fcntl
import json
import logging
from pathlib import Path
import signal
import threading
import time

from . import catalog, connections, layout
from .process import Context, Cancelled, scrub
from .store import Store, now


def execute(context):
    job = context.job
    kind, subject, payload = job["kind"], job["subject"], job["payload"]
    if kind.startswith('sdk_'):
        from . import sdk_packages
        if kind == 'sdk_catalog':
            from .sdk_catalog import refresh_job
            return refresh_job(context, payload)
        if kind == 'sdk_select':
            from .sdk_install import select
            return select(context, payload)
        if kind == 'sdk_release_link':
            from .sdk_releases import attach
            return attach(context,payload)
        if kind == 'sdk_import': return sdk_packages.import_release(context,payload)
        if kind == 'sdk_verify': return {'sdk_id':payload['sdk_id'],'verification':sdk_packages.verify(context,payload['sdk_id'])}
        if kind == 'sdk_profile':
            if payload['action']=='resolve': return sdk_packages.resolve(context,payload['sdk_id'],payload['profile'])
            return sdk_packages.cli(context,payload['sdk_id'],'Profile.ps1',['-Action','Describe','-Profile',payload['profile']])
        if kind == 'sdk_cleanup':
            from .sdk_cleanup import cleanup
            return cleanup(context)
        if kind == 'sdk_develop':
            from .sdk_projects import create_development
            return create_development(context,payload)
    if kind == "connection_delete":
        return connections.remove(context)
    if kind == "app_publish":
        from .apps import publish
        context.phase("Reading the app listing manifest")
        return publish(context.store, subject, {"published": True}, job_id=job['id'], expected_revision=payload['connection_revision'])
    if kind == "github_status":
        from .github_build import refresh
        return refresh(context, subject)
    if kind == "build_publish":
        from .github_build import run
        return run(context, subject, payload)
    if kind == "catalog":
        keys = [payload["connection_id"]] if payload.get("connection_id") else [item["id"] for item in connections.list_connections(context.store) if item["status"] == "ready"]
        results = []
        for key in keys:
            try:
                results.append(catalog.refresh(context, key))
            except Cancelled:
                raise
            except Exception as error:
                previous = connections.get(context.store, key)["catalog"]
                previous["error"] = scrub(str(error))
                with context.store.connect() as db:
                    db.execute("UPDATE connections SET catalog=? WHERE id=?", (json.dumps(previous), key))
                if key == "main":
                    context.store.set("catalog", previous)
                if len(keys) == 1:
                    raise
                context.log(previous["error"])
                results.append({"connection_id": key, "error": previous["error"]})
        return {"connections": results, "count": sum(item.get("count", 0) for item in results), "failed": sum("error" in item for item in results)}
    if kind == "download":
        return catalog.download(context, subject)
    if kind == 'refresh':
        return catalog.refresh_repository(context, subject, payload)
    if kind == "update":
        return catalog.update(context, subject)
    if kind in ("download_all", "update_all"):
        return catalog.batch(context, "download" if kind == "download_all" else "update", payload.get("connection_id"), targets=payload.get("targets"))
    if kind == "check":
        if subject:
            return catalog.inspect(context, subject, remote=not payload.get("profile_refresh",False))
        checked = []
        for repo in context.store.repositories(payload.get("connection_id")):
            if not repo["available"]:
                continue
            if catalog.repository_path(context.store, repo).exists() or repo["local"].get("commit"):
                context.phase("Checking source status: " + repo["name"])
                checked.append({"name": repo["name"], **catalog.inspect(context, repo["name"])})
        return {"repositories": checked}
    if kind.startswith("tools_"):
        from .toolsets import perform
        return perform(context, kind.removeprefix("tools_"), payload)
    if kind == "build":
        from .builds import build
        return build(context, subject, payload)
    if kind == "release":
        from .release_build import build
        return build(context, payload)
    raise ValueError("Unknown job type.")


def once(store):
    layout.migrate(store)
    job = store.claim()
    if not job:
        return False
    context = Context(store, job)
    finished = threading.Event()

    def heartbeat():
        while not finished.wait(5):
            store.set("worker", {"heartbeat": time.time(), "job": job["id"]})

    pulse = threading.Thread(target=heartbeat, name="workspace-heartbeat", daemon=True)
    pulse.start()
    try:
        context.phase("Job started")
        result = execute(context) or {}
        if not result.get('github_release', {}).get('published') and not result.get('activation_committed'):
            context.check()
        store.change_job(job["id"], status="completed", phase="Completed", result=result, finished=now())
        context.log("Result: completed")
    except Cancelled as error:
        store.change_job(job["id"], status="cancelled", error=str(error), finished=now())
        context.log(str(error))
    except Exception as error:
        logging.exception("Workspace job %s failed", job["id"])
        store.change_job(job["id"], status="failed", error=scrub(str(error))[-6000:], finished=now())
        context.log("Error: " + str(error))
    finally:
        finished.set()
        pulse.join()
    return True


def main():
    parser = argparse.ArgumentParser(description="Lokaler R4OS Repository-/Buildworker")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--config", type=Path, default=Path("/etc/r4os-workspace.json"))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text()) if args.config.exists() else {}
    root = args.root or config.get("root")
    if not root:
        parser.error("--root or a worker configuration is required")
    store = Store(root, config.get("project_root"))
    lock = (store.state / "worker.lock").open("a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise SystemExit("A worker is already running.")
    for folder in ("Repositories", "BuildTools", "Artifacts", "Release"):
        (store.root / folder).mkdir(exist_ok=True)
    store.recover()
    layout.migrate(store)
    for connection in connections.list_connections(store):
        if connection["status"] == "deleting":
            connections.request_delete(store, connection["id"], True)
    catalog.reconcile_missing(store)
    logging.basicConfig(level=logging.INFO)
    if args.once:
        once(store)
        return
    while True:
        store.set("worker", {"heartbeat": time.time(), "job": None})
        if not store.setting("catalog"):
            store.enqueue("catalog")
        if not once(store):
            time.sleep(1)


if __name__ == "__main__":
    main()
