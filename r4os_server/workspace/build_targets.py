"""Production targets and versions read from the exact source commit being built."""
import json
from pathlib import PurePosixPath
import re
import subprocess
import tomllib

from . import catalog
from .process import clean_env

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
    if path.is_absolute() or '..' in path.parts or not path.parts:
        raise ValueError('Invalid build manifest path.')
    result = subprocess.run(['git', '-C', str(catalog.repository_path(store, repo)), 'show', commit+':'+str(path)],
                            env=clean_env(), capture_output=True, timeout=20)
    return result.stdout.decode('utf-8-sig') if result.returncode == 0 else None


def pinned(store, repo, module, commit):
    """Never use a stale UI version to choose the immutable release tag."""
    item = dict(module)
    if item.get('manifest'):
        text = blob(store, repo, commit, item['manifest'])
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
        zone = blob(store, repo, commit, 'build.zig.zon')
        cargo = blob(store, repo, commit, 'Cargo.toml')
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
