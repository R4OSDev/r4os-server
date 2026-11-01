"""Durable streaming uploads, bounded validation and coordinated publication."""
from concurrent.futures import ThreadPoolExecutor
import errno
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import time
from uuid import uuid4

from ..common.http import HTTPError
from .r4u import inspect_package, immutable_package_filename
from .release_manifest import inspect_release_manifest, immutable_release_filename
from .state import PackageCache


def sync_directory(path):
    descriptor=os.open(path,os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class Uploads:
    def __init__(self,config,catalog,access):
        self.config,self.catalog,self.access=config,catalog,access
        self.store=catalog.store
        self.staging=config.data_dir/"updates/staging"
        self.staging.mkdir(parents=True,exist_ok=True)
        self.executor=ThreadPoolExecutor(max_workers=config.validation_workers,thread_name_prefix="update-validation")
        with self.store._connect() as connection:
            connection.executescript("""BEGIN IMMEDIATE;
                CREATE TABLE IF NOT EXISTS uploads (
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,filename TEXT,
                    size INTEGER NOT NULL,sha256 TEXT,metadata_json TEXT,error TEXT,
                    created_at TEXT NOT NULL,updated_at TEXT NOT NULL,source_ip TEXT NOT NULL,actor TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS uploads_status ON uploads(status,created_at);
                PRAGMA user_version=2; COMMIT;""")
            rows=connection.execute("SELECT * FROM uploads WHERE status IN ('receiving','queued','validating','valid')").fetchall()
        for row in rows:
            identifier=row['id']
            if row['status']=='receiving':
                self.update(identifier,status='invalid',error='Upload durch Dienstneustart unterbrochen')
                for suffix in ('.part','.data'):
                    (self.staging/(identifier+suffix)).unlink(missing_ok=True)
            elif row['status'] in ('queued','validating'):
                self.update(identifier,status='queued')
                self.executor.submit(self.validate,identifier)
            elif row['filename'] and (catalog.release_root/row['filename']).is_file():
                self.executor.submit(self.reconcile,identifier)
        known={row['id'] for row in rows}
        for path in self.staging.glob('*.part'):
            if path.stem not in known:
                path.unlink(missing_ok=True)

    @staticmethod
    def moment():
        return time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime())

    def update(self,identifier,**values):
        assert set(values)<= {'status','filename','size','sha256','metadata_json','error'}
        values['updated_at']=self.moment()
        with self.store._connect() as connection:
            connection.execute('UPDATE uploads SET '+','.join(key+'=?' for key in values)+' WHERE id=?',(*values.values(),identifier))

    @staticmethod
    def document(row):
        data=dict(row)
        raw=data.pop('metadata_json')
        data['metadata']=json.loads(raw) if raw else None
        data['valid']=data['status'] in ('valid','published')
        return data

    def get(self,identifier):
        with self.store._connect() as connection:
            row=connection.execute('SELECT * FROM uploads WHERE id=?',(identifier,)).fetchone()
        if row is None:
            raise HTTPError(404,'upload_not_found')
        return self.document(row)

    def list(self,limit=200,offset=0,kind=None,exclude_published=False):
        if not 1<=limit<=200 or not 0<=offset<=2**31-1:
            raise HTTPError(422,'invalid_pagination')
        if kind is not None and kind not in ('package','manifest'):
            raise HTTPError(422,'invalid_kind')
        clauses=[];values=[]
        if kind is not None:clauses.append('kind=?');values.append(kind)
        if exclude_published:clauses.append("status!='published'")
        where=' WHERE '+' AND '.join(clauses) if clauses else ''
        with self.store._connect() as connection:
            total=connection.execute('SELECT count(*) FROM uploads'+where,values).fetchone()[0]
            rows=connection.execute('SELECT * FROM uploads'+where+' ORDER BY created_at DESC,rowid DESC LIMIT ? OFFSET ?',(*values,limit,offset)).fetchall()
        return {'items':[self.document(row) for row in rows],'total':total,'limit':limit,'offset':offset}

    def counts(self):
        with self.store._connect() as connection:
            return {row['status']:row['total'] for row in connection.execute('SELECT status,count(*) total FROM uploads GROUP BY status')}

    def receive(self,handler,kind,actor):
        maximum=self.config.upload_limit if kind=='package' else 1024*1024
        content_type='application/octet-stream' if kind=='package' else 'application/json'
        length=handler.body_length(maximum,content_type)
        if not length:
            raise HTTPError(422,'empty_upload')
        if shutil.disk_usage(self.staging).free < length+16*1024**2:
            raise HTTPError(507,'insufficient_storage')
        identifier=uuid4().hex
        source=handler.source_ip()
        with self.store._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            active=connection.execute("SELECT count(*) FROM uploads WHERE status IN ('receiving','queued','validating')").fetchone()[0]
            if active>=100:
                raise HTTPError(429,'validation_queue_full')
            connection.execute('INSERT INTO uploads VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (identifier,kind,'receiving',None,length,None,None,None,self.moment(),self.moment(),source,actor))
        part=self.staging/(identifier+'.part')
        complete=self.staging/(identifier+'.data')
        digest=hashlib.sha256();remaining=length;started=time.monotonic()
        try:
            with part.open('xb') as destination:
                while remaining:
                    if time.monotonic()-started>self.config.upload_timeout:
                        raise HTTPError(408,'upload_timeout')
                    chunk=handler.rfile.read(min(1024*1024,remaining))
                    if not chunk:
                        raise HTTPError(400,'incomplete_upload')
                    destination.write(chunk);digest.update(chunk);remaining-=len(chunk)
                destination.flush();os.fsync(destination.fileno())
            os.replace(part,complete)
            sync_directory(self.staging)
            self.update(identifier,status='queued',sha256=digest.hexdigest())
            self.access.audit('upload_received',source,identifier+' '+kind,'updates')
            self.executor.submit(self.validate,identifier)
            return self.get(identifier)
        except (HTTPError,OSError) as error:
            part.unlink(missing_ok=True);complete.unlink(missing_ok=True)
            self.update(identifier,status='invalid',error='Transfer incomplete or cancelled')
            self.access.audit('upload_failed',source,identifier,'updates')
            if isinstance(error,HTTPError):
                raise
            if isinstance(error,TimeoutError):
                raise HTTPError(408,'request_timeout') from None
            if error.errno in (errno.ENOSPC,errno.EDQUOT):
                raise HTTPError(507,'insufficient_storage') from None
            raise HTTPError(400,'upload_interrupted') from None

    def validate(self,identifier):
        upload=self.get(identifier)
        path=self.staging/(identifier+'.data')
        self.update(identifier,status='validating',error=None)
        try:
            if upload['kind']=='package':
                metadata=inspect_package(path).to_dict()
                filename=immutable_package_filename(metadata)
                cache_state='r4u2-incoming'
            else:
                metadata=inspect_release_manifest(path).to_dict()
                filename=immutable_release_filename(metadata['release'])
                cache_state='release1-incoming'
            if metadata['sha256']!=upload['sha256']:
                raise ValueError('File contents changed after transfer')
            with self.catalog.mutation():
                released=self.catalog.release_root/filename
                incoming=self.catalog.incoming_root/filename
                target=released if released.exists() else incoming
                if target.exists():
                    if target.is_symlink() or self.hash_file(target)!=upload['sha256']:
                        raise ValueError('An immutable filename is already bound to different contents')
                else:
                    os.link(path,incoming,follow_symlinks=False)
                    sync_directory(self.catalog.incoming_root)
                published=released.exists()
                stat=target.stat()
                self.store.package_cache_put(PackageCache(cache_state.replace('incoming','released') if published else cache_state,filename,stat.st_size,stat.st_mtime_ns,metadata,None,time.time() if published else None))
                self.update(identifier,status='published' if published else 'valid',filename=filename,metadata_json=json.dumps(metadata,ensure_ascii=False))
                path.unlink(missing_ok=True)
            self.access.audit('validation_valid',upload['source_ip'],identifier+' '+filename,'updates')
        except Exception as error:
            self.update(identifier,status='invalid',error=str(error)[:4096])
            self.access.audit('validation_invalid',upload['source_ip'],identifier+' '+str(error)[:512],'updates')
            logging.warning('Update validation failed: %s (%s)',identifier,type(error).__name__)

    @staticmethod
    def hash_file(path):
        with path.open('rb') as stream:
            return hashlib.file_digest(stream,'sha256').hexdigest()

    def reconcile(self,identifier):
        upload=self.get(identifier)
        path=self.catalog.release_root/upload['filename']
        if self.hash_file(path)==upload['sha256']:
            self.update(identifier,status='published')

    def readiness(self,upload):
        if upload['status']=='published':
            return {'status':'published','reason':None}
        if upload['status']!='valid':
            return {'status':'pending' if upload['status'] in ('queued','validating','receiving') else 'blocked',
                    'reason':upload['error'] or 'A completed, valid check is required.'}
        try:
            metadata=upload['metadata']
            if any(item.metadata and item.metadata.get('release')==metadata['release'] for item in self.catalog.list_released_release_manifests()):
                raise ValueError('This release is already complete.')
            if upload['kind']=='manifest':
                candidate=self.catalog._entry_from_metadata(upload['filename'],upload['size'],metadata)
                self.catalog._validate_release(candidate,self.catalog.list_released())
            elif any(item.metadata and item.metadata['release']==metadata['release'] and item.metadata['package'].upper()==metadata['package'].upper() for item in self.catalog.list_released()):
                raise ValueError('This package ID is already published in the release.')
        except ValueError as error:
            return {'status':'blocked','reason':str(error)}
        return {'status':'ready','reason':None}

    def publish(self,identifier,source):
        with self.catalog.mutation():
            upload=self.get(identifier)
            if upload['status']=='published':
                return upload
            if upload['status']!='valid':
                raise HTTPError(409,'upload_not_ready','Only publish fully validated uploads')
            try:
                if upload['kind']=='package':
                    self.catalog.publish(upload['filename'])
                else:
                    self.catalog.publish_release_manifest(upload['filename'])
            except (ValueError,FileNotFoundError,FileExistsError) as error:
                self.access.audit('publication_rejected',source,identifier+' '+str(error)[:512],'updates')
                raise HTTPError(409,'publication_conflict',str(error)) from None
            with self.store._connect() as connection:
                connection.execute("UPDATE uploads SET status='published',updated_at=? WHERE filename=? AND sha256=? AND status='valid'",(self.moment(),upload['filename'],upload['sha256']))
            self.access.audit('published',source,identifier+' '+upload['filename'],'updates')
            return self.get(identifier)

    def close(self):
        self.executor.shutdown(wait=True,cancel_futures=False)
