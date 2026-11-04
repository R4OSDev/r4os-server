"""Pinned builds, verifiable GitHub releases, and a worker-refreshed UI cache."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import quote

from . import artifact_source, build_targets, builds, connections
from .github_api import Client, GitHubFailure
from .store import Conflict, identifier, under

CACHE_SECONDS = 600
MARKER = '<!-- r4os-server-build:'
PUBLISHED = 'The current GitHub revision already has a verified release artifact.'
_UNSET = object()


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS github_release_status(
        repository_name TEXT PRIMARY KEY REFERENCES repositories(name) ON DELETE CASCADE,
        signature TEXT NOT NULL, checked_at REAL NOT NULL, payload TEXT NOT NULL)''')


def build_contract(payload):
    return {key: payload.get(key, [] if key == 'additional_libraries' else {} if key == 'source_dependencies' else None)
            for key in ('sdk_profile','additional_libraries','source_dependencies','development_id')}


def signature(repo, connection, store=None):
    value = {'id': repo['id'], 'owner': repo['owner'], 'repo': repo['display_name'], 'revision': connection['revision'],
             'available':repo.get('available'), 'private':repo.get('private'), 'listed':repo.get('listed'),
             'format':2, 'commit': repo['local'].get('commit'), 'remote_commit':repo['local'].get('remote_commit'), 'branch': repo['local'].get('branch'),
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
    if (not isinstance(proof,dict) or not proof.get('sdk_id') or not proof.get('profile_sources') or receipt.get('technical') is not False
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


def inspect_release(client, repo, module, commit, contract=None, *, tagged=_UNSET):
    tag = 'v'+module['version']
    if tagged is _UNSET:tagged = client.tag_commit(tag)
    if tagged is not None and tagged != commit:
        return state('conflict', tag+' is already bound to different source. Increase the manifest version.', tag=tag)
    release = client.request('GET', '/releases/tags/'+quote(tag, safe=''), missing=True)
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
    if signature(current, latest, store) != key: return False
    with store.connect() as db:
        db.execute('''INSERT INTO github_release_status VALUES(?,?,?,?) ON CONFLICT(repository_name)
                      DO UPDATE SET signature=excluded.signature,checked_at=excluded.checked_at,payload=excluded.payload''',
                   (repo['name'], key, time.time(), json.dumps(payload)))
    return True


def artifact_summary(items):
    if not items:return 'not_applicable','No production artifact target exists at the GitHub revision.'
    if any(item['artifact_state']=='outdated' for item in items):
        return 'outdated','At least one production target has no current GitHub release artifact.'
    if all(item['artifact_state']=='current' for item in items):return 'current',PUBLISHED
    return 'unknown','The GitHub artifact status could not be verified for every target.'


def refresh(context, name, *, raise_network_errors=False):
    store = context.store
    repo = store.require_repository(name)
    connection = connections.get(store, repo['connection_id'])
    modules = build_targets.targets(repo)
    result = {'format':2, 'targets':[], 'artifacts':[], 'artifact_state':'unknown'}
    network_error = None
    context.phase('Checking GitHub release artifacts: '+repo['display_name'])
    try:
        client = Client(repo, connections.token(store, repo['connection_id']), context.check)
        commit = artifact_source.head(context,repo,client)
        result['remote_commit'] = commit
        remote_modules = artifact_source.targets(context,repo,client,commit)
        for module in remote_modules:
            context.check()
            if not module['version_valid']:
                item = state('blocked',module.get('reason','A versioned manifest is required.'))
            else:
                contract = None
                if commit == repo['local'].get('commit') and any(value['id']==module['id'] for value in modules):
                    try:
                        _,selected = builds.inputs(store,{'name':name,'target':module['id']})
                        contract = build_contract(selected)
                    except (ValueError,KeyError):pass  # Artifact reading does not require installed build tools.
                if not connection['has_token'] and artifact_source.canonical(repo):
                    tagged = artifact_source.tag(context,repo,module['version'])
                    if tagged is None:item=state('ready','No current release artifact is published.',tag='v'+module['version'])
                    else:item=inspect_release(client,repo,module,commit,contract,tagged=tagged)
                else:item = inspect_release(client,repo,module,commit,contract)
            currency = 'current' if item['state']=='published' else 'outdated' if item['state'] in ('ready','conflict') else 'unknown'
            result['artifacts'].append(dict(item,artifact_state=currency,id=module['id'],version=module['version'],name=module['name'],kind=module['kind']))
        result['artifact_state'],result['artifact_reason'] = artifact_summary(result['artifacts'])
        remote = {item['id']:item for item in result['artifacts']}
        for module in modules:
            item = remote.get(module['id'],state('blocked','This target is not present in the current GitHub source. Refresh repository metadata.'))
            if commit != repo['local'].get('commit'):
                item = state('outdated','The local repository differs from GitHub. Update it before building.')
            elif item.get('version') != module['version']:
                item = state('blocked','Refresh repository metadata before building.')
            result['targets'].append(dict(item,id=module['id'],version=module['version']))
    except artifact_source.EmptySource as error:
        result.update(artifact_state='not_applicable',artifact_reason=str(error))
        result['targets']=[dict(state('blocked',str(error)),id=module['id'],version=module['version']) for module in modules]
    except (GitHubFailure,OSError,RuntimeError,ValueError,KeyError,TypeError) as error:
        detail=str(error)
        result.update(artifact_state='unknown',artifact_reason=detail)
        result['targets']=[dict(state('unknown',detail),id=module['id'],version=module['version']) for module in modules]
        if isinstance(error,(GitHubFailure,OSError,RuntimeError)) and not isinstance(error,Conflict):network_error=error
    if not persist(store,repo,connection,result):
        raise Conflict('Repository or SDK settings changed during the artifact check. Refresh again.')
    if raise_network_errors and network_error is not None:raise network_error
    return result


def view(store, repo, *, schedule=False, force=False):
    connection = connections.get(store, repo['connection_id'])
    modules = build_targets.targets(repo)
    reason = ''
    if not repo['available']: reason = repo['unavailable_reason']
    elif not repo['local'].get('commit'): reason = 'Download the repository first.'
    elif repo['local'].get('dirty'): reason = 'Local changes must be committed or removed before building.'
    elif repo['local'].get('branch') != repo.get('default_branch'): reason = 'Use the repository default branch before building.'
    elif repo['local'].get('status') in ('outdated','ahead','diverged','unknown','conflict','empty'): reason = 'Refresh and update the local repository to match GitHub before building.'
    elif not modules: reason = 'This repository has no supported production build target.'
    elif not connection['has_token']: reason = 'Add a GitHub token with Contents write access to this connection.'
    elif repo.get('permissions',{}).get('push') is False: reason = 'This connection cannot write GitHub releases for the repository.'
    key = signature(repo, connection, store)
    with store.connect() as db:
        row = db.execute('SELECT * FROM github_release_status WHERE repository_name=?', (repo['name'],)).fetchone()
        activity=db.execute("SELECT * FROM jobs WHERE subject=? AND kind IN ('build_publish','refresh','github_status') AND status IN ('queued','running') AND (kind='refresh' OR cancel=0)",(repo['name'],)).fetchall()
    running=next((job for job in activity if job['kind']=='build_publish'),None)
    refreshing=next((job for job in activity if job['kind']=='refresh'),None)
    pending=next((job for job in activity if job['kind']=='github_status'),None)
    checking=bool(refreshing or pending)
    data = json.loads(row['payload']) if row else {}
    complete = {item['id'] for item in data.get('targets', [])} == {item['id'] for item in modules}
    valid = row is not None and data.get('format')==2 and row['signature'] == key and time.time()-row['checked_at'] < CACHE_SECONDS and complete
    eligible=repo['available'] and bool(modules or data.get('artifacts'))
    if eligible and (connection['has_token'] or row is not None) and (force or not valid) and schedule and not running and not refreshing:
        if pending and json.loads(pending['payload']).get('signature') != key:
            store.cancel_job(pending['id']);pending = None
        if not pending:
            store.enqueue('github_status', repo['name'], {'signature': key, 'connection_id': repo['connection_id']})
        checking=True
    cached = {item['id']: item for item in data.get('targets', [])} if valid and not force else {}
    tools_error = ''
    if not reason and modules:
        try: builds.active(store)
        except ValueError as error: tools_error = str(error)
    result = []
    for module in modules:
        item = cached.get(module['id'], state('checking', 'Checking GitHub release status…'))
        if item['state']=='published':pass
        elif reason: item = state('blocked', reason)
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
    currency=data.get('artifact_state','unknown') if valid and not force else 'checking' if checking else 'unknown'
    artifact_reason=data.get('artifact_reason','') if valid and not force else 'Refresh to check GitHub release artifacts.'
    if not result and currency=='current':summary=state('published',PUBLISHED)
    return dict(summary, targets=result, enabled=any(item['enabled'] for item in result), checking=any(item['state'] == 'checking' for item in result),
                artifact_state=currency,artifact_reason=artifact_reason,artifacts=data.get('artifacts',[]) if valid else [],
                remote_commit=data.get('remote_commit') if valid else None,
                source_current=data['remote_commit']==repo['local'].get('commit') if valid and data.get('remote_commit') else None,
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
        if artifact_source.head(context,repo,client)!=payload['commit']:
            with store.connect() as db:db.execute('DELETE FROM github_release_status WHERE repository_name=?',(name,))
            raise Conflict('GitHub source changed while this build was queued. Refresh and update the repository.')
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
        if artifact_source.head(context,repo,client)!=payload['commit']:
            with store.connect() as db:db.execute('DELETE FROM github_release_status WHERE repository_name=?',(name,))
            raise Conflict('GitHub source changed during this build. The release draft was retained; nothing was published.')
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
            item=dict(confirmed,id=module['id'],version=module['version'],artifact_state='current',name=module['name'],kind=module['kind'])
            persist(store, repo, connection, {'format':2,'remote_commit': payload['commit'], 'targets':[item], 'artifacts':[item], 'artifact_state':'current','artifact_reason':PUBLISHED})
        return result
