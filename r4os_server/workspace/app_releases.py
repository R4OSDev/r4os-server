"""Read published GitHub app releases; download them only through explicit jobs."""
import hashlib
import json
import re
import shutil
import time

from . import app_storage as storage, connections
from .github_api import Client
from .github_build import receipt_name, release_url, verify_receipt
from .process import Cancelled, scrub
from .store import Conflict, now

ACTIONS = ('refresh', 'download', 'update', 'remove_local')
KINDS = tuple('app_'+action for action in ACTIONS)


def fingerprint(value):
    content = {key:value[key] for key in ('repository_id','release_id','target','version','tag','commit','files')}
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def version(value):
    if not isinstance(value, str) or len(value) > 64 or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', value):
        raise ValueError('The app release has no valid manifest version.')
    return tuple(map(int, value.split('.')))


def bound(context):
    store, job = context.store, context.job
    context.check()
    repo = store.require_repository(job['subject'])
    connection = connections.get(store, repo['connection_id'])
    expected = job['payload']
    if (repo['id'] != expected['repository_id'] or connection['id'] != expected['connection_id']
            or connection['revision'] != expected['connection_revision']):
        raise Conflict('The app connection changed while the job was queued. Refresh the app again.')
    with store.connect() as db:
        storage.bind(db, repo, connection['revision'], job['id'])
    return repo, connection


