"""Installed SDK-owned build environments; no independent tool or component pins."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import tarfile
import time
import urllib.request
import uuid

from . import catalog, sdk_packages
from .store import identifier, now, under

def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
    scratch.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    scratch.replace(path)


def download(context, url, destination, expected=None):
    if not url.startswith("https://"):
        raise ValueError("Tool downloads require HTTPS.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and expected and digest(destination) == expected:
        return destination
    scratch = destination.with_name(destination.name + ".part")
    context.phase("Downloading: " + destination.name)
    request = urllib.request.Request(url, headers={"User-Agent": "R4OS-Server"})
    try:
        count, last = 0, 0
        with urllib.request.urlopen(request, timeout=60) as response, scratch.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                context.check()
                target.write(chunk)
                count += len(chunk)
                if count > 4 * 1024**3:
                    raise RuntimeError("The installation archive exceeds the size limit.")
                if time.monotonic() - last > 5:
                    context.phase(destination.name + ": " + str(count // 1048576) + " MiB downloaded")
                    last = time.monotonic()
        if expected and digest(scratch) != expected:
            raise RuntimeError("The installation archive SHA-256 does not match the version pin.")
        scratch.replace(destination)
    finally:
        scratch.unlink(missing_ok=True)
    return destination


def clone_snapshot(context, original, destination, commit):
    # Local snapshots still honour a managed source's current access/selection.
    try:
        relative = Path(original).resolve().relative_to((context.store.root / "Repositories").resolve()).as_posix()
    except ValueError:
        relative = None
    if relative:
        with context.store.connect() as db:
            row = db.execute("SELECT name FROM repositories WHERE directory=?", (relative,)).fetchone()
        if row:
            context.store.require_repository(row["name"])
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (destination / ".git").is_dir():
        head, _ = context.git(destination, "rev-parse", "HEAD")
        dirty, _ = context.git(destination, "status", "--porcelain=v1", "--untracked-files=no")
        if head.strip() != commit or dirty.strip():
            raise ValueError("An existing tool snapshot has been modified: " + str(destination))
        return
    context.run(["git", "clone", "--no-hardlinks", "--no-checkout", "--", original, destination], quiet=True, timeout=300)
    context.git(destination, "checkout", "--detach", commit)
    context.git(destination, "remote", "set-url", "--push", "origin", "disabled://local-only")


def load(store, key):
    path = under(store.root/'BuildTools/Toolsets', identifier(key))
    try: manifest = sdk_packages.strict_json((path/'toolset.json').read_bytes())
    except (OSError, ValueError): raise ValueError('This SDK environment is not installed.') from None
    if manifest.get('schema') != 2 or not manifest.get('sdk_id'):
        raise ValueError('This environment uses the removed SDK contract. Set up a verified SDK package.')
    if manifest.get('status') != 'ready': raise ValueError('This SDK environment has not passed setup checks.')
    package = sdk_packages.get(store, manifest['sdk_id'])
    if manifest.get('sdk_profile_sources') != sdk_packages.provenance(package)['profile_sources']:
        raise ValueError('The installed environment belongs to different SDK profiles.')
    return path, manifest


def active(store):
    key = store.setting('active_toolset')
    if not key: raise ValueError('Select and set up a verified SDK under Repositories → Tools.')
    return load(store, key)


def package_paths(store, manifest):
    folder = sdk_packages.root(store, manifest['sdk_id'])/'Content'
    return {'SDK': folder/'SDK', 'Contract': folder/'CONTRACT', 'Libraries': folder/'LIBRARIES',
            'Recovery': folder/'RECOVERY', 'HostTools': folder/'Tools'}


def prepare_metadata_parser(context, sdk_id=None):
    manifest = {'sdk_id':sdk_id} if sdk_id else active(context.store)[1]
    profile = sdk_packages.get(context.store, manifest['sdk_id'])['manifest']
    tools = package_paths(context.store, manifest)['HostTools']
    # The signed package's unmodified owner catalog is used directly.
    metadata = sdk_packages.strict_json((tools/'RustToolsProfile.json').read_bytes())
    host = 'Linux' if platform.system() == 'Linux' else 'Windows'
    relative = metadata['hosts'][host]['catalog']
    return under(tools, relative)


def refresh_profiles(context):
    context.phase('Refreshing repository profiles with the selected SDK')
    for repo in context.store.repositories():
        if repo['available'] and repo['local'].get('commit'):
            try: catalog.inspect(context, repo['name'], remote=False)
            except (OSError, ValueError, RuntimeError) as error:
                context.log(repo['name']+': repository profile refresh failed: '+str(error))


def overview(store):
    sets = []
    for file in sorted((store.root/'BuildTools/Toolsets').glob('*/toolset.json')):
        try:
            item = sdk_packages.strict_json(file.read_bytes())
            if item.get('schema') == 2 and item.get('sdk_id'): sets.append(item)
        except (OSError, ValueError): continue
    key = store.setting('active_toolset')
    current = next((item for item in sets if item['id'] == key and item['status'] == 'ready'), None)
    packages = [sdk_packages.summary(item) for item in sdk_packages.packages(store)]
    selected = next((item for item in packages if current and item['id'] == current['sdk_id']), None)
    components = []
    if selected:
        metadata = sdk_packages.get(store, selected['id'])
        for name, tool in metadata['manifest']['tools'].items():
            version = tool.get('version') or tool.get('rust_version')
            components.append({'id':name,'name':name,'required':version or 'SDK-defined host requirements',
                               'installed':(current or {}).get('versions',{}).get(name),
                               'state':'Ready' if name in (current or {}).get('installed_tools',[]) else 'Available from SDK',
                               'path':str(store.root/'BuildTools/Toolsets'/key/'Install') if current else '', 'host':False})
    return {'active':current['id'] if current else None, 'sdk':selected, 'packages':packages, 'toolsets':sets,
            'components':components, 'host':platform.system()+' '+platform.machine(),
            'errors':[] if current else ['A verified SDK package must be selected and set up.'],
            'jobs':store.jobs('tools')+store.jobs('sdk')}


def validate(context, path, manifest):
    package = sdk_packages.get(context.store, manifest['sdk_id'])
    context.phase('Checking SDK-owned tools and exact source bindings')
    sdk_packages.verify(context, package['id'])
    run_setup(context, package['id'], path/'Install', manifest['installed_tools'], check=True)
    return {'ready':True,'toolset':manifest['id'],'sdk_profile':sdk_packages.provenance(package)}


def run_setup(context, sdk_id, install_root, names, *, check=False):
    script = sdk_packages.root(context.store, sdk_id)/'Content/SDK/Tools/Setup.ps1'
    quote_ps = lambda value: "'"+str(value).replace("'", "''")+"'"
    command = '& '+quote_ps(script)+' -InstallRoot '+quote_ps(install_root)+' -Tools @('+','.join(quote_ps(name) for name in names)+')'+(' -Check' if check else '')
    return context.run(['pwsh','-NoLogo','-NoProfile','-Command',command],timeout=3600)


def install(context, value):
    if not isinstance(value, dict) or not value.get('sdk_id') or set(value)-{'sdk_id','tools'}:
        raise ValueError('Choose a verified sdk_id and optional SDK tool IDs.')
    store = context.store;package = sdk_packages.get(store, value['sdk_id'])
    names = value.get('tools', ['zig','rust'])
    if not isinstance(names, list) or not names or len(names) != len(set(names)) or any(name not in package['manifest']['tools'] for name in names):
        raise ValueError('Choose tool IDs declared by this SDK.')
    # Setup.ps1 is the owner for its supported bootstrap tools. Other declared
    # graphics/shader/host requirements are checked by their SDK owner builders.
    if any(name not in ('zig','rust','limine','qemu') for name in names):
        raise ValueError('This SDK tool is provisioned by its native owner builder, not Tools/Setup.ps1.')
    key = 'sdk-'+package['id']+'-'+platform.system().lower()
    path = under(store.root/'BuildTools/Toolsets', key);path.mkdir(parents=True, exist_ok=True)
    manifest_path = path/'toolset.json'
    previous = sdk_packages.strict_json(manifest_path.read_bytes()) if manifest_path.exists() else {}
    if previous.get('status') == 'ready' and set(names).issubset(previous['installed_tools']):
        validate(context,path,previous)
        with store.connect() as db:
            db.executemany('INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           [('active_toolset',json.dumps(key)),('active_sdk',json.dumps(package['id']))])
        refresh_profiles(context)
        return {'active':key,'sdk_id':package['id'],'unchanged':True}
    if previous.get('status')=='ready':names=sorted(set(names)|set(previous['installed_tools']))
    paths = package_paths(store, {'sdk_id':package['id']})
    manifest = {'schema':2,'id':key,'status':'installing','sdk_id':package['id'],
                'sdk_profile_sources':sdk_packages.provenance(package)['profile_sources'],'created':now(),
                'installed_tools':names,'versions':{},'sources':{'SDK':{'repository':'r4os-sdk','version':package['manifest']['sdk_version'],
                'commit':package['manifest']['sdk_source']['commit']}}}
    if previous.get("status") != "ready":write_json(manifest_path,manifest)
    try:
        sdk_packages.verify(context,package['id'])
        context.phase('Installing the exact tools declared by the SDK')
        run_setup(context, package['id'], path/'Install', names)
        manifest['versions']={name:package['manifest']['tools'][name].get('version') or package['manifest']['tools'][name].get('rust_version') for name in names}
        version,_=context.run(['pwsh','--version'],quiet=True);manifest['versions']['pwsh']=version.strip()
        for role,source_id in (('Contract','contract'),('Libraries','libraries')):
            source=package['manifest']['sources'][source_id]
            manifest['sources'][role]={'repository':Path(source['repository']).stem,'url':source['repository'],'version':source['version'],'commit':source['commit']}
        manifest.update(status='checking',installed=now(),capabilities=package['capabilities'])
        write_json(manifest_path,manifest)
        validate(context,path,manifest)
        manifest['status']='ready';write_json(manifest_path,manifest);context.check()
        with store.connect() as db:
            db.executemany('INSERT INTO kv VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                           [('active_toolset',json.dumps(key)),('active_sdk',json.dumps(package['id']))])
        refresh_profiles(context)
        return {'active':key,'sdk_id':package['id'],'versions':manifest['versions']}
    except BaseException as error:
        if previous.get('status')=='ready':write_json(manifest_path,previous)
        elif store.setting('active_toolset') != key:
            manifest.update(status='failed',error=str(error)[-4000:]);write_json(manifest_path,manifest)
        raise


def perform(context, action, payload):
    if platform.system() != 'Linux' or platform.machine() != 'x86_64': raise ValueError('SDK builds require a supported Linux x64 host.')
    if action in ('install','update'): return install(context,payload)
    if action == 'activate':
        if not isinstance(payload,dict) or set(payload) != {'id'}: raise ValueError('Choose an installed SDK environment.')
        path,manifest=load(context.store,payload['id']);validate(context,path,manifest)
        context.store.set('active_toolset',manifest['id']);context.store.set('active_sdk',manifest['sdk_id']);refresh_profiles(context)
        return {'active':manifest['id'],'sdk_id':manifest['sdk_id']}
    if action == 'check':
        path,manifest=active(context.store);return validate(context,path,manifest)
    raise ValueError('Unknown SDK tool action.')
