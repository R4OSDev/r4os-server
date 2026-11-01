"""Private-only workspace routes, called after the WebApp's Host/Origin checks."""
import json
import mimetypes
from pathlib import Path
import re
import time
from urllib.parse import parse_qs, unquote, urlsplit

from ..common.http import HTTPError
from . import releases, testconfigs, connections, repo_metadata
from .store import ACTIVE, identifier, under, update_candidate


def data(handler):
    value = json.loads(handler.body())
    if not isinstance(value, dict):
        raise ValueError("A JSON object is required.")
    return value


def file_response(handler, path, name):
    if not path.is_file():
        raise HTTPError(404, "output_missing", "The local output file is missing.")
    size, start, end = path.stat().st_size, 0, path.stat().st_size - 1
    status = 200
    requested = handler.headers.get("Range")
    if requested:
        match = re.fullmatch(r"bytes=(\d+)-(\d*)", requested)
        if not match:
            raise HTTPError(416, "invalid_range")
        start = int(match[1])
        end = min(int(match[2]) if match[2] else end, end)
        if start > end or start >= size:
            raise HTTPError(416, "invalid_range")
        status = 206
    handler.send_response(status)
    handler.send_header("Content-Type", mimetypes.guess_type(name)[0] or "application/octet-stream")
    handler.send_header("Content-Length", str(max(0, end - start + 1)))
    handler.send_header("Content-Disposition", 'attachment; filename="' + re.sub(r'[^A-Za-z0-9_.-]', '_', Path(name).name) + '"')
    handler.send_header("Accept-Ranges", "bytes")
    if status == 206:
        handler.send_header("Content-Range", f"bytes {start}-{end}/{size}")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    if handler.command != "HEAD":
        with path.open("rb") as source:
            source.seek(start)
            remaining = end - start + 1
            while remaining:
                chunk = source.read(min(remaining, 1024 * 1024))
                if not chunk:
                    break
                handler.wfile.write(chunk)
                remaining -= len(chunk)


