"""Server-owned application settings; publishing only lists an app in the WebApp."""
import base64
import binascii
import json
import re
from urllib.parse import quote

from . import connections, layout
from .store import Conflict, identifier, now, under


class SourceUnavailable(Exception):
    pass


def initialize(db):
    db.execute("""CREATE TABLE IF NOT EXISTS apps(
        repository_name TEXT PRIMARY KEY REFERENCES repositories(name) ON DELETE CASCADE,
        published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
        manifest_name TEXT NOT NULL, manifest_version TEXT NOT NULL,
        display_name_override TEXT, install_path_override TEXT,
        description TEXT NOT NULL DEFAULT '', source_error TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL, updated TEXT NOT NULL)""")


def install_path(value):
    if not isinstance(value, str):
        raise ValueError('Install-Path must be an absolute Windows directory.')
    value = value.strip().replace('/', '\\')
    if len(value) > 240 or not re.match(r'^[A-Za-z]:\\', value):
        raise ValueError('Install-Path must be an absolute drive path of at most 240 characters, such as C:\\Software\\MyApp.')
    parts = value[3:].split('\\')
    reserved = r'(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?'
    if any(not part or part in ('.', '..') or part.rstrip(' .') != part
           or re.search(r'[<>:"|?*\x00-\x1f\x7f]', part) or re.fullmatch(reserved, part) for part in parts):
        raise ValueError('Install-Path contains an invalid directory name.')
    return value[0].upper()+value[1:]


def manifest(text):
    if len(text.encode('utf-8')) > 65536:
        raise ValueError('The root application manifest is too large.')
    fields = {}
    for line in text.lstrip('\ufeff').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            fields.setdefault(key, []).append(value.strip())
    def single(key):
        values = fields.get(key, [])
        if len(values) != 1:
            raise ValueError('The root module.R4MF requires one '+key+' entry.')
        return values[0]
    if single('R4OS_MODULE_MANIFEST') != '2' or single('KIND') != 'R4X':
        raise ValueError('Show in Apps requires a root module.R4MF for an R4X application (manifest version 2).')
    name, version = single('NAME'), single('VERSION')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,31}', name):
        raise ValueError('The application manifest NAME is invalid.')
    if len(version) > 64 or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', version):
        raise ValueError('The application manifest VERSION is invalid.')
    install_path('C:\\Software\\'+name)
    return name, version


def source_manifest(store, repo):
    root = layout.repository_path(store, repo)
    if root.exists():
        path = under(root, 'module.R4MF')
        if not path.is_file():
            raise ValueError('A root module.R4MF is required. Update the repository if the manifest was recently added.')
        with path.open('rb') as stream:
            raw = stream.read(65537)
    else:
        from .catalog import public_json, GitHubError
        owner, name = repo['owner'], repo['display_name']
        url = ('https://api.github.com/repos/'+quote(owner, safe='')+'/'+quote(name, safe='')
               +'/contents/module.R4MF?ref='+quote(repo['default_branch'], safe=''))
        try:
            document, _ = public_json(url, connections.token(store, repo['connection_id']))
        except GitHubError as error:
            if error.status == 404:
                raise ValueError('The root module.R4MF was not found or this connection cannot read it.') from None
            raise SourceUnavailable(str(error)) from None
        except (OSError, RuntimeError):
            raise SourceUnavailable('GitHub could not be reached. Please try again.') from None
        if (not isinstance(document, dict) or document.get('encoding') != 'base64'
                or not isinstance(document.get('content'), str) or document.get('size', 65537) > 65536):
            raise ValueError('GitHub did not return a readable root application manifest.')
        try:
            raw = base64.b64decode(''.join(document['content'].split()), validate=True)
        except (KeyError, TypeError, ValueError, binascii.Error):
            raise ValueError('GitHub returned invalid manifest content.') from None
    if len(raw) > 65536:
        raise ValueError('The root application manifest is too large.')
    return manifest(raw.decode('utf-8-sig'))


