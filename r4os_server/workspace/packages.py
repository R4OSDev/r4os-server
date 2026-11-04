"""Target-based software identities and metadata, independent of UI categories."""
import hashlib
import json
from pathlib import PurePosixPath
import re

from . import build_targets, layout
from .store import Conflict, identifier, now, under


def initialize(db):
    db.execute('''CREATE TABLE IF NOT EXISTS software_packages(
        repository_name TEXT PRIMARY KEY,
        source_repository TEXT NOT NULL REFERENCES repositories(name) ON DELETE CASCADE,
        target TEXT NOT NULL, package_type TEXT NOT NULL, format TEXT NOT NULL,
        metadata TEXT NOT NULL DEFAULT '{}',
        published INTEGER NOT NULL DEFAULT 0 CHECK(published IN (0,1)),
        manifest_name TEXT NOT NULL, manifest_version TEXT NOT NULL,
        display_name_override TEXT, install_path_override TEXT,
        description TEXT NOT NULL DEFAULT '', source_error TEXT NOT NULL DEFAULT '',
        created TEXT NOT NULL, updated TEXT NOT NULL,
        UNIQUE(source_repository,target))''')
    legacy = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='apps'").fetchone()
    if legacy:
        for old in db.execute('SELECT * FROM apps').fetchall():
            module = {'id':old['manifest_name'],'name':old['manifest_name'],'kind':'R4X','version':old['manifest_version'],'manifest':'module.R4MF'}
            values = dict(old)
            values.update(source_repository=old['repository_name'], target=module['id'], package_type='application',
                          format='R4X', metadata=json.dumps(metadata(module)))
            data=json.loads(values['metadata']);data.update(runtime_dependencies=None,declared_imports=None)
            values['metadata']=json.dumps(data)
            keys=list(values)
            db.execute('INSERT OR IGNORE INTO software_packages('+','.join(keys)+') VALUES('+','.join('?' for _ in keys)+')',list(values.values()))


def package_id(repository_id, target):
    if type(repository_id) is not int or repository_id<=0:
        raise ValueError('Invalid GitHub repository ID.')
    return 'gh-'+str(repository_id)+'-'+identifier(target)


def key(repo, module):
    if module.get('manifest')=='module.R4MF' or module['id']=='kernel':
        return repo['name']
    return 'pkg-'+hashlib.sha256((repo['name']+'\0'+module['id']).encode()).hexdigest()[:40]


def kind(module):
    if module.get('kind')=='R4X':return 'application'
    if module.get('kind')=='R4L':return 'library'
    if module.get('kind')=='ELF' and module.get('id')=='kernel':return 'kernel'
    raise ValueError('This target is not an application, runtime library or production kernel.')


def runtime_path(value):
    if not isinstance(value,str) or len(value)>240 or re.search(r'[\x00-\x1f\x7f<>"|?*]',value):
        raise ValueError('Enter an absolute runtime path of at most 240 characters.')
    if value.startswith('/'):
        if '\\' in value or value.endswith('/') or any(part in ('','.','..') for part in value[1:].split('/')):
            raise ValueError('Invalid absolute runtime path.')
        return value
    from .apps import install_path
    return install_path(value)


def metadata(module):
    package_type=kind(module)
    target=module.get('target') or None
    if target:target=runtime_path(target)
    installation={'role':{'application':'application','library':'runtime_library','kernel':'boot_kernel'}[package_type],
                  'path':('C:\\Software\\'+module['id']) if package_type=='application' else target,
                  'path_kind':'directory' if package_type=='application' else 'file',
                  'activation':'client_managed' if package_type!='kernel' else 'boot_configuration',
                  'requires_client_plan':package_type=='kernel'}
    dependencies=[]
    for raw in module.get('imports') or []:
        match=re.fullmatch(r'([A-Za-z0-9_-]+):([A-Za-z0-9_]+):([0-9]{1,10})',raw)
        if not match:dependencies=None;break
        dependencies.append({'module_id':match[1],'symbol':match[2],'abi_version':int(match[3]),'package_id':None})
    exports=[]
    for raw in module.get('exports') or []:
        match=re.fullmatch(r'([A-Za-z0-9_]+):([A-Za-z0-9_]+):([0-9]{1,10})',raw)
        if not match:exports=None;break
        exports.append({'symbol':match[1],'abi_version':int(match[3])})
    return {'target':module['id'],'version':module.get('version'),'name':module.get('name') or module['id'], 'package_type':package_type,
            'format':module['kind'],'artifact_name':('r4os.elf' if package_type=='kernel' else module['id']+'.'+module['kind']),
            'manifest':module.get('manifest'), 'installation':installation,
            'compatibility':{'architecture':None,'abi':None,'os_version':None,'sdk_profile':None},
            'runtime_dependencies':dependencies, 'declared_imports':list(module.get('imports') or []),
            'provides':{'module_id':module['id'],'exports':exports} if package_type=='library' else None}


