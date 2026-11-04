"""Official SDK releases and their local, host-specific preparation state."""
from functools import lru_cache
import json
import hashlib
from pathlib import Path
import platform
import re
import threading
import time
from urllib.parse import quote
import urllib.request

from . import catalog, connections, sdk_packages, toolsets
from .store import now

REPOSITORY = 'https://api.github.com/repos/R4OSDev/r4os-sdk'
CACHE_KEY = 'sdk-release-catalog'
_refresh_lock = threading.Lock()


def required_tools(package):
    """Bootstrap build tools from SDK profiles; QEMU is an optional runtime."""
    manifest = package['manifest']
    names = set()
    for profile in manifest['profiles'].values():
        if profile['role'] in ('module', 'kernel', 'host', 'recovery'):
            names.update(manifest['toolsets'][profile['toolset']]['tools'])
    names.discard('qemu')
    if not names or names - {'zig', 'rust', 'limine'} or names - manifest['tools'].keys():
        raise ValueError('This SDK has unsupported bootstrap build requirements.')
    return sorted(names)


def _credential(store):
    main = connections.get(store, 'main')
    if main['owner'].casefold() == 'r4osdev' and main['status'] == 'ready':
        try:
            return connections.token(store, 'main')
        except connections.CredentialError:
            pass  # The official releases are public and do not require a token.
    return ''


def _manifest(url):
    base, version = sdk_packages.release_base(url)
    request = urllib.request.Request(base+'SDK-Release.json', headers={'User-Agent': 'R4OS-Server'})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(8*1024**2+1)
    if len(raw) > 8*1024**2:
        raise ValueError('The SDK release manifest is too large.')
    manifest = sdk_packages.strict_json(raw)
    sdk_packages.validate_manifest(manifest, version)
    required_tools({'manifest': manifest})
    return hashlib.sha256(raw).hexdigest()


def release_record(item):
    tag = str(item['tag_name'])
    url = 'https://github.com/R4OSDev/r4os-sdk/releases/tag/'+quote(tag, safe='')
    record = {'release_id': item['id'], 'tag': tag, 'version': tag.removeprefix('v'),
              'release_url': url, 'published_at': item.get('published_at'),
              'prerelease': bool(item.get('prerelease')), 'supported': False, 'reason': '', 'custom': False}
    assets = {asset['name'] for asset in item.get('assets', [])}
    if (not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag)
            or not {'SDK-Release.json', 'SHA256SUMS.txt', 'r4os-sdk-capabilities-v3.json'} <= assets
            or not any(re.fullmatch(r'R4OS-Recovery-[0-9.]+-source.tar.gz', name) for name in assets)):
        record['reason'] = 'This release does not provide the current SDK package contract.'
        return record
    try:
        record['manifest_sha256'] = _manifest(url)
        record['supported'] = True
    except (ValueError, KeyError, TypeError):
        record['reason'] = 'This release does not provide the current SDK package contract.'
    return record


def sort_key(item):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(.*)', item['tag'])
    version = tuple(map(int, match.groups()[:3])) if match else (-1, -1, -1)
    return (*version, not item.get('prerelease', False), item.get('published_at') or '', item.get('release_id') or 0)


def remote_catalog(store, *, refresh=False):
    with _refresh_lock:
        cache = store.setting(CACHE_KEY, {})
        if not refresh and time.time() - cache.get('attempted', 0) < (60 if cache.get('error') else 300):
            return cache
        try:
            credential, found = _credential(store), {}
            for page in range(1, 101):
                items, headers = catalog.public_json(REPOSITORY+'/releases?per_page=100&page='+str(page), credential)
                if not isinstance(items, list):
                    raise ValueError('GitHub returned an invalid SDK release list.')
                for item in items:
                    if not item.get('draft'):
                        found[item['id']] = release_record(item)
                if 'rel="next"' not in headers.get('Link', ''):
                    break
            else:
                raise ValueError('The SDK release list is incomplete; keeping the previous complete catalogue.')
            cache = {'releases': sorted(found.values(), key=sort_key, reverse=True),
                     'updated': now(), 'attempted': time.time(), 'stale': False, 'error': ''}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            cache = dict(cache, attempted=time.time(), stale=True,
                         error='The official SDK release catalogue could not be refreshed. Check GitHub access or API rate limits and retry.')
        store.set(CACHE_KEY, cache)
        return cache


