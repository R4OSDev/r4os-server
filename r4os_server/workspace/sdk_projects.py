"""Explicit app-owned dependency pins and technical SDK development snapshots."""
import json
from pathlib import Path, PurePosixPath
import re
import uuid

from . import catalog, sdk_packages
from .store import identifier, now


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS sdk_projects(
        repository_name TEXT PRIMARY KEY REFERENCES repositories(name) ON DELETE CASCADE,
        sdk_id TEXT REFERENCES sdk_packages(id), libraries TEXT NOT NULL DEFAULT '[]', development_id TEXT)''')
    db.execute('''CREATE TABLE IF NOT EXISTS sdk_developments(
        id TEXT PRIMARY KEY, sdk_id TEXT NOT NULL REFERENCES sdk_packages(id),
        metadata TEXT NOT NULL, created TEXT NOT NULL)''')


def get(store, name):
    store.require_repository(name)
    with store.connect() as db: row = db.execute('SELECT * FROM sdk_projects WHERE repository_name=?',(name,)).fetchone()
    return {'sdk_id':row['sdk_id'] if row else None, 'libraries':json.loads(row['libraries']) if row else [],
            'development_id':row['development_id'] if row else None}


def save(store, name, value):
    store.require_repository(name)
    if not isinstance(value,dict) or set(value)!={'sdk_id','libraries','development_id'}: raise ValueError('Specify sdk_id, libraries and development_id.')
    if value['sdk_id'] is not None: sdk_packages.get(store,value['sdk_id'])
    if not isinstance(value['libraries'],list) or len(value['libraries'])>32: raise ValueError('Specify at most 32 additional library pins.')
    ids=set()
    for entry in value['libraries']:
        if not isinstance(entry,dict) or set(entry)-{'id','repository','commit','path','tree'} or not {'id','repository'}.issubset(entry):
            raise ValueError('Each library requires id, repository and exactly one Git commit or vendored tree pin.')
        if not isinstance(entry['id'],str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,95}',entry['id']) or entry['id'] in ids:raise ValueError('Invalid or duplicate additional library ID.')
        ids.add(entry['id']);store.require_repository(entry['repository'])
        if ('commit' in entry)==('tree' in entry):raise ValueError('Choose exactly one Git commit or vendored tree pin.')
        if 'commit' in entry and (not isinstance(entry['commit'],str) or not re.fullmatch('[0-9a-f]{40}',entry['commit'])):raise ValueError('Each Git library needs its exact commit.')
        if 'tree' in entry:
            tree=entry['tree']
            if (not isinstance(tree,dict) or set(tree)!={'algorithm','sha256','files'} or tree['algorithm']!='r4os-files-sha256-lf-v1'
                    or not isinstance(tree['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',tree['sha256'])
                    or not isinstance(tree['files'],list) or not tree['files']):raise ValueError('Specify a complete SDK vendored tree pin.')
            seen=set()
            for file in tree['files']:
                if not isinstance(file,str) or not file or PurePosixPath(file).is_absolute() or any(part in ('','.','..') for part in file.split('/')) or '\\' in file or file.casefold() in seen:raise ValueError('Invalid or duplicate vendored tree path.')
                seen.add(file.casefold())
        if 'path' in entry:
            if not isinstance(entry['path'],str):raise ValueError('Invalid library subpath.')
            path=PurePosixPath(entry['path'])
            if path.is_absolute() or any(part in ('','.','..') for part in entry['path'].split('/')) or '\\' in entry['path']:raise ValueError('Invalid library subpath.')
    if value['development_id'] is not None:
        with store.connect() as db: row=db.execute('SELECT sdk_id FROM sdk_developments WHERE id=?',(identifier(value['development_id']),)).fetchone()
        if row is None or row['sdk_id'] != (value['sdk_id'] or store.setting('active_sdk')): raise ValueError('Development snapshot belongs to a different SDK.')
    with store.connect() as db:
        db.execute('INSERT INTO sdk_projects VALUES(?,?,?,?) ON CONFLICT(repository_name) DO UPDATE SET sdk_id=excluded.sdk_id,libraries=excluded.libraries,development_id=excluded.development_id',
                   (name,value['sdk_id'],json.dumps(value['libraries']),value['development_id']))
    return get(store,name)


def create_development(context, value):
    fields={'sdk_id','sdk_repository','contract_repository','libraries_repository','profile'}
    if not isinstance(value,dict) or set(value)!=fields: raise ValueError('Choose SDK, profile and three explicit development source repositories.')
    package=sdk_packages.get(context.store,value['sdk_id'])
    if value['profile'] not in package['manifest']['profiles']: raise ValueError('Unknown SDK profile.')
    roots={}
    for role in ('sdk','contract','libraries'):
        repo=context.store.require_repository(value[role+'_repository'])
        root=catalog.repository_path(context.store,repo)
        if not (root/'.git').is_dir(): raise ValueError('Development snapshots require explicit Git source repositories.')
        roots[role]=root
    key=uuid.uuid4().hex;output=context.store.state/'sdk-development'/(key+'.json');output.parent.mkdir(exist_ok=True)
    context.run(['pwsh','-NoLogo','-NoProfile','-File',roots['sdk']/'Tools/Profile.ps1','-Action','Develop',
                 '-Profile',value['profile'],'-ContractRoot',roots['contract'],'-LibrariesRoot',roots['libraries'],'-Output',output],timeout=600)
    snapshot=sdk_packages.strict_json(output.read_bytes())
    if snapshot.get('sdk_version')!=package['manifest']['sdk_version'] or snapshot.get('profile')!=value['profile'] or not snapshot.get('technical') or snapshot.get('hashes') != sdk_packages.provenance(package)['profile_sources']:
        output.unlink();raise ValueError('Development snapshot differs from the selected SDK profile originals.')
    record=dict(value, snapshot=key)
    with context.store.connect() as db: db.execute('INSERT INTO sdk_developments VALUES(?,?,?,?)',(key,value['sdk_id'],json.dumps(record),now()))
    return {'id':key,'sdk_id':value['sdk_id'],'technical':True}


def development(store, key, sdk_id):
    with store.connect() as db: row=db.execute('SELECT * FROM sdk_developments WHERE id=?',(identifier(key),)).fetchone()
    if row is None or row['sdk_id']!=sdk_id: raise ValueError('Development snapshot is missing or belongs to a different SDK.')
    record=json.loads(row['metadata']);roots={}
    for role in ('sdk','contract','libraries'):
        roots[role]=catalog.repository_path(store,store.require_repository(record[role+'_repository']))
    return roots,store.state/'sdk-development'/(key+'.json')