def existing(store, name):
    with store.connect() as db:
        return db.execute('SELECT * FROM apps WHERE repository_name=?', (identifier(name),)).fetchone()


def record(store, row, repo=None):
    repo = repo or store.repository(row['repository_name'])
    default_path = 'C:\\Software\\'+row['manifest_name']
    return {'repository': row['repository_name'], 'repository_name': repo['display_name'], 'owner': repo['owner'],
            'connection_id': repo['connection_id'], 'repository_available': repo['available'],
            'repository_error': repo['unavailable_reason'], 'refresh_job': repo['refresh_job'],
            'private': repo['private'], 'published': bool(row['published']),
            'manifest_name': row['manifest_name'], 'version': row['manifest_version'],
            'default_display_name': row['manifest_name'], 'display_name_override': row['display_name_override'],
            'display_name': row['display_name_override'] or row['manifest_name'],
            'default_install_path': default_path, 'install_path_override': row['install_path_override'],
            'install_path': row['install_path_override'] or default_path, 'description': row['description'],
            'source_available': repo['available'] and not row['source_error'],
            'source_error': row['source_error'] or repo['unavailable_reason'], 'updated': row['updated']}


def get(store, name):
    row = existing(store, name)
    if row is None:
        raise ValueError('Application not found. Enable Show in Apps in Repositories first.')
    return record(store, row)


def overview(store):
    repos = {repo['name']: repo for repo in store.repositories()}
    with store.connect() as db:
        rows = db.execute('SELECT * FROM apps WHERE published=1').fetchall()
    result = [record(store, row, repos[row['repository_name']]) for row in rows if row['repository_name'] in repos]
    return sorted(result, key=lambda app: (app['display_name'].casefold(), app['owner'].casefold(), app['repository']))


def check_connection(db, name, connection_id, revision):
    current = db.execute('SELECT r.connection_id,r.enabled,c.status,c.revision FROM repositories r '
                         'JOIN connections c ON c.id=r.connection_id WHERE r.name=?', (name,)).fetchone()
    if not current or current['status'] != 'ready' or current['connection_id'] != connection_id or current['revision'] != revision:
        raise Conflict('The connection changed. Refresh the page and try again.')
    return current


def request_publish(store, name, value):
    """Keep GitHub network access in the existing Internet-enabled worker."""
    if not isinstance(value, dict) or set(value) != {'published'} or type(value['published']) is not bool:
        raise ValueError('Show in Apps requires a published boolean.')
    repo = store.repository(name)
    if not value['published']:
        with store.connect() as db:
            pending = [row['id'] for row in db.execute("SELECT id FROM jobs WHERE kind='app_publish' AND subject=? AND status IN ('queued','running')", (name,))]
        for job in pending:
            store.cancel_job(job)
        return 200, publish(store, name, value)
    repo = store.require_repository(name)
    previous = existing(store, name)
    if (previous and previous['published']) or layout.repository_path(store, repo).exists():
        return 200, publish(store, name, value)
    connection = connections.get(store, repo['connection_id'])
    job = store.enqueue('app_publish', name, {'connection_id': connection['id'], 'connection_revision': connection['revision']})
    return 202, dict(store.repository(name), publication_job=job['id'])


