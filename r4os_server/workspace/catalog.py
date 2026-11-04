"""Public R4OSDev inventory and conservative local Git operations."""
import json
from datetime import datetime
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

from .store import Conflict, identifier, now, under
from . import connections, layout


class GitHubError(RuntimeError):
    def __init__(self, status, detail, rate_limited=False):
        super().__init__(detail)
        self.status, self.rate_limited = status, rate_limited


class GitHubRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        if urlsplit(newurl).scheme != "https" or urlsplit(newurl).netloc != "api.github.com":
            raise RuntimeError("GitHub redirected the request outside its API.")
        return super().redirect_request(request, fp, code, message, headers, newurl)


def public_json(url, token=""):
    if urlsplit(url).scheme != "https" or urlsplit(url).netloc != "api.github.com":
        raise ValueError("Unexpected GitHub API address.")
    request = urllib.request.Request(url, headers={"User-Agent": "R4OS-Server", "Accept": "application/vnd.github+json"})
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.build_opener(GitHubRedirect()).open(request, timeout=30) as response:
            body = response.read(16 * 1024**2 + 1)
            if len(body) > 16 * 1024**2:
                raise ValueError("The GitHub response is too large.")
            return json.loads(body), response.headers
    except urllib.error.HTTPError as error:
        if error.code == 401:
            raise GitHubError(401, "GitHub rejected the token. Edit the connection to replace or remove it.") from None
        if error.code in (403, 429):
            reset = error.headers.get("X-RateLimit-Reset", "")
            limited = error.code == 429 or error.headers.get("X-RateLimit-Remaining") == "0" or bool(error.headers.get("Retry-After"))
            raise GitHubError(error.code, "GitHub denied this request or its rate limit has been reached. Check token permissions or retry later. Reset: " + reset, limited) from None
        if error.code == 404:
            raise GitHubError(404, "GitHub account not found or this token cannot access it. Check the connection address and account type.") from None
        raise GitHubError(error.code, "GitHub is currently unavailable (HTTP " + str(error.code) + ").") from None


def pages(context, endpoint, credential=""):
    for page in range(1, 101):
        context.check()
        items, headers = public_json(endpoint + "&per_page=100&sort=full_name&page=" + str(page), credential) if credential else public_json(endpoint + "&per_page=100&sort=full_name&page=" + str(page))
        if not isinstance(items, list):
            raise RuntimeError("Invalid repository list from GitHub.")
        yield from items
        if 'rel="next"' not in headers.get("Link", ""):
            return
    raise RuntimeError("Too many GitHub catalogue pages; keeping the last complete inventory.")


