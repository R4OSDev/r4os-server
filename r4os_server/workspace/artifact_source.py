"""Read production target metadata from an immutable GitHub source revision."""
import base64
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
import tempfile
import time
from urllib.parse import quote

from . import build_targets, catalog, profiles
from .store import under


class EmptySource(ValueError):
    pass


def canonical(repo):
    return repo.get('clone_url')=='https://github.com/'+repo['owner']+'/'+repo['display_name']+'.git'


def head(context,repo,client):
    try:age=time.time()-datetime.fromisoformat(repo['local'].get('checked_at','')).timestamp()
    except (TypeError,ValueError):age=999999
    if (context.job['kind']=='refresh' and 0<=age<30 and repo['local'].get('remote_commit')
            and not repo['local'].get('dirty') and repo['local'].get('branch')==repo['default_branch']
            and repo['local'].get('status') in ('current','outdated','ahead','diverged')):
        commit=repo['local']['remote_commit']
    elif canonical(repo):
        raw,code=context.repository_git(repo,'ls-remote','--exit-code',repo['clone_url'],
            'refs/heads/'+repo['default_branch'],quiet=True,timeout=60,check=False)
        if code==2 and not raw.strip():raise EmptySource('GitHub default branch has no source commit. Refresh catalogue metadata if the branch changed.')
        if code or not raw.strip():raise RuntimeError('GitHub default-branch source status could not be read.')
        commit=raw.split()[0]
    else:commit=client.request('GET','/commits/'+quote(repo['default_branch'],safe=''))['sha']
    if not isinstance(commit,str) or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}',commit):
        raise ValueError('GitHub did not return a complete source commit.')
    return commit


def tag(context,repo,version):
    name='refs/tags/v'+version
    raw,code=context.repository_git(repo,'ls-remote','--tags','--exit-code',repo['clone_url'],name,name+'^{}',quiet=True,timeout=60,check=False)
    if code==2:return None
    if code:raise RuntimeError('GitHub release tag status could not be read.')
    values={line.split()[1]:line.split()[0] for line in raw.splitlines() if len(line.split())==2}
    return values.get(name+'^{}') or values.get(name)


def targets(context,repo,client,commit):
    store=context.store;root=catalog.repository_path(store,repo)
    local=root.exists() and commit in (repo['local'].get('commit'),repo['local'].get('remote_commit'))
    if local:entries=build_targets.tree(store,repo,commit)
    else:
        tree=client.request('GET','/git/trees/'+commit+'?recursive=1')
        if tree.get('truncated') is not False or not isinstance(tree.get('tree'),list):
            raise ValueError('GitHub did not return a complete source tree.')
        entries=tree['tree']
    if len(entries)>100000:raise ValueError('The source tree is too large to inspect safely.')
    contents={};total=0
    with tempfile.TemporaryDirectory(prefix='artifact-metadata-',dir=store.state) as folder:
        for entry in entries:
            context.check();relative=entry['path'];path=PurePosixPath(relative)
            if any(part.lower() in build_targets.NONPRODUCTION for part in path.parts):continue
            manifest=relative.endswith('.R4MF')
            native=repo['display_name']=='r4os-libraries' and len(path.parts)==3 and path.parts[1:]==('Tools','Build.ps1')
            if not (manifest or relative in ('Cargo.toml','build.zig.zon') or native):continue
            if entry.get('type')!='blob' or entry.get('mode') not in ('100644','100755'):
                raise ValueError('Source metadata must be a regular Git file: '+relative)
            if len(contents)>=512:raise ValueError('Too many production manifests.')
            if native:text=''
            elif local:text=build_targets.blob(store,repo,commit,relative)
            else:
                document=client.request('GET','/git/blobs/'+entry['sha'])
                if document.get('encoding')!='base64' or document.get('size',1048577)>1048576:
                    raise ValueError('GitHub returned invalid source metadata.')
                text=base64.b64decode(''.join(document['content'].split()),validate=True).decode('utf-8-sig')
            if text is None:raise ValueError('Source metadata changed while reading the pinned revision.')
            total+=len(text.encode('utf-8'))
            if total>2*1024**2 or manifest and len(text.encode('utf-8'))>65536:
                raise ValueError('Production metadata exceeds its size limit.')
            target=under(Path(folder),relative);target.parent.mkdir(parents=True,exist_ok=True);target.write_text(text)
            contents[relative]=text
        discovered=profiles.scan(folder,repo['display_name'],archive=True)
        if discovered['profile']['errors']:raise ValueError('; '.join(discovered['profile']['errors'])[:2000])
        result=[]
        for module in build_targets.targets({'local':discovered}):
            try:item=build_targets.versioned(module,contents.get);item['version_valid']=True
            except ValueError as error:item=dict(module,version_valid=False,reason=str(error))
            result.append(item)
        return result