@lru_cache(maxsize=256)
def _hashed(path, inode, size, modified, changed):
    return sdk_packages.digest(path)


def _matches(path, sha, size=None):
    stat = path.stat()
    return ((size is None or stat.st_size == size)
            and _hashed(str(path), stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) == sha)


def prepared(store, package, environment):
    """Cheap repeated checks of verified originals and the owner's setup receipt.

    Actual activation runs the SDK's complete package and tool checks again.
    """
    try:
        manifest = package['manifest']
        names = required_tools(package)
        if platform.system() != 'Linux' or platform.machine() != 'x86_64':
            return False
        if package['status'] != 'ready' or environment['status'] != 'ready' or environment['schema'] != 2:
            return False
        if environment['sdk_profile_sources'] != sdk_packages.provenance(package)['profile_sources']:
            return False
        if not set(names) <= set(environment['installed_tools']):
            return False
        folder = sdk_packages.root(store, package['id'])
        for asset in package['assets']:
            if not _matches(folder/'Payload'/sdk_packages.asset_name(asset['path']), asset['sha256'], asset['bytes']):
                return False
        for name, relative in sdk_packages.PROFILE_FILES.items():
            if not _matches(folder/'Content/SDK'/relative, manifest['profile_sources'][name]['sha256']):
                return False
        if not all((folder/'Content'/name).is_dir() for name in ('CONTRACT', 'LIBRARIES', 'RECOVERY', 'Tools')):
            return False
        path, loaded = toolsets.load(store, environment['id'])
        if loaded['sdk_id'] != package['id']:
            return False
        receipt = sdk_packages.strict_json((path/'Install/.SDK-Tools.json').read_bytes())
        if (receipt['schema'] != 1 or receipt['host'] != platform.system()
                or receipt['sdk_version'] != manifest['sdk_version']
                or receipt['profiles'] != sdk_packages.provenance(package)['profile_sources']):
            return False
        executables = {'zig': 'Toolchains/Zig/zig', 'rust': 'Toolchains/Rust/cargo/bin/rustc', 'limine': 'Boot/Limine/limine'}
        return all(receipt['tools'].get(name, {}).get('ready') is True
                   and (path/'Install'/executables[name]).is_file() for name in names)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def custom_release(store, url, refresh=False):
    _, version = sdk_packages.release_base(url)
    cache = store.setting('sdk-custom-releases', {})
    previous = cache.get(url, {})
    if not refresh and time.time()-previous.get('attempted', 0) < (60 if previous.get('failed') else 300):
        return dict(previous['record'])
    failed = False
    try:
        item, _ = catalog.public_json(REPOSITORY+'/releases/tags/'+quote('v'+version, safe=''), _credential(store))
        if item.get('draft'):
            raise ValueError('The selected SDK release is not published.')
        record = release_record(item)
        if record['release_url'] != url:
            raise ValueError('The release identity differs from the requested SDK.')
        record['custom'] = True
    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
        failed = True
        record = {'release_url':url, 'tag':'v'+version, 'version':version, 'supported':False,
                  'custom':True, 'reason':'The custom SDK release could not be loaded. Check the URL and retry.'}
    cache[url] = {'attempted':time.time(), 'record':record, 'failed':failed}
    cache = dict(sorted(cache.items(), key=lambda pair:pair[1]['attempted'], reverse=True)[:32])
    store.set('sdk-custom-releases', cache)
    return dict(record)