def refresh(context, connection_id="main", *, inspect_local=True):
    store, found = context.store, {}
    connection = connections.get(store, connection_id)
    if connection["status"] != "ready":
        raise ValueError("This connection is being deleted.")
    owner = connection["owner"]
    credential = connections.token(store, connection_id)
    context.phase("Fetching repositories: " + owner)
    kind = "orgs" if connection["account_type"] == "organisation" else "users"
    public_type = "public" if kind == "orgs" else "owner"
    streams = [(f"https://api.github.com/{kind}/{owner}/repos?type={public_type}", "", False)]
    if credential:
        endpoint = f"https://api.github.com/orgs/{owner}/repos?type=private" if kind == "orgs" else "https://api.github.com/user/repos?visibility=private&affiliation=owner,collaborator,organization_member"
        streams.append((endpoint, credential, True))
    for endpoint, auth, private in streams:
        items = pages(context, endpoint, auth)
        for item in items:
            if bool(item.get("private")) != private or item.get("owner", {}).get("login", "").lower() != owner.lower():
                continue
            name = identifier(item["name"])
            readable, access_error = True, ""
            if private:
                try:
                    branches, _ = public_json(f"https://api.github.com/repos/{owner}/{name}/branches?per_page=1", credential)
                    if not isinstance(branches, list):
                        raise RuntimeError("Invalid branch list from GitHub.")
                except GitHubError as error:
                    if error.status not in (403, 404) or error.rate_limited:
                        raise
                    readable, access_error = False, "The token cannot read this repository. Check its Contents permission and refresh the connection."
            found[int(item["id"])] = {"name": name, "id": int(item["id"]), "description": item.get("description") or "",
                          "display_name": name, "owner": owner,
                          "url": f"https://github.com/{owner}/{name}", "clone_url": f"https://github.com/{owner}/{name}.git",
                          "default_branch": item["default_branch"], "language": item.get("language"),
                          "archived": bool(item.get("archived")), "disabled": bool(item.get("disabled")),
                          "size_kib": item.get("size", 0), "pushed_at": item.get("pushed_at"), "fetched_at": now(),
                          "private": private, "visibility": "private" if private else "public", "read_access": readable,
                          "access_revision": connection["revision"], "access_error": access_error,
                          "permissions": {key: bool(value) for key, value in item.get("permissions", {}).items() if key in ("pull", "push", "admin")}}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        latest = connections.get(store, connection_id, db=db)
        if latest["revision"] != connection["revision"] or latest["status"] != "ready":
            raise ValueError("The connection changed during the catalogue refresh. Refresh it again.")
        db.execute("UPDATE repositories SET present=0 WHERE connection_id=?", (connection_id,))
        for item in found.values():
            prior = db.execute("SELECT name,directory,enabled,metadata FROM repositories WHERE connection_id=? AND github_id=?", (connection_id, item["id"])).fetchone()
            key = prior["name"] if prior else item["name"] if connection_id == "main" else connection_id + "." + str(item["id"])
            if not prior and db.execute("SELECT 1 FROM repositories WHERE name=?", (key,)).fetchone():
                key = connection_id + "." + str(item["id"])
            item["name"] = key
            directory = prior["directory"] if prior else layout.allocate(store, db, connection, item)
            enabled = prior["enabled"] if prior else int(connection_id == "main" and not item["private"])
            if prior and item["private"] and not json.loads(prior["metadata"]).get("private"):
                enabled = 0
            db.execute("""INSERT INTO repositories(name,github_id,metadata,present,connection_id,enabled,directory) VALUES(?,?,?,1,?,?,?)
                ON CONFLICT(name) DO UPDATE SET metadata=excluded.metadata,present=1,enabled=excluded.enabled""",
                       (key, item["id"], json.dumps(item, ensure_ascii=False), connection_id, enabled, directory))
        status = {"updated": now(), "time": time.time(), "error": "", "count": len(found)}
        db.execute("UPDATE connections SET catalog=? WHERE id=?", (json.dumps(status), connection_id))
    if connection_id == "main":
        store.set("catalog", status)
    for item in store.repositories(connection_id) if inspect_local else []:
        if item["available"] and (repository_path(store, item).exists() or item["local"].get("commit")):
            inspect(context, item["name"], remote=True)
        elif item["available"]:
            from .apps import refresh as refresh_app
            refresh_app(store, item)
    return {"count": len(found), "connection_id": connection_id}


def request_refresh_all(store):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        selected=[dict(row) for row in db.execute("SELECT id,revision FROM connections WHERE status='ready' ORDER BY id")]
        payload={'connections':selected}
        prior=db.execute("""SELECT * FROM jobs p WHERE p.kind='refresh_all' AND
            (p.status IN ('queued','running') OR EXISTS(SELECT 1 FROM jobs j
             WHERE json_extract(j.payload,'$.batch_id')=p.id AND j.status IN ('queued','running')))
            ORDER BY p.created DESC,p.rowid DESC LIMIT 1""").fetchone()
        if prior:
            job=store.job_record(prior)
            if job['payload']==payload and not job['cancel']: return job
            raise Conflict('A refresh batch is already active. Wait for it to finish.')
        return store.enqueue('refresh_all',payload=payload,connection=db)


def refresh_all(context):
    from .process import Cancelled, scrub
    store=context.store;result={'connections':[], 'jobs':[], 'skipped':[]}
    for selected in context.job['payload']['connections']:
        context.check()
        try:
            connection=connections.get(store,selected['id'])
            if connection['revision']!=selected['revision'] or connection['status']!='ready':
                raise Conflict('The connection changed while Refresh all was queued.')
            refresh(context,selected['id'],inspect_local=False)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                parent=db.execute('SELECT status,cancel FROM jobs WHERE id=?',(context.job['id'],)).fetchone()
                if not parent or parent['cancel']:
                    from .process import Cancelled
                    raise Cancelled('Refresh all was cancelled.')
                current=connections.get(store,selected['id'],db=db)
                if current['revision']!=selected['revision'] or current['status']!='ready':
                    raise Conflict('The connection changed during Refresh all.')
                rows=db.execute('SELECT name,metadata FROM repositories WHERE connection_id=? AND enabled=1 ORDER BY name',(selected['id'],)).fetchall()
                for row in rows:
                    metadata=json.loads(row['metadata'])
                    try:
                        child=store.enqueue('refresh',row['name'],{'connection_id':selected['id'],
                            'connection_revision':selected['revision'],'batch_id':context.job['id'],
                            'catalog_at':metadata.get('fetched_at')},connection=db)
                        result['jobs'].append({'repository':row['name'],'id':child['id']})
                    except Conflict as error:
                        result['skipped'].append({'repository':row['name'],'reason':str(error)})
                        context.log('Refresh skipped for '+row['name']+': '+str(error))
            result['connections'].append({'connection_id':selected['id'],'status':'completed','targets':len(rows)})
        except Exception as error:
            if isinstance(error,Cancelled):raise
            context.log('Catalogue refresh failed: '+str(error))
            detail=scrub(str(error))
            try:
                with store.connect() as db:
                    db.execute('BEGIN IMMEDIATE')
                    current=connections.get(store,selected['id'],db=db)
                    if current['revision']==selected['revision'] and current['status']=='ready':
                        db.execute('UPDATE connections SET catalog=? WHERE id=?',(json.dumps(dict(current['catalog'],error=detail)),selected['id']))
            except ValueError:pass
            result['connections'].append({'connection_id':selected['id'],'status':'failed','error':detail})
    context.phase('Repository refresh jobs queued: '+str(len(result['jobs'])))
    return result


