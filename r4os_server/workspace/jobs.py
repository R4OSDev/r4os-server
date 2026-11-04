"""Private read-only aggregation of the existing background-work queues."""
import json
import sqlite3
from http.client import HTTPException

from .process import scrub


KINDS = {'refresh':'Repository refresh', 'catalog':'Catalogue refresh', 'refresh_all':'Refresh all repositories',
         'download':'Download repository', 'update':'Update repository', 'download_all':'Download repositories',
         'update_all':'Update repositories', 'check':'Check repository', 'github_status':'Artifact check',
         'build':'Build', 'build_publish':'Build and publish', 'release':'Build release',
         'app_publish':'Show in Apps', 'connection_delete':'Remove connection',
         'app_refresh':'App refresh','app_download':'Download app','app_update':'Update app','app_remove_local':'Remove local app files'}


def workspace_job(job):
    kind, payload = job['kind'], job['payload']
    area = ('SDK / Tools' if kind.startswith(('sdk_','tools_')) else 'Apps' if kind.startswith('app_')
            else 'Builds' if kind in ('build','build_publish','release') else 'Repositories')
    title = KINDS.get(kind, kind.replace('_',' ').capitalize())
    release=payload.get('release')
    target = payload.get('app_label') or payload.get('repository_label') or (release.get('name') if isinstance(release,dict) else '') or job['subject']
    return {'key':'workspace:'+job['id'], 'id':job['id'], 'source':'workspace', 'area':area,
            'title':title, 'target':scrub(target or ''), 'status':job['status'],
            'phase':'Cancellation requested' if job['cancel'] else scrub(job['phase']),
            'created':job['created'], 'started':job['started'], 'progress':job['result'].get('progress'),
            'cancellable':not job['cancel'] and not job['result'].get('activation_committed') and not job['result'].get('github_release',{}).get('published'),
            'log':True, 'details':False}


def update_job(item):
    return {'key':'updates:'+item['id'], 'id':item['id'], 'source':'updates', 'area':'Updates',
            'title':'Package upload' if item['kind']=='package' else 'Release manifest upload',
            'target':item.get('filename') or item['id'][:8], 'status':item['status'],
            'phase':{'receiving':'Receiving upload', 'queued':'Waiting for validation', 'validating':'Validating upload'}[item['status']],
            'created':item['created_at'], 'started':None, 'progress':None,
            'cancellable':False, 'log':False, 'details':True}


def snapshot(handler):
    jobs = []
    sources = {'workspace':False, 'updates':False}
    try:
        jobs = [workspace_job(job) for job in handler.server.workspace.active_jobs()]
        sources['workspace'] = True
    except sqlite3.Error:
        pass
    connection = None
    try:
        connection = handler.backend_connection(handler.server.config, handler.server.config.updates_port, timeout=1.5)
        connection.request('GET','/api/updates/jobs',headers={'Authorization':'Bearer '+handler.server.config.admin_token})
        response = connection.getresponse()
        raw = response.read(1024*1024+1)
        if response.status != 200 or len(raw)>1024*1024:
            raise ValueError('Update jobs unavailable')
        data = json.loads(raw)
        jobs.extend(update_job(item) for item in data['items'])
        sources['updates'] = True
    except (OSError, HTTPException, ValueError, KeyError, TypeError):
        pass  # A failed service is not evidence for an empty queue.
    finally:
        if connection is not None: connection.close()
    jobs.sort(key=lambda job:(job['created'],job['key']))
    complete = all(sources.values())
    return {'jobs':jobs, 'count':len(jobs) if complete else None, 'known_count':len(jobs),
            'complete':complete, 'sources':sources}
