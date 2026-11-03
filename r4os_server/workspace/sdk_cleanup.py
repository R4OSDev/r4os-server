"""Remove inventoried legacy build installations after a verified SDK cutover."""
import json
from pathlib import Path
import shutil
import re

from . import sdk_packages, toolsets
from .store import Conflict, now


def inventory(store):
    active,manifest=toolsets.active(store)
    sdk_packages.get(store,manifest['sdk_id'])
    paths=[];obsolete_versions=set();retired_repositories=[]
    root=store.root/'BuildTools'
    for folder in sorted((root/'Toolsets').iterdir()):
        if not folder.is_dir() or folder==active:continue
        try:record=sdk_packages.strict_json((folder/'toolset.json').read_bytes())
        except (ValueError,OSError):continue  # Unknown content is not implicitly disposable.
        if record.get('schema')!=2:
            paths.append(folder)
            if record.get('sources',{}).get('SDK',{}).get('version'):obsolete_versions.add(record['sources']['SDK']['version'])
    for name in ('Metadata','ReleaseTools','Boot'):
        directory=root/name
        if directory.is_dir():paths.append(directory)
    with store.connect() as db:
        jobs={row['id']:json.loads(row['payload']) for row in db.execute('SELECT id,payload FROM jobs')}
    for folder in sorted((root/'Workspaces').glob('*')):
        payload=jobs.get(folder.name)
        source=folder/'Repositories/SDK/build.zig.zon'
        version=None
        try:
            if source.is_file():
                match=re.search(r'\.version\s*=\s*"([^"]+)"',source.read_text())
                version=match[1] if match else None
        except OSError:pass  # An operator-private/unknown workspace is not implicitly disposable.
        if folder.is_dir() and ((payload is not None and not payload.get('sdk_id')) or version in obsolete_versions):paths.append(folder)
    for repo in store.repositories(enabled_only=False):
        if repo['connection_id']=='main' and repo['display_name']=='r4os-devkit':
            path=store.root/'Repositories'/repo['directory']
            if path.is_dir():paths.append(path);retired_repositories.append(repo['name'])
    # Cache/download directories from the removed installers have fixed roles.
    # SDK caches live under Cache/SDK and the installed environment is self-contained.
    for name in (('Cache/Rust','Cache/Zig','Downloads') if obsolete_versions else ()):
        directory=root/name
        if directory.is_dir():paths.append(directory)
    return {'active':manifest['id'],'sdk_id':manifest['sdk_id'],'paths':[str(p.relative_to(store.root)) for p in paths],'obsolete_sdk_versions':sorted(obsolete_versions),'retired_repositories':retired_repositories}


def cleanup(context):
    store=context.store
    tools,manifest=toolsets.active(store)
    toolsets.validate(context,tools,manifest)
    with store.connect() as db:
        pending=db.execute("SELECT id FROM jobs WHERE status IN ('queued','running') AND id<>? AND kind NOT IN ('check','catalog','github_status')",(context.job['id'],)).fetchall()
    if pending:raise Conflict('Finish or cancel other SDK/build/source jobs before removing old installations.')
    report=inventory(store)
    for name in report['retired_repositories']:
        repo=store.repository(name);path=store.root/'Repositories'/repo['directory']
        dirty,_=context.git(path,'status','--porcelain=v1','--untracked-files=normal')
        if dirty.strip():raise Conflict('The retired DevKit checkout contains local changes; preserve them before cleanup.')
    # Ensure the new environment does not link back into an installation to delete.
    removed=[store.root/path for path in report['paths']]
    for link in tools.rglob('*'):
        if link.is_symlink() and any(link.resolve().is_relative_to(path.resolve()) for path in removed):
            raise Conflict('The new SDK environment still references an obsolete installation: '+str(link.relative_to(store.root)))
    for path in removed:
        context.phase('Removing obsolete build installation: '+str(path.relative_to(store.root)))
        if path.is_symlink():raise Conflict('Unexpected symlink in cleanup inventory.')
        shutil.rmtree(path)
    # Retain historical job/receipt facts. Only operational legacy settings expire.
    with store.connect() as db:
        db.execute("DELETE FROM kv WHERE key IN ('boot_tools','metadata_parser','toolset_sources')")
        for name in report['retired_repositories']:
            db.execute("UPDATE repositories SET enabled=0,local='{}' WHERE name=?",(name,))
    report['completed_at']=now()
    sdk_packages.write_json(store.state/'sdk-cleanup.json',report)
    sdk_packages.publish_index(store)
    return report