def repository_path(store, repo):
    return layout.repository_path(store, repo)


def refresh_repository(context, name, expected):
    """Refresh one registered identity without discovering or updating its peers."""
    store = context.store
    repo = store.require_repository(name)
    connection = connections.get(store, repo['connection_id'])
    if (expected.get('connection_id', connection['id']) != connection['id']
            or expected.get('connection_revision', connection['revision']) != connection['revision']):
        raise Conflict('The connection changed while this refresh was queued. Refresh the repository again.')
    try:catalogue_age=time.time()-datetime.fromisoformat(repo.get('fetched_at','')).timestamp()
    except (TypeError,ValueError):catalogue_age=999999
    if expected.get('catalog_at') and expected.get('batch_id') and expected['catalog_at']==repo.get('fetched_at') and 0<=catalogue_age<300:
        context.phase('Using fresh catalogue metadata: '+repo['owner']+'/'+repo['display_name'])
        with store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            current=connections.get(store,connection['id'],db=db)
            if current['revision']!=connection['revision'] or current['status']!='ready':
                raise Conflict('The connection changed during the refresh.')
            db.execute('DELETE FROM github_release_status WHERE repository_name=?',(name,))
    else:
        refresh_repository_metadata(context,repo,connection)
    context.phase('Checking the selected repository source status')
    local = inspect(context, name, remote=True)
    if local['status'] == 'missing':
        from .apps import refresh as refresh_app
        refresh_app(store, store.require_repository(name))
    if local['status'] in ('unknown','conflict') and local.get('detail'):
        raise RuntimeError('Repository source status could not be refreshed: '+local['detail'])
    from .github_build import refresh as refresh_release
    release = refresh_release(context, name, raise_network_errors=True)
    from .app_releases import refresh as refresh_app_release
    app_release = refresh_app_release(context, store.require_repository(name))
    return {'repository':name, 'local':local, 'github_release_status':release, 'app_release':app_release}


def refresh_repository_metadata(context, repo, connection):
    store, name = context.store, repo['name']
    credential = connections.token(store, connection['id'])
    context.phase('Refreshing repository metadata: '+repo['owner']+'/'+repo['display_name'])
    endpoint = 'https://api.github.com/repos/'+quote(repo['owner'], safe='')+'/'+quote(repo['display_name'], safe='')
    try:
        item, _ = public_json(endpoint, credential)
    except GitHubError as error:
        if error.status == 404:
            raise ValueError('GitHub could not find this repository or the connection token no longer has access.') from None
        raise
    if (not isinstance(item, dict) or item.get('id') != repo['id']
            or item.get('owner', {}).get('login', '').casefold() != connection['owner'].casefold()):
        raise Conflict('The GitHub repository identity or owning account changed. Refresh its connection catalogue.')
    slug = identifier(item['name'])
    endpoint = 'https://api.github.com/repos/'+quote(connection['owner'], safe='')+'/'+quote(slug, safe='')
    readable, access_error = True, ''
    if item.get('private'):
        if not credential:
            readable, access_error = False, 'A connection token with Contents read access is required.'
        else:
            try:
                branches, _ = public_json(endpoint+'/branches?per_page=1', credential)
                if not isinstance(branches, list):
                    raise ValueError('GitHub returned an invalid repository branch list.')
            except GitHubError as error:
                if error.status not in (403,404) or error.rate_limited:
                    raise
                readable, access_error = False, 'The token cannot read this repository. Check its Contents permission and refresh the connection.'
    context.check()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        latest = connections.get(store, connection['id'], db=db)
        row = db.execute('SELECT * FROM repositories WHERE name=?', (name,)).fetchone()
        if (latest['revision'] != connection['revision'] or latest['status'] != 'ready' or row is None
                or row['connection_id'] != connection['id'] or row['github_id'] != repo['id'] or not row['enabled']):
            raise Conflict('The connection or repository selection changed during the refresh. Refresh again.')
        metadata = json.loads(row['metadata'])
        became_private = not metadata.get('private') and bool(item.get('private'))
        metadata.update(display_name=slug, owner=connection['owner'], description=item.get('description') or '',
                        url='https://github.com/'+connection['owner']+'/'+slug,
                        clone_url='https://github.com/'+connection['owner']+'/'+slug+'.git',
                        default_branch=item['default_branch'], language=item.get('language'),
                        archived=bool(item.get('archived')), disabled=bool(item.get('disabled')),
                        size_kib=item.get('size', 0), pushed_at=item.get('pushed_at'), fetched_at=now(),
                        private=bool(item.get('private')), visibility='private' if item.get('private') else 'public',
                        read_access=readable, access_revision=connection['revision'], access_error=access_error,
                        permissions={key:bool(value) for key,value in item.get('permissions', {}).items() if key in ('pull','push','admin')})
        db.execute('UPDATE repositories SET metadata=?,enabled=? WHERE name=?',
                   (json.dumps(metadata, ensure_ascii=False), 0 if became_private else row['enabled'], name))
        db.execute('DELETE FROM github_release_status WHERE repository_name=?', (name,))
    if became_private:
        raise Conflict('The repository is now private. Enable it explicitly in its connection settings.')
    if not readable:
        raise ValueError(access_error)


