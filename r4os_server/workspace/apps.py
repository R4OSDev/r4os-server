"""Server-owned application settings; publishing only lists an app in the WebApp."""
import base64
import binascii
import json
import re
from urllib.parse import quote

from . import connections, layout, repo_metadata, packages
from .store import Conflict, identifier, now, under


class SourceUnavailable(Exception):
    pass


def initialize(db):
    packages.initialize(db)


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
        return db.execute('SELECT * FROM software_packages WHERE repository_name=?', (identifier(name),)).fetchone()


def record(store, row, repo=None):
    repo = repo or store.repository(row['source_repository'])
    from .app_releases import view as release_view
    release = release_view(store, dict(repo,package_key=row['repository_name']))
    release_version = (release['local'] or release['available'] or {}).get('version')
    data=(release['local'] or {}).get('package') or json.loads(row['metadata'])
    default_path=data['installation']['path']
    return {'repository': row['repository_name'], 'source_repository':row['source_repository'],
            'package_id':packages.package_id(repo['id'],row['target']), 'target':row['target'],
            'package_type':row['package_type'], 'format':row['format'], 'installation':data['installation'], 'repository_name': repo['display_name'], 'owner': repo['owner'],
            'connection_id': repo['connection_id'], 'repository_available': repo['available'],
            'category': repo['server_metadata'].get('category', 'Undefined'),
            'repository_error': repo['unavailable_reason'], 'refresh_job': repo['refresh_job'],
            'private': repo['private'], 'published': bool(row['published']),
            'manifest_name': row['manifest_name'], 'source_version': row['manifest_version'],
            'version': release_version, 'release': release,
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
        rows = db.execute('SELECT * FROM software_packages WHERE published=1').fetchall()
    result = [record(store, row, repos[row['source_repository']]) for row in rows if row['source_repository'] in repos]
    return sorted(result, key=lambda app: (app['display_name'].casefold(), app['owner'].casefold(), app['repository']))


def check_connection(db, name, connection_id, revision):
    current = db.execute('SELECT r.connection_id,r.enabled,c.status,c.revision FROM repositories r '
                         'JOIN connections c ON c.id=r.connection_id WHERE r.name=?', (name,)).fetchone()
    if not current or current['status'] != 'ready' or current['connection_id'] != connection_id or current['revision'] != revision:
        raise Conflict('The connection changed. Refresh the page and try again.')
    return current


def publication_value(value):
    if (not isinstance(value,dict) or not {'published'}<=set(value) or set(value)-{'published','targets'}
            or type(value['published']) is not bool or 'targets' in value and
            (not isinstance(value['targets'],list) or any(not isinstance(x,str) for x in value['targets']) or len(set(value['targets']))!=len(value['targets']))):
        raise ValueError('Show in Apps requires a published boolean and optional unique target IDs.')


def candidates(store, name):
    repo=store.repository(name);modules=packages.discover(store,repo)
    with store.connect() as db:rows=db.execute('SELECT * FROM software_packages WHERE source_repository=?',(name,)).fetchall()
    selected={row['target'] for row in rows if row['published']}
    if not modules:modules=[{'id':row['target'],'name':row['manifest_name'],'kind':row['format'],'version':row['manifest_version']} for row in rows]
    return {'targets':[{'id':m['id'],'name':m.get('name') or m['id'],'format':m['kind'],'package_type':packages.kind(m),
                        'version':m.get('version'),'selected':m['id'] in selected} for m in modules]}


def request_publish(store, name, value):
    publication_value(value);repo=store.repository(name)
    if not value['published']:
        with store.connect() as db:pending=[row['id'] for row in db.execute("SELECT id FROM jobs WHERE kind GLOB 'app_*' AND subject=? AND status IN ('queued','running')",(name,))]
        for job in pending:store.cancel_job(job)
        return 200,publish(store,name,value)
    repo=store.require_repository(name)
    if layout.repository_path(store,repo).exists():return 200,publish(store,name,value)
    connection=connections.get(store,repo['connection_id'])
    payload={'connection_id':connection['id'],'connection_revision':connection['revision']}
    if 'targets' in value:payload['targets']=value['targets']
    job=store.enqueue('app_publish',name,payload)
    return 202,dict(store.repository(name),publication_job=job['id'])


def publish(store, name, value, *, job_id=None, expected_revision=None):
    publication_value(value);repo=store.repository(name);connection=connections.get(store,repo['connection_id'])
    if expected_revision is not None and connection['revision']!=expected_revision:raise Conflict('The connection changed. Refresh and retry.')
    modules=[]
    if value['published']:
        store.require_repository(name)
        if layout.repository_path(store,repo).exists():modules=packages.discover(store,repo)
        else:
            try:
                target,version=source_manifest(store,repo)
                modules=[{'id':target,'name':target,'kind':'R4X','version':version,'manifest':'module.R4MF'}]
            except ValueError:
                if not job_id:raise
                from .process import Context
                modules=packages.discover(store,repo,Context(store,store.job(job_id)))
        available={module['id'] for module in modules}
        if not available:raise ValueError('This repository has no supported production software packages.')
        selected=value.get('targets')
        if selected is None:
            if len(modules)!=1:raise Conflict('Select the production packages to show in Apps.')
            selected=[modules[0]['id']]
        if not set(selected)<=available:raise Conflict('A selected package target is no longer available. Refresh and choose again.')
    else:selected=[]
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');current=check_connection(db,name,connection['id'],connection['revision'])
        if job_id:
            job=db.execute('SELECT cancel,status FROM jobs WHERE id=?',(job_id,)).fetchone()
            if not job or job['cancel'] or job['status']!='running':
                from .process import Cancelled
                raise Cancelled('Adding packages was cancelled.')
        if selected and not current['enabled']:raise Conflict('This repository was disabled.')
        for module in modules:packages.upsert(db,repo,module)
        db.execute('UPDATE software_packages SET published=0,updated=? WHERE source_repository=?',(now(),name))
        for target in selected:db.execute('UPDATE software_packages SET published=1 WHERE source_repository=? AND target=?',(name,target))
    return store.repository(name)


def save(store, name, value):
    if not isinstance(value, dict) or set(value) not in ({'display_name', 'install_path', 'description'}, {'display_name', 'install_path', 'description', 'category'}):
        raise ValueError('Provide Display-Name, Install-Path and Description.')
    app = get(store, name)
    row=existing(store,name)
    repo = store.repository(row['source_repository'])
    connection = connections.get(store, repo['connection_id'])
    display = value['display_name']
    if display is not None:
        if not isinstance(display, str) or len(display) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in display):
            raise ValueError('Display-Name must be text of at most 128 characters without control characters.')
        display = display.strip() or None
    target = value['install_path']
    if target is not None:
        target = None if isinstance(target, str) and not target.strip() else (install_path(target) if row['package_type']=='application' else packages.runtime_path(target))
    description = value['description']
    if not isinstance(description, str) or len(description) > 8192 or any(ord(char) < 32 and char not in '\r\n\t' for char in description):
        raise ValueError('Description must be text of at most 8192 characters.')
    description = description.replace('\r\n', '\n').replace('\r', '\n')
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        check_connection(db, repo['name'], repo['connection_id'], connection['revision'])
        if 'category' in value:
            repo_metadata.update(db, repo['name'], {'category': value['category']})
        db.execute('UPDATE software_packages SET display_name_override=?,install_path_override=?,description=?,updated=? WHERE repository_name=?',
                   (display, target, description, now(), app['repository']))
    return get(store, name)