def dispatch(handler):
    store = handler.server.workspace
    url = urlsplit(handler.path)
    parts = [unquote(value) for value in url.path.removeprefix("/api/repositories/").split("/") if value]
    method = handler.command
    read = method in ("GET", "HEAD")
    try:
        if parts == ["connections"]:
            if read:
                return handler.json(200, {"connections": connections.list_connections(store)})
            if method == "POST":
                saved = connections.save(store, data(handler))
                store.enqueue("catalog", payload={"connection_id": saved["id"], "revision": saved["revision"]})
                return handler.json(201, saved)
        if len(parts) >= 2 and parts[0] == "connections":
            key = parts[1]
            connection = connections.get(store, key)
            if len(parts) == 2:
                if read:
                    return handler.json(200, {"connection": connection, "repositories": store.repositories(key, enabled_only=False)})
                if method == "PUT":
                    saved = connections.save(store, data(handler), key)
                    store.enqueue("catalog", payload={"connection_id": key, "revision": saved["revision"]})
                    return handler.json(200, saved)
                if method == "DELETE":
                    return handler.json(202, connections.request_delete(store, key, data(handler).get("confirmed")))
            if parts[2:] == ["selection"] and method == "PUT":
                return handler.json(200, connections.select(store, key, data(handler)))
            if parts[2:] == ["delete-preview"] and read:
                return handler.json(200, connections.preview_delete(store, key))
        if parts == ["state"] and read:
            worker = store.setting("worker", {})
            repositories = store.repositories()
            return handler.json(200, {"repositories": repositories, "catalog": store.setting("catalog", {}),
                                      "categories": repo_metadata.CATEGORIES, "update_count": sum(bool(update_candidate(repo)) for repo in repositories),
                                      "worker": dict(worker, ready=time.time() - worker.get("heartbeat", 0) < 20),
                                      "jobs": store.jobs(), "connections": connections.list_connections(store), "project_root": store.project_root})
        if len(parts) == 3 and parts[0] == "items" and parts[2] == "metadata" and method == "PUT":
            return handler.json(200, repo_metadata.save(store, parts[1], data(handler)))
        if len(parts) == 2 and parts[0] == "items" and read:
            repo = store.repository(parts[1])
            tools = None
            tools_error = ""
            if store.setting("active_toolset"):
                from .toolsets import active
                try:
                    _, tools = active(store)
                except ValueError as error:
                    tools_error = str(error)
            return handler.json(200, {"repository": repo, "jobs": store.jobs(parts[1]), "toolset": tools["id"] if tools else None, "build_tools": tools, "tools_error": tools_error,
                                      "last_success": store.last_success(parts[1], "build")})
        if len(parts) == 2 and parts[0] == "actions" and method == "POST":
            kind = parts[1]
            if kind not in ("catalog", "download", "update", "download_all", "update_all", "check"):
                raise ValueError("Unknown repository action.")
            value = data(handler)
            name = value.get("name", "")
            if name:
                store.require_repository(name)
            if kind in ("download", "update") and not name:
                raise ValueError("A repository is required.")
            scope = value.get("connection_id")
            if scope:
                if connections.get(store, scope)["status"] != "ready":
                    raise ValueError("This connection is being deleted.")
            return handler.json(202, store.enqueue(kind, name, {"connection_id": scope} if scope else {}))
        if parts == ["jobs"] and read:
            query = parse_qs(url.query)
            category = query.get("category", ["all"])[0]
            subject = query.get("subject", [None])[0]
            limit = min(100, max(1, int(query.get("limit", ["50"])[0])))
            offset = max(0, int(query.get("offset", ["0"])[0]))
            return handler.json(200, {"jobs": store.jobs(subject, limit, category=category, offset=offset),
                                      "total": store.job_count(subject, category=category), "offset": offset, "limit": limit,
                                      "last_success": store.last_success(subject, "release") if subject and category == "releases" else None})
        if len(parts) >= 2 and parts[0] == "jobs":
            job = store.job(parts[1])
            if len(parts) == 2 and read:
                return handler.json(200, job)
            if parts[2:] == ["cancel"] and method == "POST":
                return handler.json(200, store.cancel_job(job["id"]))
            if parts[2:] == ["log"] and read:
                path = store.state / "logs" / (job["id"] + ".log")
                offset = max(0, int(parse_qs(url.query).get("offset", ["0"])[0]))
                text = ""
                if path.is_file():
                    with path.open("rb") as source:
                        source.seek(offset)
                        chunk = source.read(128 * 1024)
                        text, offset = chunk.decode("utf-8", "replace"), source.tell()
                return handler.json(200, {"text": text, "offset": offset, "status": job["status"]})
            if len(parts) == 4 and parts[2] == "files" and read:
                matches = [item for item in job["result"].get("files", []) if item["id"] == parts[3]]
                if not matches or job["status"] != "completed":
                    raise HTTPError(404, "output_not_found")
                item = matches[0]
                if item["path"].split("/", 1)[0] not in ("Artifacts", "Release"):
                    raise HTTPError(404, "output_not_found")
                return file_response(handler, under(store.root, item["path"]), item["name"])
        if parts == ["tools"] and read:
            from .toolsets import overview
            return handler.json(200, overview(store))
        if len(parts) == 3 and parts[:2] == ["tools", "actions"] and method == "POST":
            if parts[2] not in ("check", "install", "update", "activate"):
                raise ValueError("Unknown tool action.")
            value = data(handler)
            return handler.json(202, store.enqueue("tools_" + parts[2], "tools", value))
        if parts == ["build"] and method == "POST":
            from .builds import enqueue
            return handler.json(202, enqueue(store, data(handler)))
        if parts == ["releases"] and read:
            return handler.json(200, {"releases": releases.list_types(store), "repositories": store.repositories(),
                                      "test_configurations": testconfigs.list_sets(store)})
        if parts == ["releases"] and method == "POST":
            return handler.json(201, releases.save_type(store, data(handler)))
        if len(parts) == 2 and parts[0] == "releases":
            if read:
                return handler.json(200, dict(releases.preview(store, parts[1]), jobs=store.jobs("release:" + parts[1]),
                                             last_success=store.last_success("release:" + parts[1], "release")))
            if method == "PUT":
                return handler.json(200, releases.save_type(store, data(handler), parts[1]))
            if method == "DELETE":
                return handler.json(200, releases.delete_type(store, parts[1], data(handler).get("revision")))
        if parts == ["test-configurations"] and read:
            return handler.json(200, {"test_configurations": testconfigs.list_sets(store), "repositories": store.repositories()})
        if parts == ["test-configurations"] and method == "POST":
            return handler.json(201, testconfigs.save(store, data(handler)))
        if len(parts) == 2 and parts[0] == "test-configurations":
            if read:
                return handler.json(200, testconfigs.get(store, parts[1]))
            if method == "PUT":
                return handler.json(200, testconfigs.save(store, data(handler), parts[1]))
            if method == "DELETE":
                return handler.json(200, testconfigs.delete(store, parts[1], data(handler).get("revision")))
        if len(parts) == 3 and parts[0] == "releases" and parts[2] == "build" and method == "POST":
            from .release_build import enqueue
            return handler.json(202, enqueue(store, parts[1], data(handler)))
        raise HTTPError(404, "not_found")
    except connections.CredentialError as error:
        raise HTTPError(500, "credential_storage_error", str(error)) from None
    except releases.Conflict as error:
        raise HTTPError(409, "configuration_changed", str(error)) from None
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPError(422, "validation_error", str(error)) from None