def refresh_job(context, value):
    """GitHub egress belongs to the worker, never the LAN-only WebApp."""
    url = value.get('release_url')
    if url:
        context.phase('Loading the custom SDK release')
        record = custom_release(context.store, url, refresh=True)
        if not record['supported']:
            raise ValueError(record['reason'])
        return {'release_url':url, 'supported':True}
    context.phase('Refreshing the official SDK release catalogue')
    cache = remote_catalog(context.store, refresh=True)
    if cache.get('error'):
        raise RuntimeError(cache['error'])
    return {'updated':cache['updated'], 'release_count':len(cache['releases'])}


def overview(store, *, refresh=False, custom_url=None):
    cache = store.setting(CACHE_KEY, {})
    catalogue_job = None
    if refresh or time.time()-cache.get('attempted', 0) >= (60 if cache.get('error') else 300):
        catalogue_job = store.enqueue('sdk_catalog', 'sdk', {})
    else:
        catalogue_job = next((job for job in store.jobs('sdk') if job['kind'] == 'sdk_catalog' and job['status'] in ('queued','running')), None)
    records = {item['release_url']: dict(item) for item in cache.get('releases', [])}
    packages = sdk_packages.packages(store)
    environments = toolsets.overview(store)['toolsets']
    active = store.setting('active_toolset')
    active_sdk = store.setting('active_sdk')
    environments.sort(key=lambda environment: environment.get('id') != active)
    for package in packages:
        url = package['release_url']
        records.setdefault(url, {'release_url': url, 'tag': 'v'+package['manifest']['sdk_version'],
                                'version': package['manifest']['sdk_version'], 'supported': True,
                                'reason': '', 'custom': True, 'prerelease': False})
    if custom_url and custom_url not in records:
        _, version = sdk_packages.release_base(custom_url)
        custom = store.setting('sdk-custom-releases', {}).get(custom_url, {})
        pending = None
        subject = 'sdk-custom-'+hashlib.sha256(custom_url.encode()).hexdigest()[:16]
        if refresh or time.time()-custom.get('attempted', 0) >= (60 if custom.get('failed') else 300):
            pending = store.enqueue('sdk_catalog', subject, {'release_url':custom_url})
        else:
            pending = next((job for job in store.jobs(subject) if job['status'] in ('queued','running')), None)
        records[custom_url] = dict(custom.get('record') or {'release_url':custom_url, 'tag':'v'+version,
                                  'version':version, 'custom':True, 'supported':False, 'reason':'Loading the custom SDK release…'})
        if pending:
            catalogue_job = pending
    for url, record in records.items():
        package = next((p for p in packages if p['release_url'] == url), None)
        ready = next((env for env in environments if package and env.get('sdk_id') == package['id'] and prepared(store, package, env)), None)
        record.update(sdk_id=package['id'] if package else None, environment_id=ready['id'] if ready else None,
                      state='active' if ready and ready['id'] == active and ready['sdk_id'] == active_sdk else 'installed' if ready else 'download')
        if package and record.get('manifest_sha256') and record['manifest_sha256'] != package['manifest_sha256']:
            record.update(supported=False, reason='This release tag now identifies different immutable SDK bytes.')
        if not record.get('supported'):
            record['state'] = 'unsupported'
    jobs = store.jobs('sdk', limit=100)
    running = next((job for job in jobs if job['kind'] == 'sdk_select' and job['status'] in ('queued', 'running')), None)
    return {'releases': sorted(records.values(), key=sort_key, reverse=True),
            'catalog': dict({key:cache.get(key) for key in ('updated', 'stale', 'error')},
                            refreshing=bool(catalogue_job), job_id=catalogue_job['id'] if catalogue_job else None),
            'active_sdk': active_sdk, 'active_url': next((p['release_url'] for p in packages if p['id'] == active_sdk), None),
            'job': running or next((job for job in jobs if job['kind'] == 'sdk_select'), None)}
