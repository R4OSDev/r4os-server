"""Immutable SDK packages. Profile policy and source verification belong to the SDK."""
from contextlib import contextmanager
import fcntl
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import tarfile
import time
from urllib.parse import quote, urlsplit
import urllib.request
import uuid

from .store import Conflict, identifier, now, under

INTERFACE = 'r4os-sdk-package'
PROFILE_FILES = {'Build': 'Profiles/Build.json', 'Tools': 'Profiles/Tools.json', 'Components': 'Profiles/Components.json'}


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS sdk_packages(
        id TEXT PRIMARY KEY, sdk_version TEXT NOT NULL UNIQUE, status TEXT NOT NULL,
        metadata TEXT NOT NULL, error TEXT NOT NULL DEFAULT '', created TEXT NOT NULL, updated TEXT NOT NULL)''')
    db.execute('''CREATE TABLE IF NOT EXISTS sdk_assets(
        sdk_id TEXT NOT NULL REFERENCES sdk_packages(id) ON DELETE CASCADE,
        name TEXT NOT NULL, role TEXT NOT NULL, bytes INTEGER NOT NULL, sha256 TEXT NOT NULL,
        PRIMARY KEY(sdk_id,name))''')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write_json(path, value):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.'+uuid.uuid4().hex)
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2)+'\n')
    temporary.replace(path)


def strict_json(raw):
    def pairs(values):
        result, seen = {}, set()
        for key, value in values:
            if key.casefold() in seen: raise ValueError('Duplicate JSON key: '+key)
            seen.add(key.casefold());result[key] = value
        return result
    def constant(value): raise ValueError('Invalid JSON constant: '+value)
    return json.loads(raw.decode('utf-8-sig') if isinstance(raw, bytes) else raw,
                      object_pairs_hook=pairs, parse_constant=constant)


def root(store, key):
    return under(store.root/'BuildTools/SDK/Packages', identifier(key))


def get(store, key, *, ready=True):
    with store.connect() as db: row = db.execute('SELECT * FROM sdk_packages WHERE id=?', (identifier(key),)).fetchone()
    if row is None: raise ValueError('SDK package not found.')
    if ready and row['status'] != 'ready': raise ValueError('The SDK package has not passed complete verification.')
    return dict(strict_json(row['metadata']), id=row['id'], status=row['status'], error=row['error'])


def packages(store):
    with store.connect() as db: rows = db.execute('SELECT * FROM sdk_packages ORDER BY created DESC,id').fetchall()
    return [dict(strict_json(row['metadata']), id=row['id'], status=row['status'], error=row['error']) for row in rows]


def summary(item):
    manifest = item['manifest']
    return {'id': item['id'], 'version': manifest['sdk_version'], 'status': item['status'], 'error': item.get('error', ''),
            'source': manifest['sdk_source'], 'profile_sources': manifest['profile_sources'],
            'profiles': manifest['profiles'], 'toolsets': manifest['toolsets'], 'component_sets': manifest['component_sets'],
            'release_url': item['release_url'], 'manifest_sha256': item['manifest_sha256'],
            'assets': item['assets'], 'verified_at': item.get('verified_at'), 'r4os_releases':item.get('r4os_releases',[]),
            'tools':manifest['tools'],'system_libraries':manifest['system_libraries'],
            'source_bindings':{key:{field:value[field] for field in ('repository','commit','version','path') if field in value}
                               for key,value in manifest['sources'].items()}}


def publish_index(store):
    items = [summary(item) for item in packages(store) if item['status'] == 'ready']
    write_json(store.root/'BuildTools/SDK/catalog.json', {'schema': 1, 'interface': INTERFACE, 'packages': items})


def release_base(url):
    parsed = urlsplit(url)
    match = re.fullmatch(r'/R4OSDev/r4os-sdk/releases/tag/(v[0-9]+\.[0-9]+\.[0-9]+)', parsed.path)
    if parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment or not match:
        raise ValueError('Use an official R4OSDev/r4os-sdk GitHub release URL.')
    return 'https://github.com/R4OSDev/r4os-sdk/releases/download/'+match[1]+'/', match[1][1:]


def fetch(context, url, path, *, expected=None, size=None, limit=2*1024**3):
    path = Path(path);path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and expected and digest(path) == expected and (size is None or path.stat().st_size == size): return
    temporary = path.with_name(path.name+'.part')
    context.phase('Downloading SDK asset: '+path.name)
    request = urllib.request.Request(url, headers={'User-Agent': 'R4OS-Server'})
    try:
        with urllib.request.urlopen(request, timeout=45) as response, temporary.open('wb') as output:
            total = 0
            while chunk := response.read(1024*1024):
                context.check();total += len(chunk)
                if total > (size if size is not None else limit): raise ValueError('SDK asset exceeds its declared size: '+path.name)
                output.write(chunk)
        if size is not None and total != size: raise ValueError('SDK asset length differs: '+path.name)
        if expected and digest(temporary) != expected: raise ValueError('SDK asset SHA256 differs: '+path.name)
        temporary.replace(path)
    finally: temporary.unlink(missing_ok=True)


def asset_name(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,159}', value):
        raise ValueError('Invalid SDK asset filename.')
    return value


def checksums(raw):
    result, seen = {}, set()
    for line in raw.decode('utf-8-sig').splitlines():
        match = re.fullmatch(r'([0-9a-f]{64}) [ *]([A-Za-z0-9][A-Za-z0-9_.-]{0,159})', line)
        if not match: raise ValueError('Invalid SDK SHA256SUMS entry.')
        name = asset_name(match[2])
        if name.casefold() in seen: raise ValueError('Duplicate SDK checksum filename.')
        seen.add(name.casefold());result[name] = match[1]
    if 'SDK-Release.json' not in result: raise ValueError('SDK-Release.json is not covered by SHA256SUMS.')
    return result


def validate_manifest(manifest, version):
    if type(manifest.get('schema')) is not int or manifest['schema'] != 1 or manifest.get('interface') != INTERFACE:
        raise ValueError('Unsupported SDK release schema or interface.')
    if manifest.get('sdk_version') != version: raise ValueError('SDK release version differs from its selected release.')
    sources = manifest.get('profile_sources', {})
    if set(sources) != set(PROFILE_FILES): raise ValueError('The SDK must declare exactly three profile originals.')
    for name, relative in PROFILE_FILES.items():
        record = sources[name]
        if record.get('path') != relative or type(record.get('schema')) is not int or record['schema'] != 1 or not re.fullmatch('[0-9a-f]{64}', record.get('sha256', '')):
            raise ValueError('Invalid SDK profile source: '+name)
    for field in ('profiles', 'toolsets', 'tools', 'component_sets', 'sources', 'system_libraries', 'policy', 'sdk_source', 'assets'):
        if field not in manifest: raise ValueError('Missing SDK release field: '+field)
    source = manifest['sdk_source']
    if not re.fullmatch('[0-9a-f]{40}', source.get('commit', '')) or source.get('dirty'):
        raise ValueError('A regular SDK package requires an exact clean SDK source identity.')


def extract(archive, destination, prefix):
    if prefix not in ('SDK', 'CONTRACT', 'LIBRARIES', 'RECOVERY', 'Tools'): raise ValueError('Unknown SDK archive prefix.')
    seen = set();total = 0
    with tarfile.open(archive) as package:
        for member in package:
            path = PurePosixPath(member.name)
            if path.is_absolute() or '..' in path.parts or '\\' in member.name or not path.parts or path.parts[0] != prefix:
                raise ValueError('Unsafe SDK archive path.')
            key = str(path).casefold()
            if key in seen: raise ValueError('Ambiguous SDK archive path.')
            seen.add(key);total += member.size
            if total > 3*1024**3 or not (member.isfile() or member.isdir()): raise ValueError('Unsupported SDK archive member.')
            package.extract(member, destination, filter='data')


def roots_file(context, key):
    folder = root(context.store, key)/'Content'
    value = {'contract': str(folder/'CONTRACT'), 'libraries': str(folder/'LIBRARIES'),
             'recovery-sdk': str(folder/'RECOVERY/Platform/SDK'),
             'recovery-contract': str(folder/'RECOVERY/Platform/Contract'),
             'recovery-libraries': str(folder/'RECOVERY/Legal/Sources/Libraries.zip')}
    path = context.store.state/'sdk-roots'/(identifier(key)+'.json');write_json(path, value)
    return path


def cli(context, key, script, arguments=(), *, output=True, timeout=900):
    folder = root(context.store, key)/'Content/SDK'
    path = context.store.state/'sdk-results'/(uuid.uuid4().hex+'.json')
    path.parent.mkdir(exist_ok=True)
    args = ['pwsh', '-NoLogo', '-NoProfile', '-File', folder/'Tools'/script, *arguments]
    if output: args += ['-Output', path]
    try:
        text, _ = context.run(args, timeout=timeout, quiet=output)
        return strict_json(path.read_bytes() if path.exists() else text) if output else text
    finally: path.unlink(missing_ok=True)


def _verify(context, key):
    folder = root(context.store, key)
    package = get(context.store, key, ready=False)
    for item in package['assets']:
        path = folder/'Payload'/item['path']
        if not path.is_file() or path.stat().st_size != item['bytes'] or digest(path) != item['sha256']:
            raise ValueError('SDK payload differs from its immutable registration: '+item['path'])
    context.phase('Verifying the complete SDK package with its owner tools')
    result = cli(context, key, 'Package.ps1', ['-Action', 'Verify', '-PayloadRoot', folder/'Payload',
                 '-Manifest', folder/'Payload/SDK-Release.json', '-RootsFile', roots_file(context, key), '-ToolsRoot', folder/'Content/Tools'])
    cli(context, key, 'Profile.ps1', ['-Action', 'Validate'])
    return result


def verify(context,key):
    try:return _verify(context,key)
    except (OSError,ValueError,RuntimeError) as error:
        with context.store.connect() as db:
            changed=db.execute("UPDATE sdk_packages SET status='invalid',error=?,updated=? WHERE id=? AND status='ready'",(str(error)[-4000:],now(),key))
        if changed.rowcount:publish_index(context.store)
        raise


@contextmanager
def lock(context):
    path = context.store.state/'sdk.lock'
    with path.open('a') as stream:
        while True:
            context.check()
            try: fcntl.flock(stream, fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError: time.sleep(.1)
        try: yield
        finally: fcntl.flock(stream, fcntl.LOCK_UN)


def import_release(context, value):
    if not isinstance(value, dict) or set(value) != {'release_url'}: raise ValueError('Specify the SDK release_url.')
    base, version = release_base(value['release_url']);store = context.store
    with lock(context):
        with store.connect() as db:existing=db.execute("SELECT id FROM sdk_packages WHERE sdk_version=? AND status='ready'",(version,)).fetchone()
        if existing:
            verify(context,existing['id'])
            return {'sdk_id':existing['id'],'unchanged':True}
    stage = store.state/'sdk-imports'/context.job['id'];stage.mkdir(parents=True, exist_ok=True)
    fetch(context, base+'SHA256SUMS.txt', stage/'SHA256SUMS.txt', limit=65536)
    sums = checksums((stage/'SHA256SUMS.txt').read_bytes())
    fetch(context, base+'SDK-Release.json', stage/'SDK-Release.json', expected=sums['SDK-Release.json'], limit=8*1024**2)
    manifest = strict_json((stage/'SDK-Release.json').read_bytes());validate_manifest(manifest, version)
    sha = digest(stage/'SDK-Release.json');key = version+'-'+sha[:24]
    with lock(context):
        with store.connect() as db:
            previous = db.execute('SELECT id,status FROM sdk_packages WHERE sdk_version=?', (version,)).fetchone()
        if previous and previous['id'] != key: raise Conflict('This SDK version is already bound to a different immutable package.')
        if previous and previous['status'] == 'ready':
            verify(context, key);return {'sdk_id': key, 'unchanged': True}
        entries = {};roles = set()
        for item in manifest['assets']:
            name = asset_name(item['path']);role = item['role']
            if name.casefold() in {n.casefold() for n in entries} or role in roles: raise ValueError('Duplicate SDK asset identity.')
            if type(item['bytes']) is not int or not 0 < item['bytes'] <= 2*1024**3 or item['sha256'] != sums.get(name):
                raise ValueError('SDK manifest/checksum contract differs: '+name)
            if item['url'] != base+name: raise ValueError('SDK assets must come from the selected official release.')
            entries[name] = item;roles.add(role)
        required = {'sdk-source','contract-source','libraries-source','recovery-sources','tools','capabilities','qualification','readme'}
        if roles != required or set(sums) != set(entries)|{'SDK-Release.json'}: raise ValueError('The complete SDK asset set is required.')
        entries['SDK-Release.json'] = {'path':'SDK-Release.json','role':'release','bytes':(stage/'SDK-Release.json').stat().st_size,'sha256':sha,'url':base+'SDK-Release.json'}
        entries['SHA256SUMS.txt'] = {'path':'SHA256SUMS.txt','role':'checksums','bytes':(stage/'SHA256SUMS.txt').stat().st_size,'sha256':digest(stage/'SHA256SUMS.txt'),'url':base+'SHA256SUMS.txt'}
        metadata = {'manifest': manifest, 'manifest_sha256': sha, 'release_url': value['release_url'], 'assets': list(entries.values())}
        with store.connect() as db:
            db.execute('''INSERT INTO sdk_packages VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                          status=excluded.status,error='',updated=excluded.updated''',
                       (key, version, 'importing', json.dumps(metadata), '', now(), now()))
        folder = root(store, key)
        try:
            payload = folder/'Payload';payload.mkdir(parents=True, exist_ok=True)
            for name, entry in entries.items():
                fetch(context, base+quote(name), payload/name, expected=entry['sha256'], size=entry['bytes'])
            content = folder/'Content'
            if content.exists(): shutil.rmtree(content)
            content.mkdir()
            for entry in entries.values():
                if entry.get('prefix'): extract(payload/entry['path'], content, entry['prefix'])
            for name, relative in PROFILE_FILES.items():
                path = content/'SDK'/relative
                if digest(path) != manifest['profile_sources'][name]['sha256']: raise ValueError('SDK original profile hash differs: '+name)
                profile = strict_json(path.read_bytes())
                if type(profile.get('schema')) is not int or profile['schema'] != 1: raise ValueError('Unsupported SDK profile schema: '+name)
            capability = strict_json((payload/next(v['path'] for v in entries.values() if v['role']=='capabilities')).read_bytes())
            if (type(capability.get('schema')) is not int or capability['schema'] != 3 or capability.get('package_interface') != INTERFACE
                    or capability.get('profile_sources') != {n:v['sha256'] for n,v in manifest['profile_sources'].items()}
                    or capability.get('build_profiles') != manifest['profiles']):
                raise ValueError('SDK capabilities do not belong to this profile package.')
            verify(context, key)
            metadata.update(capabilities=capability, verified_at=now())
            with store.connect() as db:
                db.execute("UPDATE sdk_packages SET status='ready',metadata=?,error='',updated=? WHERE id=?", (json.dumps(metadata), now(), key))
                db.execute('DELETE FROM sdk_assets WHERE sdk_id=?', (key,))
                db.executemany('INSERT INTO sdk_assets VALUES(?,?,?,?,?)', [(key, name, item['role'], item['bytes'], item['sha256']) for name,item in entries.items()])
            publish_index(store)
            shutil.rmtree(stage)
            return {'sdk_id': key, 'version': version, 'assets': len(entries), 'status':'ready'}
        except BaseException as error:
            with store.connect() as db: db.execute("UPDATE sdk_packages SET status='invalid',error=?,updated=? WHERE id=?", (str(error)[-4000:], now(), key))
            raise


def resolve(context, key, profile, *, additional=None, development=None):
    get(context.store, key)
    args = ['-Action', 'Resolve', '-Profile', profile, '-RootsFile', roots_file(context, key)]
    if additional: args += ['-AdditionalLibrariesFile', additional]
    if development: args += ['-DevelopmentProfile', development]
    return cli(context, key, 'Profile.ps1', args)


def provenance(item, profile=None):
    manifest = item['manifest']
    return {'sdk_id':item['id'], 'sdk_version':manifest['sdk_version'], 'sdk_commit':manifest['sdk_source']['commit'],
            'release_sha256':item['manifest_sha256'], 'profile_sources':{name:entry['sha256'] for name,entry in manifest['profile_sources'].items()},
            **({'profile':profile} if profile else {})}