def refresh(store, repo):
    with store.connect() as db:rows=db.execute('SELECT * FROM software_packages WHERE source_repository=?',(repo['name'],)).fetchall()
    if not rows:return
    connection=connections.get(store,repo['connection_id'])
    try:
        modules=packages.discover(store,repo)
        if not modules:
            name,version=source_manifest(store,repo)
            modules=[{'id':name,'name':name,'version':version,'kind':'R4X','manifest':'module.R4MF'}]
        by_id={module['id']:module for module in modules}
    except (OSError,ValueError,SourceUnavailable,connections.CredentialError):by_id={}
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');check_connection(db,repo['name'],connection['id'],connection['revision'])
        for row in rows:
            module=by_id.get(row['target'])
            if module:packages.upsert(db,repo,module)
            else:db.execute('UPDATE software_packages SET source_error=?,updated=? WHERE repository_name=?',
                            ('The production package manifest could not be refreshed. Check source access and target identity.',now(),row['repository_name']))


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
            return handler.json(200, {'apps': overview(store), 'publishing': pending, 'active_jobs':store.active_jobs(), 'categories': repo_metadata.CATEGORIES})
        if parts[:1] == ['actions'] and len(parts) == 2 and handler.command == 'POST':
            from .app_releases import request, batch
            value = json.loads(handler.body())
            action = parts[1]
            if action.endswith('_all'):
                if value != {}:
                    raise ValueError('Bulk app actions require an empty JSON object.')
                return handler.json(202, batch(store, action.removesuffix('_all')))
            if not isinstance(value, dict) or set(value) != {'name'}:
                raise ValueError('Provide the selected app name.')
            return handler.json(202, request(store, value['name'], action))
        if len(parts)==2 and parts[0]=='targets' and handler.command in ('GET','HEAD'):
            return handler.json(200,candidates(store,parts[1]))
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