def publish(store, name, value, *, job_id=None, expected_revision=None):
    if not isinstance(value, dict) or set(value) != {'published'} or type(value['published']) is not bool:
        raise ValueError('Show in Apps requires a published boolean.')
    repo = store.repository(name)
    connection = connections.get(store, repo['connection_id'])
    if expected_revision is not None and connection['revision'] != expected_revision:
        raise Conflict('The connection changed. Refresh the page and try again.')
    selected = value['published']
    previous = existing(store, name)
    source = None
    if selected:
        store.require_repository(name)
        if previous is None or not previous['published']:
            source = source_manifest(store, repo)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        current = check_connection(db, name, repo['connection_id'], connection['revision'])
        if job_id:
            job = db.execute('SELECT cancel,status FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job or job['cancel'] or job['status'] != 'running':
                from .process import Cancelled
                raise Cancelled('Adding the app was cancelled.')
        if selected and not current['enabled']:
            raise Conflict('This repository was disabled. Refresh the page and try again.')
        if source:
            db.execute("""INSERT INTO apps(repository_name,published,manifest_name,manifest_version,created,updated)
                          VALUES(?,1,?,?,?,?) ON CONFLICT(repository_name) DO UPDATE SET
                          published=1,manifest_name=excluded.manifest_name,manifest_version=excluded.manifest_version,
                          source_error='',updated=excluded.updated""", (name, *source, now(), now()))
        else:
            db.execute('UPDATE apps SET published=?,updated=? WHERE repository_name=?', (int(selected), now(), name))
    return store.repository(name)


def save(store, name, value):
    if not isinstance(value, dict) or set(value) != {'display_name', 'install_path', 'description'}:
        raise ValueError('Provide Display-Name, Install-Path and Description.')
    app = get(store, name)
    repo = store.repository(name)
    connection = connections.get(store, repo['connection_id'])
    display = value['display_name']
    if display is not None:
        if not isinstance(display, str) or len(display) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in display):
            raise ValueError('Display-Name must be text of at most 128 characters without control characters.')
        display = display.strip() or None
    target = value['install_path']
    if target is not None:
        target = None if isinstance(target, str) and not target.strip() else install_path(target)
    description = value['description']
    if not isinstance(description, str) or len(description) > 8192 or any(ord(char) < 32 and char not in '\r\n\t' for char in description):
        raise ValueError('Description must be text of at most 8192 characters.')
    description = description.replace('\r\n', '\n').replace('\r', '\n')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        check_connection(db, name, repo['connection_id'], connection['revision'])
        db.execute('UPDATE apps SET display_name_override=?,install_path_override=?,description=?,updated=? WHERE repository_name=?',
                   (display, target, description, now(), app['repository']))
    return get(store, name)


def refresh(store, repo):
    """Refresh existing app defaults during repository work; preserve manual fields."""
    if not existing(store, repo['name']):
        return
    connection = connections.get(store, repo['connection_id'])
    try:
        name, version = source_manifest(store, repo)
        error = ''
    except (OSError, ValueError, SourceUnavailable, connections.CredentialError):
        name = version = None
        error = 'The application manifest could not be refreshed. Check repository access and its root module.R4MF.'
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        check_connection(db, repo['name'], repo['connection_id'], connection['revision'])
        db.execute('UPDATE apps SET manifest_name=COALESCE(?,manifest_name),manifest_version=COALESCE(?,manifest_version),'
                   'source_error=?,updated=? WHERE repository_name=?', (name, version, error, now(), repo['name']))


def dispatch(handler):
    from ..common.http import HTTPError
    store = handler.server.workspace
    parts = [value for value in handler.path.split('?', 1)[0].removeprefix('/api/apps/').split('/') if value]
    from urllib.parse import unquote
    parts = [unquote(value) for value in parts]
    try:
        if parts == ['overview'] and handler.command in ('GET', 'HEAD'):
            with store.connect() as db:
                pending = db.execute("SELECT count(*) FROM jobs WHERE kind='app_publish' AND status IN ('queued','running') AND cancel=0").fetchone()[0]
            return handler.json(200, {'apps': overview(store), 'publishing': pending, 'active_jobs':store.active_jobs()})
        if len(parts) == 2 and parts[0] == 'items':
            if handler.command in ('GET', 'HEAD'):
                return handler.json(200, get(store, parts[1]))
            if handler.command == 'PUT':
                return handler.json(200, save(store, parts[1], json.loads(handler.body())))
        raise HTTPError(404, 'not_found')
    except connections.CredentialError as error:
        raise HTTPError(500, 'credential_storage_error', str(error)) from None
    except Conflict as error:
        raise HTTPError(409, 'configuration_changed', str(error)) from None
    except (ValueError, KeyError, TypeError) as error:
        raise HTTPError(422, 'validation_error', str(error)) from None