def parse(text, relative='module.R4MF'):
    if len(text.encode())>65536:raise ValueError('The package manifest is too large.')
    fields={}
    for raw in text.lstrip('\ufeff').splitlines():
        if raw.strip() and not raw.lstrip().startswith('#') and '=' in raw:
            name,value=raw.strip().split('=',1);fields.setdefault(name,[]).append(value.strip())
    def one(name,default=None):
        values=fields.get(name,[])
        if not values and default is not None:return default
        if len(values)!=1:raise ValueError('The package manifest requires one '+name+' entry.')
        return values[0]
    if one('R4OS_MODULE_MANIFEST')!='2':raise ValueError('Unsupported package manifest version.')
    name=one('NAME');version=one('VERSION')
    if not re.fullmatch('[A-Za-z0-9_-]{1,31}',name) or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+',version) or len(version)>64:
        raise ValueError('Invalid package name or version.')
    module={'id':name,'name':name,'version':version,'kind':one('KIND'),'manifest':relative,
            'target':one('TARGET',''),'imports':fields.get('IMPORT',[]),'exports':fields.get('EXPORT',[])}
    metadata(module)
    return module


def discover(store, repo, context=None):
    root=layout.repository_path(store,repo)
    result=[]
    if root.exists():
        cached=[m['manifest'] for m in repo['local'].get('modules',[]) if m.get('manifest')]
        paths=cached or [p.relative_to(root).as_posix() for p in root.rglob('*.R4MF')]
        for relative in sorted(set(paths)):
            if any(part.lower() in build_targets.NONPRODUCTION for part in PurePosixPath(relative).parts):continue
            file=under(root,relative)
            text=file.read_text(encoding='utf-8-sig')
            if re.search(r'^KIND=(?:R4X|R4L)\s*$',text,re.M):result.append(parse(text,relative))
        if repo['display_name']=='r4os-kernel':
            module={'id':'kernel','name':'Production kernel','kind':'ELF','language':'Rust'}
            result.append(build_targets.versioned(module,lambda name:(root/name).read_text() if (root/name).is_file() else None))
    elif context:
        from . import artifact_source,connections
        from .github_api import Client
        client=Client(repo,connections.token(store,repo['connection_id']),context.check)
        commit=artifact_source.head(context,repo,client)
        result=[m for m in artifact_source.targets(context,repo,client,commit) if m.get('kind') in ('R4X','R4L') or m['id']=='kernel']
    else:
        return []
    ids=[module['id'] for module in result]
    if len(set(ids))!=len(ids):raise ValueError('The repository has ambiguous package target IDs.')
    return result


def upsert(db, repo, module):
    data=metadata(module);timestamp=now()
    old=db.execute('SELECT * FROM software_packages WHERE source_repository=? AND target=?',(repo['name'],module['id'])).fetchone()
    if old and (old['package_type'],old['format'])!=(data['package_type'],data['format']):
        raise Conflict('The package type changed for an existing target. Use a new target identity.')
    item=old['repository_name'] if old else key(repo,module)
    collision=db.execute('SELECT target FROM software_packages WHERE repository_name=?',(item,)).fetchone()
    if collision and collision['target']!=module['id']:item='pkg-'+hashlib.sha256((repo['name']+'\0'+module['id']).encode()).hexdigest()[:40]
    db.execute('''INSERT INTO software_packages(repository_name,source_repository,target,package_type,format,metadata,
                  manifest_name,manifest_version,created,updated) VALUES(?,?,?,?,?,?,?,?,?,?)
                  ON CONFLICT(repository_name) DO UPDATE SET metadata=excluded.metadata,manifest_name=excluded.manifest_name,
                  manifest_version=excluded.manifest_version,source_error='',updated=excluded.updated''',
               (item,repo['name'],module['id'],data['package_type'],data['format'],json.dumps(data),module.get('name') or module['id'],module['version'],timestamp,timestamp))
    return item


def release_metadata(context, repo, client, commit, target):
    from . import artifact_source
    cache=getattr(context,'package_source_metadata',{})
    key=(repo['id'],commit)
    if key not in cache:
        cache[key]=artifact_source.targets(context,repo,client,commit);context.package_source_metadata=cache
    found=[module for module in cache[key] if module['id']==target]
    if len(found)!=1:raise ValueError('The release source has no unique matching production package target.')
    return metadata(found[0])