def selection(client, repo, target):
    identity = client.request('GET', '')
    if not isinstance(identity, dict) or identity.get('id') != repo['id']:
        raise Conflict('The GitHub repository identity changed. Refresh its connection catalogue.')
    release = client.request('GET', '/releases/latest', missing=True)
    if release is None:
        return None
    if (not isinstance(release, dict) or release.get('draft') is not False or release.get('prerelease') is not False
            or not isinstance(release.get('tag_name'), str) or len(release['tag_name']) > 128):
        raise ValueError('GitHub did not return a published regular app release.')
    storage.number(release['id'])
    assets = client.assets(release)
    if len(assets) > 128 or len({a['name'].casefold() for a in assets}) != len(assets):
        raise ValueError('The app release has too many or ambiguous assets.')
    module = {'id': target, 'kind': 'R4X'}
    proof = next((a for a in assets if a['name'] == receipt_name(module) and a.get('state') == 'uploaded'), None)
    if proof is None:
        raise ValueError('The latest release has no qualified build receipt for this app.')
    raw = client.asset_bytes(proof)
    document = json.loads(raw)
    module['version'] = document.get('version') if isinstance(document, dict) else None
    version(module['version'])
    if release['tag_name'] != 'v'+module['version']:
        raise ValueError('The release tag and its app manifest version differ.')
    commit = client.tag_commit(release['tag_name'])
    if not isinstance(commit, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
        raise ValueError('The app release tag cannot be resolved to a source commit.')
    # A catalogue refresh reads metadata and the small receipt; artifact bytes are
    # hashed during the explicit download, even if GitHub omits its own digest.
    receipt = verify_receipt(client, repo, module, commit, release, asset_hashes=False)
    if receipt != document:
        raise Conflict('The release receipt changed during the check. Refresh again.')
    files = []
    for item in receipt['assets']:
        asset = next(a for a in assets if a['name'] == item['name'])
        storage.filename(item['name']);storage.number(asset['id'])
        files.append(dict(item, id=asset['id'], updated_at=asset.get('updated_at')))
    checksum = target+'.sha256'
    if not any(item['name'] == checksum for item in files):
        raise ValueError('The latest app release has no target SHA-256 file.')
    files.append({'id': proof['id'], 'name': storage.filename(proof['name']), 'size': len(raw),
                  'sha256': hashlib.sha256(raw).hexdigest(), 'updated_at': proof.get('updated_at')})
    if int(proof['size']) != len(raw):
        raise ValueError('The app build receipt is incomplete.')
    storage.number(proof['id'])
    return {'repository_id': repo['id'], 'release_id': release['id'], 'target': target,
            'version': module['version'], 'tag': release['tag_name'], 'commit': commit,
            'url': release_url(repo, release['tag_name']), 'published_at': release.get('published_at'),
            'files': sorted(files, key=lambda item: item['name'])}


def save_catalog(store, repo, revision, status, selected=None, error='', job_id=None):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        storage.bind(db, repo, revision, job_id)
        db.execute('INSERT INTO app_release_catalog VALUES(?,?,?,?,?,?) ON CONFLICT(repository_name) DO UPDATE SET '
                   'connection_revision=excluded.connection_revision,status=excluded.status,selection=excluded.selection,'
                   'error=excluded.error,checked_at=excluded.checked_at',
                   (repo['name'], revision, status, json.dumps(selected or {}), scrub(error), now()))


def refresh(context, repo):
    """Also called by repository refresh, without downloading or altering sources."""
    from .apps import existing
    app = existing(context.store, repo['name'])
    if app is None or not app['published']:
        return None
    connection = connections.get(context.store, repo['connection_id'])
    def check():
        context.check()
        current = connections.get(context.store, repo['connection_id'])
        if current['revision'] != connection['revision'] or current['status'] != 'ready':
            raise Conflict('The app connection changed during the release check.')
    context.phase('Checking the latest published app release')
    try:
        client = Client(repo, connections.token(context.store, repo['connection_id']), check)
        chosen = selection(client, repo, app['manifest_name'])
        local = storage.current(context.store, repo['name'], hashes=True)
        if local is not None and not local['valid']:
            context.log(local['error'])
        save_catalog(context.store, repo, connection['revision'], 'available' if chosen else 'missing', chosen,
                     job_id=context.job['id'])
        return chosen
    except Cancelled:
        raise
    except Exception as error:
        save_catalog(context.store, repo, connection['revision'], 'unknown', error=str(error), job_id=context.job['id'])
        raise


def view(store, repo):
    connection = connections.get(store, repo['connection_id'])
    with store.connect() as db:
        catalog = db.execute('SELECT * FROM app_release_catalog WHERE repository_name=?', (repo['name'],)).fetchone()
        count = db.execute('SELECT count(*) FROM app_release_refs WHERE repository_name=?', (repo['name'],)).fetchone()[0]
        row = db.execute("SELECT * FROM jobs WHERE subject=? AND (kind IN ('app_refresh','app_download','app_update','app_remove_local') "
                         "OR kind='refresh') ORDER BY created DESC,rowid DESC LIMIT 1", (repo['name'],)).fetchone()
    job = store.job_record(row) if row else None
    local = storage.current(store, repo['name'])
    selected = json.loads(catalog['selection']) if catalog else None
    status = catalog['status'] if catalog and catalog['connection_revision'] == connection['revision'] else 'unknown'
    error = catalog['error'] if catalog and status == 'unknown' else ''
    available = selected if status == 'available' else None
    state = 'not_downloaded' if local is None else 'local_available'
    can_download = bool(repo['available'] and available and local is None)
    can_update = False
    if local and not local['valid']:
        state, error = 'damaged', local['error']
    elif local and available:
        if local['release_id'] == available['release_id']:
            if fingerprint(local) == fingerprint(available):
                state = 'current'
            else:
                state, error = 'conflict', 'The published assets of this release changed. Remove local files before downloading again.'
        elif version(available['version']) > version(local['version']):
            state, can_update = 'outdated', bool(repo['available'])
        else:
            state = 'local_newer'
    if status == 'unknown' and not error:
        error = 'Refresh this app to check the latest published release.'
    reason = error or ('No published regular app release is available.' if status == 'missing' else
                       'The latest release is already available locally.' if state == 'current' else
                       'The local release is newer than or equal to the release selected by GitHub.' if state == 'local_newer' else '')
    busy = bool(job and job['status'] in ('queued', 'running'))
    return {'state': state, 'remote_status': status, 'available': available, 'local': local,
            'checked_at': catalog['checked_at'] if catalog else None, 'error': error, 'reason': reason,
            'can_download': can_download and not busy, 'can_update': can_update and not busy,
            'can_remove': count > 0 and not busy, 'local_release_count': count, 'job': job}


def request(store, name, action, *, db=None):
    from .apps import existing
    if action not in ACTIONS:
        raise ValueError('Unknown app action.')
    repo = store.repository(name) if action == 'remove_local' else store.require_repository(name)
    app = existing(store, name)
    if app is None or not app['published']:
        raise ValueError('Enable Show in Apps before starting an app action.')
    connection = connections.get(store, repo['connection_id'])
    if db is None:
        with store.connect() as transaction:
            transaction.execute('BEGIN IMMEDIATE')
            return request(store, name, action, db=transaction)
    storage.bind(db, repo, connection['revision'])
    active = db.execute("SELECT * FROM jobs WHERE subject=? AND kind IN ('app_refresh','app_download','app_update','app_remove_local') "
                        "AND status IN ('queued','running') ORDER BY created,rowid LIMIT 1", (name,)).fetchone()
    if active:
        job = store.job_record(active)
        if job['kind'] == 'app_'+action and not job['cancel'] and job['payload']['connection_revision'] == connection['revision']:
            return job
        raise Conflict('Another app action is already queued or running: '+job['id'])
    state = view(store, repo)
    if action in ('download','update','remove_local') and not state['can_'+('remove' if action == 'remove_local' else action)]:
        raise Conflict(state['reason'] or ('No local app files are available.' if action == 'remove_local' else 'Refresh the app and check its available release first.'))
    payload = {'connection_id': connection['id'], 'connection_revision': connection['revision'],
               'repository_id': repo['id'], 'app_label': app['display_name_override'] or app['manifest_name']}
    if action in ('download','update'):
        payload['selection'] = state['available']
    return store.enqueue('app_'+action, name, payload, connection=db)


def batch(store, action):
    if action not in ('refresh','download','update'):
        raise ValueError('Unsupported bulk app action.')
    from .apps import overview
    jobs, skipped = [], []
    for app in overview(store):
        if action != 'refresh' and not app['release']['can_'+action]:
            continue
        try:
            jobs.append(request(store, app['repository'], action))
        except (ValueError, Conflict) as error:
            skipped.append({'repository': app['repository'], 'error': str(error)})
    return {'jobs': jobs, 'skipped': skipped}


def run(context):
    store, action = context.store, context.job['kind'].removeprefix('app_')
    if action == 'remove_local':
        repo = store.repository(context.job['subject'])
        connection = connections.get(store, repo['connection_id'])
        expected = context.job['payload']
        if repo['id'] != expected['repository_id'] or connection['id'] != expected['connection_id'] or connection['revision'] != expected['connection_revision']:
            raise Conflict('The app connection changed. Retry removing local files.')
        context.phase('Removing local app releases')
        return storage.remove(store, {repo['name']}, context=context, repo=repo, revision=connection['revision'])
    repo, connection = bound(context)
    if action == 'refresh':
        from .catalog import refresh_repository
        return refresh_repository(context, repo['name'], context.job['payload'])
    selected = context.job['payload']['selection']
    client = Client(repo, connections.token(store, repo['connection_id']), lambda: bound(context))
    context.phase('Checking the selected app release before download')
    chosen = selection(client, repo, selected['target'])
    if chosen is None or fingerprint(chosen) != fingerprint(selected):
        save_catalog(store, repo, connection['revision'], 'available' if chosen else 'missing', chosen, job_id=context.job['id'])
        raise Conflict('The latest release changed while the app download was queued. Refresh and retry.')
    staged = storage.path(store, 'Apps/.staging/'+context.job['id'])
    try:
        destination = storage.path(store, storage.directory(repo['id'], selected['release_id']))
        if destination.exists():
            storage.verify(destination, selected)
            context.phase('Reusing this verified GitHub release from app storage')
            return storage.install(context, repo, connection['revision'], selected, destination)
        if staged.exists():
            shutil.rmtree(staged)
        staged.mkdir(parents=True)
        total, completed, last = sum(item['size'] for item in selected['files']), 0, 0
        for item in selected['files']:
            def progress(size):
                nonlocal last
                if time.monotonic()-last >= .3 or size == item['size']:
                    context.progress('downloading', 'Downloading '+item['name'], downloaded=completed+size, total=total,
                                     scope='App release '+selected['tag'])
                    last = time.monotonic()
            client.download(item, staged/item['name'], item['sha256'], progress)
            completed += item['size']
        checksum = (staged/(selected['target']+'.sha256')).read_text(encoding='utf-8')
        binary = next(item for item in selected['files'] if item['name'] == selected['target']+'.R4X')
        if not any(line.strip().split() == [binary['sha256'], binary['name']] for line in checksum.splitlines()):
            raise ValueError('The SHA-256 file does not match the app artifact and build receipt.')
        context.phase('Verifying the downloaded app release')
        latest = selection(client, repo, selected['target'])
        if latest is None or fingerprint(latest) != fingerprint(selected):
            raise Conflict('The app release changed during download. The previous local release has been retained.')
        return storage.install(context, repo, connection['revision'], selected, staged)
    finally:
        if staged.exists():
            shutil.rmtree(staged)
