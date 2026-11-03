"""Compare published R4OS source receipts with an independently selected SDK."""
import re
import json
from urllib.parse import urlsplit

from . import sdk_packages
from .store import now


def validate(package, record):
    proof=record.get('sdk_profile',{})
    manifest=package['manifest']
    if (proof.get('schema')!=1 or proof.get('sdk_version')!=manifest['sdk_version']
            or proof.get('profile_sources')!=sdk_packages.provenance(package)['profile_sources']
            or proof.get('profiles')!=manifest['profiles'] or proof.get('system_libraries')!=manifest['system_libraries']):
        raise ValueError('R4OS-SOURCES/sdk_profile differs from the selected SDK package.')
    sources={item['name']:item for item in record.get('repositories',[])}
    for name,bound in (('SDK',manifest['sdk_source']),('Contract',manifest['sources']['contract']),('Libraries',manifest['sources']['libraries'])):
        actual=sources.get(name,{})
        if actual.get('commit')!=bound['commit'] or actual.get('dirty') is not False:raise ValueError('R4OS source identity differs from the SDK: '+name)
    if record.get('technical') is not False:raise ValueError('The published R4OS reference is a technical candidate.')
    return proof


def attach(context,value):
    if not isinstance(value,dict) or set(value)!={'sdk_id','release_url'}:raise ValueError('Specify sdk_id and the official R4OS release_url.')
    package=sdk_packages.get(context.store,value['sdk_id'])
    url=urlsplit(value['release_url'])
    match=re.fullmatch(r'/R4OSDev/r4os-distribution/releases/tag/v([0-9]+\.[0-9]+\.[0-9]+)',url.path)
    if url.scheme!='https' or url.netloc!='github.com' or url.query or url.fragment or not match:raise ValueError('Use an official Distribution release URL.')
    version=match[1];base='https://github.com/R4OSDev/r4os-distribution/releases/download/v'+version+'/'
    folder=context.store.state/'sdk-release-links'/package['id']/version;folder.mkdir(parents=True,exist_ok=True)
    sums=folder/'SHA256SUMS.txt';sdk_packages.fetch(context,base+sums.name,sums,limit=65536)
    checks={}
    for line in sums.read_text().splitlines():
        entry=re.fullmatch(r'([0-9a-f]{64}) [ *]([A-Za-z0-9][A-Za-z0-9_.-]{0,159})',line)
        if not entry or entry[2].casefold() in {name.casefold() for name in checks}:raise ValueError('Invalid R4OS release checksums.')
        checks[entry[2]]=entry[1]
    name='R4OS-SOURCES-'+version+'.json'
    if name not in checks:raise ValueError('R4OS source receipt is missing from release checksums.')
    target=folder/name;sdk_packages.fetch(context,base+name,target,expected=checks[name],limit=8*1024**2)
    record=sdk_packages.strict_json(target.read_bytes());validate(package,record)
    if record.get('release_version')!=version:raise ValueError('R4OS release version differs from its source receipt.')
    link={'version':version,'release_url':value['release_url'],'source_url':base+name,'source_sha256':checks[name],
          'distribution_commit':record['distribution_commit'],'sdk_profile':record['sdk_profile'],'verified_at':now()}
    with sdk_packages.lock(context):
        package=sdk_packages.get(context.store,package['id'])
        links={item['version']:item for item in package.get('r4os_releases',[])}
        if version in links and links[version]['source_sha256']!=link['source_sha256']:raise ValueError('This R4OS release is already bound to different original bytes.')
        links[version]=link;package['r4os_releases']=list(links.values())
        with context.store.connect() as db:db.execute('UPDATE sdk_packages SET metadata=?,updated=? WHERE id=?',(json.dumps({k:v for k,v in package.items() if k not in ('id','status','error')}),now(),package['id']))
        sdk_packages.publish_index(context.store)
    return link
