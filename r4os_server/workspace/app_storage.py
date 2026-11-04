"""Verified GitHub release files, independent of source checkouts and build output."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import shutil

from .store import Conflict, identifier, now


def initialize(db):
    # Rebind the two child tables after the one-time app -> package migration.
    saved = {}
    for name in ('app_release_catalog','app_release_refs'):
        if any(row['table']=='apps' for row in db.execute('PRAGMA foreign_key_list('+name+')')):
            saved[name]=[tuple(row) for row in db.execute('SELECT * FROM '+name)]
            db.execute('DROP TABLE '+name)
    script = '''
        CREATE TABLE IF NOT EXISTS app_release_catalog(
            repository_name TEXT PRIMARY KEY REFERENCES software_packages(repository_name) ON DELETE CASCADE,
            connection_revision INTEGER NOT NULL, status TEXT NOT NULL,
            selection TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', checked_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS app_releases(
            repository_id INTEGER NOT NULL, release_id INTEGER NOT NULL,
            metadata TEXT NOT NULL, directory TEXT NOT NULL UNIQUE,
            downloaded_at TEXT NOT NULL, verified_at TEXT NOT NULL,
            PRIMARY KEY(repository_id,release_id));
        CREATE TABLE IF NOT EXISTS app_release_refs(
            repository_name TEXT NOT NULL REFERENCES software_packages(repository_name) ON DELETE CASCADE,
            repository_id INTEGER NOT NULL, release_id INTEGER NOT NULL,
            current INTEGER NOT NULL DEFAULT 0 CHECK(current IN (0,1)),
            PRIMARY KEY(repository_name,repository_id,release_id),
            FOREIGN KEY(repository_id,release_id) REFERENCES app_releases(repository_id,release_id));
        CREATE UNIQUE INDEX IF NOT EXISTS app_current_release
            ON app_release_refs(repository_name) WHERE current=1;
    '''
    for statement in script.split(';'):
        if statement.strip():
            db.execute(statement)
    for name, rows in saved.items():
        for row in rows:
            db.execute('INSERT INTO '+name+' VALUES('+','.join('?' for _ in row)+')',row)
    db.execute('DROP TABLE IF EXISTS apps')


def number(value):
    if type(value) is not int or value <= 0:
        raise ValueError('A positive GitHub object ID is required.')
    return str(value)


def directory(repository_id, release_id):
    return 'Apps/'+number(repository_id)+'/Releases/'+number(release_id)


def filename(value):
    if (not isinstance(value, str) or not value or len(value) > 200 or value in ('.', '..')
            or value.startswith('.') or re.search(r'[/\\\x00-\x1f\x7f]', value)):
        raise ValueError('The release contains an unsafe asset filename.')
    return value


def path(store, relative):
    parts = PurePosixPath(relative).parts
    if not parts or parts[0] != 'Apps' or any(part in ('.', '..') for part in parts):
        raise ValueError('Invalid app storage path.')
    target = store.root
    for part in parts:
        target /= part
        if target.is_symlink():
            raise ValueError('Unexpected symbolic link in app storage.')
    if not target.resolve().is_relative_to(store.root/'Apps'):
        raise ValueError('The app path is outside app storage.')
    return target


def digest(file):
    value = hashlib.sha256()
    with file.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024*1024), b''):
            value.update(chunk)
    return value.hexdigest()


def verify(folder, selection, *, hashes=True, strict=False):
    files = selection.get('files')
    if not isinstance(files, list) or not files:
        raise ValueError('The app release has no verified file list.')
    names = set()
    for item in files:
        name = filename(item['name'])
        if name.casefold() in names:
            raise ValueError('The app release has ambiguous filenames.')
        names.add(name.casefold())
        file = folder/name
        if (type(item['size']) is not int or not 0 < item['size'] <= 2*1024**3
                or not re.fullmatch('[0-9a-f]{64}', item['sha256'])
                or file.is_symlink() or not file.is_file() or file.stat().st_size != item['size']
                or hashes and digest(file) != item['sha256']):
            raise ValueError('Local app files are missing or differ from their verified checksums.')
    if strict and {item.name.casefold() for item in folder.iterdir()} != names:
        raise ValueError('The app release directory contains unexpected files.')


def selections(value):
    return value.get('targets', {}) if 'targets' in value else {value['target']:value}


def current(store, name, *, hashes=False):
    with store.connect() as db:
        row=db.execute('SELECT a.*,p.target FROM app_releases a JOIN app_release_refs r USING(repository_id,release_id) '
                       'JOIN software_packages p ON p.repository_name=r.repository_name WHERE r.repository_name=? AND r.current=1',(name,)).fetchone()
    if row is None:return None
    document=json.loads(row['metadata']);chosen=selections(document).get(row['target'])
    if chosen is None:return None
    value=dict(chosen)
    verified=document.get('verified',{}).get(row['target'],row['verified_at'])
    value.update(directory=row['directory'],downloaded_at=row['downloaded_at'],verified_at=verified,valid=bool(verified),error='')
    try:verify(path(store,row['directory']),value,hashes=hashes)
    except (OSError,ValueError):value['valid']=False
    if not value['valid']:value['error']='Local package files are missing or damaged. Remove local files and download again.'
    if hashes:
        with store.connect() as db:
            latest=db.execute('SELECT metadata FROM app_releases WHERE repository_id=? AND release_id=?',(row['repository_id'],row['release_id'])).fetchone()
            data=json.loads(latest['metadata']);updated={'targets':selections(data),'verified':data.get('verified',{})}
            updated['verified'][row['target']]=now() if value['valid'] else ''
            db.execute('UPDATE app_releases SET metadata=? WHERE repository_id=? AND release_id=?',(json.dumps(updated),row['repository_id'],row['release_id']))
    return value


def bind(db, repo, revision, job_id=None):
    row = db.execute('SELECT a.published,r.github_id,r.enabled,c.status,c.revision FROM software_packages a '
                     'JOIN repositories r ON r.name=a.source_repository JOIN connections c ON c.id=r.connection_id '
                     'WHERE a.repository_name=?', (repo.get('package_key',repo['name']),)).fetchone()
    if (not row or not row['published'] or not row['enabled'] or row['status'] != 'ready'
            or row['revision'] != revision or row['github_id'] != repo['id']):
        raise Conflict('The app or its connection changed. Refresh the app and try again.')
    if job_id:
        job = db.execute('SELECT cancel,status FROM jobs WHERE id=?', (job_id,)).fetchone()
        if not job or job['cancel'] or job['status'] != 'running':
            from .process import Cancelled
            raise Cancelled('App job cancelled.')


def install(context, repo, revision, selection, staged):
    """Expose a complete target only after all immutable release assets exist."""
    store=context.store;item=repo.get('package_key',repo['name'])
    if selection['repository_id']!=repo['id']:raise Conflict('The release belongs to another repository.')
    relative=directory(repo['id'],selection['release_id']);destination=path(store,relative)
    verify(staged,selection);context.check()
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE');bind(db,repo,revision,context.job['id'])
        row=db.execute('SELECT metadata FROM app_releases WHERE repository_id=? AND release_id=?',(repo['id'],selection['release_id'])).fetchone()
        document=json.loads(row['metadata']) if row else {'targets':{},'verified':{}}
        targets=selections(document);old=targets.get(selection['target'])
        immutable=lambda data:{k:data[k] for k in ('repository_id','release_id','target','version','tag','commit','files')}
        if old and immutable(old)!=immutable(selection):raise Conflict('This release target already has different local assets. Remove local files before retrying.')
        destination.mkdir(parents=True,exist_ok=True)
        for asset in selection['files']:
            file=destination/filename(asset['name'])
            if file.exists():
                if file.is_symlink() or not file.is_file() or file.stat().st_size!=asset['size'] or digest(file)!=asset['sha256']:
                    raise Conflict('A local release filename belongs to different bytes. No file was overwritten.')
            else:(staged/asset['name']).rename(file)
        timestamp=now();targets[selection['target']]=selection
        verified=document.get('verified',{});verified[selection['target']]=timestamp
        db.execute('INSERT INTO app_releases VALUES(?,?,?,?,?,?) ON CONFLICT(repository_id,release_id) DO UPDATE SET metadata=excluded.metadata,verified_at=excluded.verified_at',
                   (repo['id'],selection['release_id'],json.dumps({'targets':targets,'verified':verified}),relative,timestamp,timestamp))
        db.execute('UPDATE app_release_refs SET current=0 WHERE repository_name=?',(item,))
        db.execute('INSERT INTO app_release_refs VALUES(?,?,?,1) ON CONFLICT(repository_name,repository_id,release_id) DO UPDATE SET current=1',(item,repo['id'],selection['release_id']))
        result={'activation_committed':True,'release':selection,'directory':relative}
        db.execute('UPDATE jobs SET result=? WHERE id=?',(json.dumps(result),context.job['id']))
    context.activation_committed=True
    return result


def remove(store, names, *, context=None, repo=None, revision=None):
    if not names:return {'removed_releases':0}
    removed=0
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        if context and repo:bind(db,repo,revision,context.job['id'])
        marks=','.join('?' for _ in names)
        rows=db.execute('SELECT DISTINCT a.* FROM app_releases a JOIN app_release_refs r USING(repository_id,release_id) WHERE r.repository_name IN ('+marks+')',tuple(names)).fetchall()
        db.execute('DELETE FROM app_release_refs WHERE repository_name IN ('+marks+')',tuple(names))
        for row in rows:
            keep={r['target'] for r in db.execute('SELECT p.target FROM app_release_refs r JOIN software_packages p ON p.repository_name=r.repository_name WHERE r.repository_id=? AND r.release_id=?',(row['repository_id'],row['release_id']))}
            folder=path(store,directory(row['repository_id'],row['release_id']))
            document=json.loads(row['metadata']);targets=selections(document)
            if not keep:
                if folder.exists():shutil.rmtree(folder)
                db.execute('DELETE FROM app_releases WHERE repository_id=? AND release_id=?',(row['repository_id'],row['release_id']));removed+=1
            else:
                needed={file['name'] for target,data in targets.items() if target in keep for file in data['files']}
                for target,data in list(targets.items()):
                    if target in keep:continue
                    for file in data['files']:
                        if file['name'] not in needed:
                            candidate=path(store,row['directory']+'/'+filename(file['name']));candidate.unlink(missing_ok=True)
                    del targets[target]
                document={'targets':targets,'verified':{k:v for k,v in document.get('verified',{}).items() if k in keep}}
                db.execute('UPDATE app_releases SET metadata=? WHERE repository_id=? AND release_id=?',(json.dumps(document),row['repository_id'],row['release_id']))
        result={'activation_committed':True,'removed_releases':removed}
        if context and repo:db.execute('UPDATE jobs SET result=? WHERE id=?',(json.dumps(result),context.job['id']))
    if context and repo:context.activation_committed=True
    return result


def recover(store):
    """An interrupted staging directory can never become the current release."""
    staging = path(store, 'Apps/.staging')
    if not staging.exists():
        return
    with store.connect() as db:
        active = {row[0] for row in db.execute("SELECT id FROM jobs WHERE status IN ('queued','running')")}
    for folder in staging.iterdir():
        identifier(folder.name)
        if folder.name not in active:
            if folder.is_symlink() or not folder.is_dir():
                raise ValueError('Unexpected file in app download staging.')
            shutil.rmtree(folder)


def remove_orphans(store, repos, names):
    """Connection removal also cleans a rename left before a database commit."""
    marks = ','.join('?' for _ in names)
    for repository_id in {repo['id'] for repo in repos}:
        with store.connect() as db:
            other = db.execute('SELECT 1 FROM repositories WHERE github_id=? AND name NOT IN ('+marks+') LIMIT 1',
                               (repository_id, *names)).fetchone()
        if not other:
            folder = path(store, 'Apps/'+number(repository_id))
            if folder.exists():
                shutil.rmtree(folder)
