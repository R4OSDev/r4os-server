"""Pinned builds, verifiable GitHub releases, and a worker-refreshed UI cache."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote

from . import build_targets, builds, connections
from .github_api import Client, GitHubFailure
from .store import Conflict, identifier, under

CACHE_SECONDS = 600
MARKER = '<!-- r4os-server-build:'
PUBLISHED = 'The current version is already published.'


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS github_release_status(
        repository_name TEXT PRIMARY KEY REFERENCES repositories(name) ON DELETE CASCADE,
        signature TEXT NOT NULL, checked_at REAL NOT NULL, payload TEXT NOT NULL)''')


def build_contract(payload):
    return {key: payload.get(key, [] if key == 'additional_libraries' else {} if key == 'source_dependencies' else None)
            for key in ('sdk_profile','additional_libraries','source_dependencies','development_id')}


def signature(repo, connection, store=None):
    value = {'id': repo['id'], 'owner': repo['owner'], 'repo': repo['display_name'], 'revision': connection['revision'],
             'commit': repo['local'].get('commit'), 'branch': repo['local'].get('branch'),
             'default_branch': repo.get('default_branch', ''), 'dirty': repo['local'].get('dirty'),
             'targets': [(m['id'], m['version'], m.get('supported'), m.get('manifest')) for m in build_targets.targets(repo)]}
    if store is not None:
        from . import sdk_projects
        value['sdk'] = store.setting('active_sdk')
        value['options'] = sdk_projects.get(store, repo['name'])
    value['profiles'] = [m.get('sdk_profile') for m in build_targets.targets(repo)]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def state(status, reason, **fields):
    return dict(state=status, reason=reason, **fields)


def receipt_name(module):
    return 'r4os-build-'+identifier(module['id'])+'.json'


def release_url(repo, tag):
    return 'https://github.com/'+quote(repo['owner'], safe='')+'/'+quote(repo['display_name'], safe='')+'/releases/tag/'+quote(tag, safe='')


def marker(release):
    body = release.get('body') or ''
    if MARKER not in body: return {}
    try:
        value = json.loads(body.split(MARKER, 1)[1].split('-->', 1)[0])
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError): return {}


def verify_receipt(client, repo, module, commit, release, contract=None):
    assets = client.assets(release)
    names = [asset['name'] for asset in assets]
    if len(names) != len(set(names)):
        raise Conflict('The release contains ambiguous asset names.')
    by_name = {asset['name']: asset for asset in assets}
    asset = by_name.get(receipt_name(module))
    if not asset or asset.get('state') != 'uploaded':
        raise Conflict('This version has no complete build receipt for the selected target. Increase the manifest version.')
    try: receipt = json.loads(client.asset_bytes(asset))
    except (ValueError, UnicodeError): raise Conflict('The release build receipt is invalid.') from None
    if not isinstance(receipt, dict): raise Conflict('The release build receipt must be an object.')
    for key, expected in (('schema', 2), ('repository_id', repo['id']), ('target', module['id']), ('version', module['version']), ('commit', commit)):
        if receipt.get(key) != expected:
            raise Conflict('This release belongs to a different source or target. Increase the manifest version.')
    proof = receipt.get('sdk_profile', {})
    if (not proof.get('sdk_id') or not proof.get('profile_sources') or receipt.get('technical') is not False
            or (contract is not None and build_contract(receipt) != contract)):
        raise Conflict('This release has no matching verified SDK build. Increase the manifest version.')
    files = receipt.get('assets')
    if not isinstance(files, list) or not files:
        raise Conflict('The release build receipt has no artifact list.')
    required = set()
    for item in files:
        if not isinstance(item, dict): raise Conflict('Invalid release artifact entry.')
        name, digest, size = item.get('name'), item.get('sha256'), item.get('size')
        if (not isinstance(name, str) or not name or '/' in name or '\\' in name or name in required
                or name == receipt_name(module) or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                or type(size) is not int or not 0 < size <= 2*1024**3):
            raise Conflict('Invalid release artifact contract.')
        required.add(name)
        found = by_name.get(name)
        if not found or found.get('state') != 'uploaded' or int(found['size']) != size or client.asset_digest(found) != digest:
            raise Conflict('Release assets are missing or differ from their build receipt.')
    if module['kind'] in ('R4X', 'R4L', 'R4D', 'R4P') and module['id']+'.'+module['kind'] not in required:
        raise Conflict('The selected target artifact is missing from this release.')
    return receipt


def inspect_release(client, repo, module, commit, contract=None):
    tag = 'v'+module['version']
    tagged = client.tag_commit(tag)
    release = client.request('GET', '/releases/tags/'+quote(tag, safe=''), missing=True)
    if tagged is not None and tagged != commit:
        return state('conflict', tag+' is already bound to different source. Increase the manifest version.', tag=tag)
    if release is None:
        return state('ready', 'Build and publish '+tag+'.', tag=tag)
    if release.get('tag_name') != tag:
        raise GitHubFailure(502, 'GitHub returned a different release tag.')
    if release.get('draft'):
        owned = marker(release)
        if owned.get('schema') == 2 and owned.get('build_contract') == contract and all(owned.get(key) == expected for key, expected in (('repository_id', repo['id']), ('commit', commit), ('target', module['id']), ('version', module['version']))):
            return state('ready', 'Resume the unfinished release upload.', tag=tag)
        return state('conflict', 'This tag is reserved by another release draft. Increase the manifest version.', tag=tag)
    if tagged != commit:
        return state('conflict', 'The release tag cannot be verified. Increase the manifest version.', tag=tag)
    try: verify_receipt(client, repo, module, commit, release, contract)
    except Conflict as error: return state('conflict', str(error), tag=tag)
    return state('published', PUBLISHED, tag=tag, release_id=release['id'], release_url=release_url(repo, tag))


def persist(store, repo, connection, payload):
    key = signature(repo, connection, store)
    current = store.repository(repo['name'])
    latest = connections.get(store, current['connection_id'])
    if signature(current, latest, store) != key: return
    with store.connect() as db:
        db.execute('''INSERT INTO github_release_status VALUES(?,?,?,?) ON CONFLICT(repository_name)
                      DO UPDATE SET signature=excluded.signature,checked_at=excluded.checked_at,payload=excluded.payload''',
                   (repo['name'], key, time.time(), json.dumps(payload)))


def refresh(context, name, *, raise_network_errors=False):
    store = context.store
    repo = store.require_repository(name)
    connection = connections.get(store, repo['connection_id'])
    modules = build_targets.targets(repo)
    if not repo['local'].get('commit') or not connection['has_token'] or not modules:
        return {'targets': []}
    context.phase('Checking GitHub releases: '+repo['display_name'])
    client = Client(repo, connections.token(store, repo['connection_id']), context.check)
    result = {'targets': []}
    try:
        result['remote_commit'] = client.request('GET', '/commits/'+quote(repo['default_branch'], safe=''))['sha']
        for module in modules:
            context.check()
            if result['remote_commit'] != repo['local']['commit']:
                item = state('outdated', 'Update the repository to the current default branch before building.')
            elif not module['version_valid']:
                item = state('blocked', 'A versioned project manifest is required.')
            else:
                pinned = build_targets.pinned(store, repo, module, repo['local']['commit'])
                if pinned['version'] != module['version']:
                    item = state('blocked', 'Refresh repository metadata before building.')
                else:
                    _, selected = builds.inputs(store, {'name': name, 'target': pinned['id']})
                    item = inspect_release(client, repo, pinned, repo['local']['commit'], build_contract(selected))
            result['targets'].append(dict(item, id=module['id'], version=module['version']))
    except (GitHubFailure, ValueError, KeyError) as error:
        result['targets'] = [dict(state('unknown', str(error)), id=module['id'], version=module['version']) for module in modules]
        if raise_network_errors and isinstance(error, GitHubFailure):
            persist(store, repo, connection, result)
            raise
    persist(store, repo, connection, result)
    return result


def view(store, repo, *, schedule=False, force=False):
    connection = connections.get(store, repo['connection_id'])
    modules = build_targets.targets(repo)
    reason = ''
    if not repo['available']: reason = repo['unavailable_reason']
    elif not repo['local'].get('commit'): reason = 'Download the repository first.'
    elif repo['local'].get('dirty'): reason = 'Local changes must be committed or removed before building.'
    elif repo['local'].get('branch') != repo.get('default_branch'): reason = 'Use the repository default branch before building.'
    elif not modules: reason = 'This repository has no supported production build target.'
    elif not connection['has_token']: reason = 'Add a GitHub token with Contents write access to this connection.'
    key = signature(repo, connection, store)
    with store.connect() as db:
        row = db.execute('SELECT * FROM github_release_status WHERE repository_name=?', (repo['name'],)).fetchone()
        running = db.execute("SELECT id FROM jobs WHERE kind='build_publish' AND subject=? AND status IN ('queued','running') AND cancel=0", (repo['name'],)).fetchone()
        refreshing = db.execute("SELECT id FROM jobs WHERE kind='refresh' AND subject=? AND status IN ('queued','running')", (repo['name'],)).fetchone()
    data = json.loads(row['payload']) if row else {}
    complete = {item['id'] for item in data.get('targets', [])} == {item['id'] for item in modules}
    valid = row is not None and row['signature'] == key and time.time()-row['checked_at'] < CACHE_SECONDS and complete
    if not reason and (force or not valid) and schedule and not running and not refreshing:
        with store.connect() as db:
            pending = db.execute("SELECT id,payload FROM jobs WHERE kind='github_status' AND subject=? AND status IN ('queued','running') AND cancel=0", (repo['name'],)).fetchone()
        if pending and json.loads(pending['payload']).get('signature') != key:
            store.cancel_job(pending['id']);pending = None
        if not pending:
            store.enqueue('github_status', repo['name'], {'signature': key, 'connection_id': repo['connection_id']})
    cached = {item['id']: item for item in data.get('targets', [])} if valid and not force else {}
    tools_error = ''
    if not reason and modules:
        try: builds.active(store)
        except ValueError as error: tools_error = str(error)
    result = []
    for module in modules:
        item = cached.get(module['id'], state('checking', 'Checking GitHub release status…'))
        if reason: item = state('blocked', reason)
        elif not module['version_valid']: item = state('blocked', 'A valid manifest version is required.')
        elif module.get('supported') is False: item = state('blocked', module.get('reason', 'This build target is not supported.'))
        elif item['state'] == 'ready' and tools_error: item = state('blocked', tools_error)
        elif item['state'] == 'ready':
            try: builds.inputs(store, {'name': repo['name'], 'target': module['id']})
            except (ValueError, KeyError) as error: item = state('blocked', str(error))
        if running: item = state('running', 'Build and GitHub upload in progress.')
        result.append(dict(item, id=module['id'], name=module['name'], kind=module['kind'], version=module['version'],
                           enabled=item['state'] == 'ready'))
    if not result: summary = state('blocked', reason or 'No build target is available.')
    elif all(item['state'] == 'published' for item in result): summary = state('published', PUBLISHED)
    elif any(item['enabled'] for item in result): summary = state('ready', 'Build and upload the selected target to GitHub Releases.')
    else: summary = dict(result[0])
    return dict(summary, targets=result, enabled=any(item['enabled'] for item in result), checking=any(item['state'] == 'checking' for item in result),
                job_id=running['id'] if running else None)


def enqueue(store, value):
    if not isinstance(value, dict) or set(value) != {'name', 'target'}:
        raise ValueError('Choose a repository and build target.')
    repo, payload = builds.inputs(store, value)
    if payload.get('development_id'): raise Conflict('Technical development builds cannot be published as qualified releases.')
    selected = next((m for m in build_targets.targets(repo) if m['id'] == payload['target']), None)
    if selected is None: raise ValueError('Choose a production build target.')
    module = build_targets.pinned(store, repo, payload['module'], payload['commit'])
    connection = connections.get(store, repo['connection_id'])
    payload.update(module=module, github_release=True, connection_id=repo['connection_id'], connection_revision=connection['revision'],
                   repository_id=repo['id'], repository_owner=repo['owner'], repository_slug=repo['display_name'])
    payload['fingerprint'] = build_targets.fingerprint(payload)
    status = view(store, repo, schedule=True)
    current = next(item for item in status['targets'] if item['id'] == module['id'])
    if status['job_id']:
        existing = store.job(status['job_id'])
        inputs = {key:value for key,value in existing['payload'].items() if key != 'repository_label'}
        if existing['status'] in ('queued','running') and not existing['cancel'] and inputs == payload:
            return existing
    if not current['enabled']: raise Conflict(current['reason'])
    if module['version'] != current['version']: raise Conflict('The manifest version changed. Refresh before building.')
    return store.enqueue('build_publish', repo['name'], payload)


@contextmanager
def publication_lock(context, name):
    folder = context.store.state/'release-locks';folder.mkdir(exist_ok=True)
    with (folder/(identifier(name)+'.lock')).open('a') as lock:
        while True:
            context.check()
            try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB);break
            except BlockingIOError: time.sleep(.1)
        try: yield
        finally: fcntl.flock(lock, fcntl.LOCK_UN)


def reusable(store, name, payload):
    with store.connect() as db:
        rows = db.execute("SELECT result FROM jobs WHERE kind='build_publish' AND subject=? ORDER BY created DESC,rowid DESC LIMIT 30", (name,)).fetchall()
    for row in rows:
        result = json.loads(row['result'])
        if result.get('fingerprint') != payload['fingerprint'] or not result.get('local_build'): continue
        local = result['local_build']
        try:
            for file in local['files']:
                path = under(store.root, file['path'])
                if not path.is_file() or builds.digest(path) != file['sha256']: break
            else: return local
        except (OSError, ValueError, KeyError): pass
    return None


def upload_files(store, repo, module, payload, local):
    if local.get('technical') or local.get('sdk_profile') != payload.get('sdk_profile'):
        raise Conflict('The local build has no matching qualified SDK proof.')
    folder = under(store.root, local['output'])/'files'
    files = {path.name: path for path in sorted(folder.iterdir()) if path.is_file() and not path.is_symlink()}
    if not files: raise ValueError('No local target artifact is available for upload.')
    pending = store.state/'release-uploads'/payload['fingerprint'];pending.mkdir(parents=True, exist_ok=True)
    checksums = pending/(module['id']+'.sha256')
    checksums.write_text(''.join(builds.digest(path)+'  '+name+'\n' for name, path in files.items()))
    if checksums.name in files: raise ValueError('An artifact collides with the checksum filename.')
    files[checksums.name] = checksums
    receipt = {'schema': 2, 'repository_id': repo['id'], 'target': module['id'], 'version': module['version'],
               'commit': payload['commit'], 'fingerprint': payload['fingerprint'], 'toolset': payload['toolset'],
               'source_dependencies': payload['source_dependencies'], 'sdk_profile': payload['sdk_profile'],
               'additional_libraries': payload.get('additional_libraries', []), 'development_id': None, 'technical': False,
               'assets': [{'name': name, 'size': path.stat().st_size, 'sha256': builds.digest(path)} for name, path in files.items()]}
    path = pending/receipt_name(module)
    path.write_text(json.dumps(receipt, sort_keys=True, separators=(',', ':'))+'\n')
    if path.name in files: raise ValueError('An artifact collides with the build receipt filename.')
    files[path.name] = path
    return files


def run(context, name, payload):
    store = context.store
    def bound(check_cancel=True):
        if check_cancel: context.check()
        current = store.require_repository(name)
        connection = connections.get(store, current['connection_id'])
        if (current['id'], current['owner'], current['display_name'], current['connection_id'], connection['revision']) != (
                payload['repository_id'], payload['repository_owner'], payload['repository_slug'], payload['connection_id'], payload['connection_revision']):
            raise Conflict('The GitHub connection changed. Start a new build after refreshing it.')
        return current, connection
    with publication_lock(context, name):
        if not payload.get('sdk_profile') or payload.get('development_id'):
            raise Conflict('Start a new qualified SDK build.')
        repo, connection = bound()
        module = build_targets.pinned(store, repo, payload['module'], payload['commit'])
        client = Client(repo, connections.token(store, repo['connection_id']), bound)
        tag = 'v'+module['version']
        context.phase('Checking release '+tag)
        before = inspect_release(client, repo, module, payload['commit'], build_contract(payload))
        if before['state'] == 'conflict': raise Conflict(before['reason'])
        if before['state'] == 'published':
            return {'github_release': dict(before, published=True), 'files': []}
        local = reusable(store, name, payload)
        if local: context.phase('Reusing the verified local build for this upload')
        else: local = builds.build(context, name, payload)
        result = dict(local, local_build=local, fingerprint=payload['fingerprint'])
        store.change_job(context.job['id'], result=result)
        bound()
        files = upload_files(store, repo, module, payload, local)
        context.phase('Preparing GitHub release '+tag)
        current = inspect_release(client, repo, module, payload['commit'], build_contract(payload))
        if current['state'] == 'published':
            result['github_release'] = dict(current, published=True)
            store.change_job(context.job['id'], result=result)
            return result
        if current['state'] == 'conflict': raise Conflict(current['reason'])
        owned = {'schema': 2, 'build_contract': build_contract(payload), 'repository_id': repo['id'], 'target': module['id'], 'version': module['version'],
                 'commit': payload['commit'], 'fingerprint': payload['fingerprint']}
        release = client.request('GET', '/releases/tags/'+quote(tag, safe=''), missing=True)
        if release is None:
            try:
                release = client.request('POST', '/releases', {'tag_name': tag, 'target_commitish': payload['commit'], 'name': tag,
                          'draft': True, 'body': 'Built by R4OS Server.\n\n'+MARKER+json.dumps(owned, sort_keys=True)+' -->'})
            except GitHubFailure as error:
                if error.status not in (409, 422): raise
                release = client.request('GET', '/releases/tags/'+quote(tag, safe=''), missing=True)
                if release is None: raise
        if not release.get('draft') or marker(release) != owned:
            raise Conflict('This release is reserved by another build. Increase the manifest version.')
        result['github_release'] = {'tag': tag, 'release_id': release['id'], 'published': False}
        store.change_job(context.job['id'], result=result)
        assets = {asset['name']: asset for asset in client.assets(release)}
        for filename, path in files.items():
            bound()
            expected = builds.digest(path)
            asset = assets.get(filename)
            if asset and asset.get('state') == 'starter' and int(asset['size']) == 0:
                # Only our exact draft/input fingerprint permits removing a failed upload stub.
                client.request('DELETE', '/releases/assets/'+str(int(asset['id'])))
                asset = None
            if asset:
                if asset.get('state') != 'uploaded' or int(asset['size']) != path.stat().st_size or client.asset_digest(asset) != expected:
                    raise Conflict('An existing release asset differs. No artifact was overwritten.')
                continue
            context.phase('Uploading '+filename+' to GitHub')
            try: client.upload(release, filename, path)
            except GitHubFailure as error:
                if error.status != 422: raise
                asset = next((a for a in client.assets(release) if a['name'] == filename), None)
                if not asset or asset.get('state') != 'uploaded' or int(asset['size']) != path.stat().st_size or client.asset_digest(asset) != expected:
                    raise Conflict('Another upload occupies this asset name; nothing was overwritten.') from None
        context.phase('Verifying release assets')
        verify_receipt(client, repo, module, payload['commit'], release, build_contract(payload))
        tagged = client.tag_commit(tag)
        if tagged is not None and tagged != payload['commit']:
            raise Conflict('The release tag changed during upload. Nothing was published.')
        bound()
        final = client.request('PATCH', '/releases/'+str(int(release['id'])), {'draft': False})
        if final.get('draft') is not False: raise GitHubFailure(502, 'GitHub did not confirm release publication.')
        # Cancellation after this confirmed external action cannot undo a release.
        client.check = lambda: bound(False)
        confirmed = inspect_release(client, repo, module, payload['commit'], build_contract(payload))
        if confirmed['state'] != 'published': raise Conflict('GitHub publication could not be verified. Retry to inspect the existing release.')
        result['github_release'] = dict(confirmed, published=True)
        store.change_job(context.job['id'], result=result)
        _, current_inputs = builds.inputs(store, {'name': name, 'target': module['id']})
        if repo['local'].get('commit') == payload['commit'] and build_contract(current_inputs) == build_contract(payload):
            persist(store, repo, connection, {'remote_commit': payload['commit'], 'targets': [dict(confirmed, id=module['id'], version=module['version'])]})
        return result
