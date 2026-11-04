"""Read-only client projection of explicitly listed, verified local packages."""
from collections import OrderedDict
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading

from ..common.http import HTTPError
from ..workspace import app_storage, packages


QUERY = '''SELECT p.*,r.github_id,r.server_metadata,c.status AS connection_status,
           a.release_id,a.metadata AS release_metadata,a.directory,a.verified_at
           FROM software_packages p JOIN repositories r ON r.name=p.source_repository
           JOIN connections c ON c.id=r.connection_id
           JOIN app_release_refs f ON f.repository_name=p.repository_name
           JOIN app_releases a ON a.repository_id=f.repository_id AND a.release_id=f.release_id
           WHERE p.published=1 AND r.enabled=1 AND c.status='ready' '''


class Catalog:
    def __init__(self, root):
        self.root=Path(root)
        self.database=self.root/'state/workspace.sqlite3'
        self.verified=OrderedDict()
        self.lock=threading.Lock()

    @contextmanager
    def connect(self):
        db=sqlite3.connect('file:'+str(self.database)+'?mode=ro',uri=True,timeout=2)
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            yield db
        finally:db.close()

    def open_asset(self, row, asset):
        expected=app_storage.directory(row['github_id'],row['release_id'])
        if row['directory']!=expected:raise HTTPError(503,'invalid_package_storage')
        name=app_storage.filename(asset['name'])
        if type(asset['size']) is not int or not 0<asset['size']<=2*1024**3 or not re.fullmatch('[0-9a-f]{64}',asset['sha256']):
            raise HTTPError(503,'invalid_package_metadata')
        directory=None;fd=None
        try:
            directory=os.open(self.root/'Apps',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            for part in expected.split('/')[1:]:
                child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
                os.close(directory);directory=child
            fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size!=asset['size']:raise HTTPError(404,'asset_unavailable')
            signature=(info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
            cache_key=(signature,asset['sha256'])
            with self.lock:known=cache_key in self.verified
            if not known:
                value=hashlib.sha256()
                while chunk:=os.read(fd,1024*1024):value.update(chunk)
                after=os.fstat(fd)
                if value.hexdigest()!=asset['sha256'] or signature!=(after.st_dev,after.st_ino,after.st_size,after.st_mtime_ns,after.st_ctime_ns):
                    raise HTTPError(404,'asset_unavailable')
                with self.lock:
                    self.verified[cache_key]=True
                    while len(self.verified)>4096:self.verified.popitem(last=False)
            os.lseek(fd,0,os.SEEK_SET)
            stream=os.fdopen(fd,'rb');fd=None
            return stream
        except OSError:
            raise HTTPError(404,'asset_unavailable') from None
        finally:
            if fd is not None:os.close(fd)
            if directory is not None:os.close(directory)

    @staticmethod
    def selected(row):
        document=json.loads(row['release_metadata'])
        selection=app_storage.selections(document).get(row['target'])
        verified=document.get('verified',{}).get(row['target'],row['verified_at'])
        if not selection or not verified:return None
        if selection['repository_id']!=row['github_id'] or selection['release_id']!=row['release_id'] or selection['target']!=row['target']:
            raise HTTPError(503,'invalid_package_metadata')
        return selection

    def entry(self,row,selection):
        data=selection.get('package')
        if data is None:
            data=packages.metadata({'id':row['target'],'kind':row['format'],'version':selection['version']})
            data.update(runtime_dependencies=None,declared_imports=None,provides=None)
        if data['format']!=row['format'] or data['package_type']!=row['package_type']:
            raise HTTPError(503,'invalid_package_metadata')
        primary=data['artifact_name']
        files=[];sdk_profile=data['compatibility'].get('sdk_profile')
        package_id=packages.package_id(row['github_id'],row['target'])
        for asset in sorted(selection['files'],key=lambda item:item['name']):
            app_storage.number(asset['id'])
            with self.open_asset(row,asset) as stream:
                role='artifact' if asset['name']==primary else 'checksums' if asset['name']==row['target']+'.sha256' else 'build_receipt' if asset['name']=='r4os-build-'+row['target']+'.json' else 'support'
                if role=='build_receipt' and sdk_profile is None:
                    raw=stream.read(131073)
                    if len(raw)>131072:raise HTTPError(503,'invalid_package_metadata')
                    proof=json.loads(raw)
                    if proof.get('schema')!=2 or proof.get('target')!=row['target'] or proof.get('commit')!=selection['commit'] or proof.get('version')!=selection['version']:
                        raise HTTPError(503,'invalid_package_metadata')
                    sdk_profile=proof.get('sdk_profile')
            files.append({'asset_id':str(asset['id']),'name':asset['name'],'role':role,'size_bytes':asset['size'],
                          'sha256':asset['sha256'],'download_url':'/api/v1/packages/'+package_id+'/files/'+str(asset['id'])})
        if not any(asset['role']=='artifact' for asset in files):raise HTTPError(503,'invalid_package_metadata')
        installation=dict(data['installation'])
        if row['install_path_override'] is not None:installation['path']=row['install_path_override']
        sdk={key:sdk_profile.get(key) for key in ('sdk_id','sdk_version','profile')} if isinstance(sdk_profile,dict) else None
        return {'package_id':package_id,'package_type':row['package_type'],'target':row['target'],'format':row['format'],
                'display_name':row['display_name_override'] or row['manifest_name'],'description':row['description'],
                'category':json.loads(row['server_metadata']).get('category','Undefined'),
                'version':selection['version'],'release':{'id':str(selection['release_id']),'tag':selection['tag'],'commit':selection['commit']},
                'installation':installation,'compatibility':{**{k:data['compatibility'].get(k) for k in ('architecture','abi','os_version')},'sdk_profile':sdk},
                'runtime_dependencies':data.get('runtime_dependencies'),'declared_imports':data.get('declared_imports'),
                'provides':data.get('provides'),'assets':files}

    def snapshot(self):
        with self.connect() as db:rows=db.execute(QUERY+' AND f.current=1 ORDER BY p.repository_name').fetchall()
        found={};unavailable=set()
        for row in rows:
            key=packages.package_id(row['github_id'],row['target'])
            selection=self.selected(row)
            try:
                if not selection:unavailable.add(key);continue
                item=self.entry(row,selection)
            except HTTPError as error:
                if error.status!=404:raise
                unavailable.add(key);continue
            if key in found and found[key]!=item:raise HTTPError(409,'package_configuration_conflict','A listed package identity has conflicting metadata or releases.')
            found[key]=item;unavailable.discard(key)
        # Resolve a provider only when its declared exports satisfy the import.
        providers={}
        for item in found.values():
            provided=item.get('provides')
            if provided:providers.setdefault(provided['module_id'],[]).append(item)
        for item in found.values():
            for requirement in item['runtime_dependencies'] or []:
                choices=[p for p in providers.get(requirement['module_id'],[]) if
                         any(e['symbol']==requirement['symbol'] and e['abi_version']==requirement['abi_version'] for e in p['provides']['exports'] or [])]
                requirement['package_id']=choices[0]['package_id'] if len(choices)==1 else None
        return {'schema_version':1,'packages':[found[key] for key in sorted(found)],
                'unavailable':[{'package_id':key,'reason':'local_files_unavailable'} for key in sorted(unavailable) if key not in found]}

    def asset(self,package_id,asset_id):
        if not re.fullmatch(r'gh-[1-9][0-9]*-[A-Za-z0-9_.-]{1,128}',package_id) or not re.fullmatch(r'[1-9][0-9]{0,19}',asset_id):
            raise HTTPError(404,'asset_not_found')
        with self.connect() as db:rows=db.execute(QUERY+' ORDER BY p.repository_name').fetchall()
        found=None
        for row in rows:
            if packages.package_id(row['github_id'],row['target'])!=package_id:continue
            selection=self.selected(row)
            for asset in (selection or {}).get('files',[]):
                if str(asset['id'])!=asset_id:continue
                if found and (found[1]['sha256'],found[1]['size'],found[1]['name'])!=(asset['sha256'],asset['size'],asset['name']):
                    raise HTTPError(409,'package_configuration_conflict')
                found=(row,asset)
        if found is None:raise HTTPError(404,'asset_not_found')
        return found
