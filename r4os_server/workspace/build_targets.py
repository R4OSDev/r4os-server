"""Production targets and versions read from the exact source commit being built."""
import json
from pathlib import PurePosixPath
import re
import subprocess
import tomllib

from . import catalog
from .process import clean_env, scrub

VERSION = re.compile(r'[0-9]+\.[0-9]+\.[0-9]+\Z')
NONPRODUCTION = {'tests', 'test', 'fixtures', 'smoke', 'examples', 'templates', 'thirdparty', 'legal'}


def targets(repo):
    result = []
    for module in repo['local'].get('modules', []):
        path = module.get('manifest', '')
        if any(part.lower() in NONPRODUCTION for part in PurePosixPath(path).parts):
            continue
        version = module.get('version')
        if not path:
            profile = repo['local'].get('profile', {})
            version = (profile.get('cargo_package_version') or profile.get('package_version')) if module.get('language') == 'Rust' else (profile.get('package_version') or profile.get('cargo_package_version'))
        item = dict(module, version=version or '')
        item['version_valid'] = bool(VERSION.fullmatch(item['version']))
        result.append(item)
    return sorted(result, key=lambda item: (item.get('manifest') != 'module.R4MF', item['id']))


def blob(store, repo, commit, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts or '\0' in str(path):
        raise ValueError('Invalid build manifest path.')
    if not isinstance(commit, str) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})', commit):
        raise ValueError('A complete pinned Git source commit is required.')
    root = catalog.repository_path(store, repo)
    if (not root.is_dir() or root.is_symlink() or (root/'.git').is_symlink()
            or not (root/'.git').is_dir() or not root.resolve().is_relative_to((store.root/'Repositories').resolve())):
        raise ValueError('The managed Git checkout is missing or invalid. Refresh or download the repository.')
    # Checkouts belong to the build worker. Trust only this validated path for
    # these object reads; keep system/global config, hooks and credentials out.
    command = ['git', '--no-replace-objects', '--literal-pathspecs', '-c', 'safe.directory='+str(root), '-C', str(root)]

    def read(*arguments):
        try:
            result = subprocess.run(command+list(arguments), env=clean_env(), capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            raise ValueError('Git timed out while reading the pinned build source.') from None
        except OSError:
            raise ValueError('Git could not be executed to read the pinned build source.') from None
        if result.returncode:
            detail = ' '.join(scrub(result.stderr.decode('utf-8', 'replace')).split())[:1200]
            raise ValueError('Could not read the pinned build source from the managed repository: '+(detail or 'Git failed.'))
        return result.stdout

    if read('cat-file', '-t', commit).strip() != b'commit':
        raise ValueError('The pinned build source is not a Git commit.')
    entry = read('ls-tree', '-z', '--full-tree', commit, '--', str(path))
    if not entry:
        return None  # An absent optional version file is not a Git access error.
    entries = entry.rstrip(b'\0').split(b'\0')
    metadata, name = entries[0].split(b'\t', 1)
    mode, kind, object_id = metadata.split()
    if len(entries) != 1 or name != str(path).encode('utf-8') or kind != b'blob' or mode not in (b'100644', b'100755'):
        raise ValueError('The pinned build manifest must be a regular Git file: '+str(path))
    if int(read('cat-file','-s',object_id.decode('ascii'))) > 1024**2:
        raise ValueError('The pinned build metadata file is too large: '+str(path))
    try:
        return read('cat-file', 'blob', object_id.decode('ascii')).decode('utf-8-sig')
    except UnicodeError:
        raise ValueError('The pinned build manifest is not valid UTF-8: '+str(path)) from None


def pinned(store, repo, module, commit):
    return versioned(module, lambda relative: blob(store, repo, commit, relative))


def tree(store, repo, commit):
    # Reuse exactly the path/ownership/commit validation of pinned blob reads.
    blob(store,repo,commit,'module.R4MF')
    root=catalog.repository_path(store,repo)
    try:
        result=subprocess.run(['git','--no-replace-objects','-c','safe.directory='+str(root),'-C',str(root),
                               'ls-tree','-rz','--full-tree',commit],env=clean_env(),capture_output=True,timeout=20)
    except (OSError,subprocess.TimeoutExpired):raise ValueError('Could not read the Git source tree.') from None
    if result.returncode or len(result.stdout)>16*1024**2:
        raise ValueError('Could not read the complete Git source tree: '+scrub(result.stderr.decode('utf-8','replace'))[:800])
    entries=[]
    for raw in result.stdout.rstrip(b'\0').split(b'\0'):
        if not raw:continue
        metadata,path=raw.split(b'\t',1);mode,kind,sha=metadata.decode().split()
        entries.append({'path':path.decode('utf-8'),'mode':mode,'type':kind,'sha':sha})
    return entries


def versioned(module, read):
    """Never use a stale UI version to choose the immutable release tag."""
    item = dict(module)
    if item.get('manifest'):
        text = read(item['manifest'])
        if text is None:
            raise ValueError('The pinned build manifest is missing from the selected source commit: '+item['manifest'])
        fields = {}
        for raw in (text or '').splitlines():
            if raw.strip() and not raw.lstrip().startswith('#') and '=' in raw:
                key, value = raw.strip().split('=', 1)
                fields.setdefault(key, []).append(value)
        for key, value in (('R4OS_MODULE_MANIFEST', '2'), ('NAME', item['id']), ('KIND', item['kind'])):
            if fields.get(key) != [value]:
                raise ValueError('The selected target differs from its pinned manifest: '+key)
        versions = fields.get('VERSION', [])
        version = versions[0] if len(versions) == 1 else ''
        source = item['manifest']
    else:
        zone = read('build.zig.zon')
        cargo = read('Cargo.toml')
        match = re.search(r'\.version\s*=\s*"([^"]+)"', zone or '')
        cargo_version = tomllib.loads(cargo or '').get('package', {}).get('version', '')
        use_cargo = bool(cargo_version) and (item.get('language') == 'Rust' or not match)
        version = cargo_version if use_cargo else match.group(1) if match else ''
        source = 'Cargo.toml' if use_cargo else 'build.zig.zon'
    if not isinstance(version, str) or not VERSION.fullmatch(version):
        raise ValueError('This target needs a valid MAJOR.MINOR.PATCH version in its project manifest.')
    item.update(version=version, version_manifest=source)
    return item


def fingerprint(payload):
    import hashlib
    value = {key: payload[key] for key in ('target', 'commit', 'toolset', 'source_dependencies')}
    value['version'] = payload['module']['version']
    value['sdk_profile'] = payload.get('sdk_profile')
    value['additional_libraries'] = payload.get('additional_libraries', [])
    value['development_id'] = payload.get('development_id')
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