def reconcile_missing(store):
    for repo in store.repositories(enabled_only=False):
        if not repo["local"].get("commit"):
            continue
        try:
            missing = not repository_path(store, repo).exists()
        except ValueError:
            continue
        if missing:
            directory = layout.directory(store, repo)
            store.local(repo["name"], {"status": "missing", "directory": directory, "path": store.project_root + "/Repositories/" + directory})


def inspect(context, name, remote=True):
    store = context.store
    repo = store.require_repository(name)
    target = repository_path(store, repo)
    previous = repo["local"]
    directory = layout.directory(store, repo)
    local = {"status": "missing", "directory": directory, "path": store.project_root + "/Repositories/" + directory}
    if not target.exists():
        store.local(name, local)
        return local
    if target.is_symlink() or not (target / ".git").is_dir():
        local.update(status="conflict", detail="The destination is not a managed Git checkout.")
        store.local(name, local)
        return local
    origin, _ = context.git(target, "remote", "get-url", "origin")
    if origin.strip() != repo["clone_url"]:
        local.update(status="conflict", detail="The remote address differs from the connection catalogue.")
        store.local(name, local)
        return local
    head, code = context.git(target, "rev-parse", "--verify", "HEAD", check=False)
    if code:
        local.update(status="empty", detail="The repository has no commits yet.")
        store.local(name, local)
        return local
    branch, _ = context.git(target, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    dirty, _ = context.git(target, "status", "--porcelain=v1", "--untracked-files=normal")
    local.update(commit=head.strip(), branch=branch.strip(), dirty=bool(dirty.strip()), changes=dirty.splitlines()[:40],
                 checked_at=previous.get("checked_at"), remote_commit=previous.get("remote_commit"),
                 modules=previous.get("modules", []), profile=previous.get("profile", {}))
    if local["dirty"]:
        local.update(status="modified", detail="Local changes have been preserved.")
    elif local["branch"] != repo["default_branch"]:
        local.update(status="diverged", detail="The local branch differs from the default branch.")
    elif remote:
        try:
            remote_value, _ = context.repository_git(repo, "ls-remote", "--exit-code", repo["clone_url"], "refs/heads/" + repo["default_branch"], quiet=True, timeout=60)
            latest = remote_value.split()[0]
            local.update(remote_commit=latest, checked_at=now(), status="current")
            if latest != local["commit"]:
                context.repository_git(repo, "-C", target, "fetch", "--no-tags", "--no-recurse-submodules", "origin", repo["default_branch"], quiet=True, timeout=120)
                _, behind = context.git(target, "merge-base", "--is-ancestor", "HEAD", "FETCH_HEAD", check=False)
                _, ahead = context.git(target, "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD", check=False)
                local["status"] = "outdated" if behind == 0 else "ahead" if ahead == 0 else "diverged"
        except (OSError, RuntimeError) as error:
            local.update(status="unknown", detail=str(error))
    elif local["remote_commit"] == local["commit"]:
        local["status"] = "current"
    else:
        local["status"] = "unknown"
    try:
        from .profiles import scan, validate
        local.update(validate(context, target, scan(target, repo["display_name"])))
    except (OSError, ValueError) as error:
        local.update(profile={"ready": False, "reason": str(error)})
    store.local(name, local)
    from .apps import refresh as refresh_app
    refresh_app(store, repo)
    try:
        from .releases import import_defaults
        if repo["connection_id"] == "main" and all(module.get("validated") or "manifest" not in module for module in local.get("modules", [])):
            import_defaults(store, name, local, target)
    except ImportError:
        pass
    return local


def download(context, name):
    store, repo = context.store, context.store.require_repository(name)
    if not repo["listed"] or repo.get("disabled"):
        raise ValueError("The repository is no longer available to this connection.")
    target = repository_path(store, repo)
    if target.exists():
        return inspect(context, name)
    context.phase("Downloading repository: " + name)
    layout.ensure_folder(store, connections.get(store, repo["connection_id"]))
    scratch = target.parent / (".staging-" + identifier(context.job["id"]) + "-" + identifier(name))
    scratch.parent.mkdir(parents=True, exist_ok=True)
    if scratch.exists():
        shutil.rmtree(scratch)
    try:
        context.repository_git(repo, "clone", "--no-hardlinks", "--", repo["clone_url"], scratch, timeout=1800)
        context.git(scratch, "remote", "set-url", "--push", "origin", "disabled://local-only")
        scratch.rename(target)
    finally:
        if scratch.exists():
            shutil.rmtree(scratch)
    return inspect(context, name)


def update(context, name):
    local = inspect(context, name)
    if local["status"] == "current":
        return local
    if local["status"] != "outdated":
        raise ValueError(local.get("detail") or "The checkout is not ready for a fast-forward update: " + local["status"])
    context.phase("Updating repository: " + name)
    context.git(repository_path(context.store, context.store.repository(name)), "merge", "--ff-only", "--no-edit", "FETCH_HEAD")
    return inspect(context, name)


def update_selected(context, targets, connection_id=None):
    if not isinstance(targets, list) or not targets:
        raise ValueError("This update job has no saved targets. Use Refresh and start Update all again.")
    for target in targets:
        if (not isinstance(target, dict) or not isinstance(target.get("name"), str)
                or not isinstance(target.get("connection_id"), str) or not isinstance(target.get("github_id"), int)
                or (connection_id and target["connection_id"] != connection_id)):
            raise ValueError("Invalid saved update targets. Use Refresh and start Update all again.")
    results = []
    for index, target in enumerate(targets, 1):
        context.check()
        name = target["name"]
        context.phase(f"Update target {index}/{len(targets)}: {name}")
        try:
            try:
                repo = context.store.repository(name)
            except ValueError:
                results.append({"name": name, "status": "skipped", "detail": "The repository has been removed."})
                continue
            if (repo["connection_id"] != target["connection_id"] or repo["id"] != target["github_id"]
                    or not repo["enabled"] or not repo["available"]
                    or connections.get(context.store, repo["connection_id"])["status"] != "ready"):
                results.append({"name": name, "status": "skipped", "detail": "The repository is no longer available for this update job."})
                continue
            if not repository_path(context.store, repo).exists():
                inspect(context, name)
                results.append({"name": name, "status": "skipped", "detail": "The local checkout no longer exists."})
                continue
            value = update(context, name)
            unchanged = value["status"] == "current" and value.get("commit") == repo["local"].get("commit")
            result = {"name": name, "status": "skipped" if unchanged else value["status"]}
            if unchanged:
                result["detail"] = "Already up to date."
            results.append(result)
        except (OSError, ValueError, RuntimeError) as error:
            context.log(name + ": " + str(error))
            results.append({"name": name, "status": "failed", "error": str(error)})
    return {"repositories": results, "target_count": len(targets), "failed": sum(item["status"] == "failed" for item in results)}


def batch(context, action, connection_id=None, *, targets=None):
    if action == "update":
        return update_selected(context, targets, connection_id)
    results = []
    for repo in context.store.repositories(connection_id):
        context.check()
        name = repo["name"]
        try:
            present = repository_path(context.store, repo).exists()
            if present or not repo["available"]:
                results.append({"name": name, "status": "skipped"})
                continue
            value = download(context, name)
            results.append({"name": name, "status": value["status"]})
        except (OSError, ValueError, RuntimeError) as error:
            context.log(name + ": " + str(error))
            results.append({"name": name, "status": "failed", "error": str(error)})
    return {"repositories": results, "failed": sum(item["status"] == "failed" for item in results)}
