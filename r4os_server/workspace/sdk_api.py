"""SDK management in the LAN WebApp and immutable client downloads."""
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, unquote, urlsplit

from ..common.http import HTTPError
from . import sdk_packages, sdk_projects, toolsets
from .api import data, file_response
from .store import Conflict, identifier, under


def client_download(handler, storage, path, *, public=False):
    parts = [unquote(value) for value in path.strip('/').split('/')]
    prefix = ['sdk'] if public else ['api','sdk']
    if parts[:len(prefix)] != prefix: raise HTTPError(404,'not_found')
    parts = parts[len(prefix):]
    index = Path(storage)/'SDK/catalog.json'
    try: catalog = sdk_packages.strict_json(index.read_bytes())
    except FileNotFoundError: catalog = {'schema':1,'interface':sdk_packages.INTERFACE,'packages':[]}
    base = handler.server.sdk_base_url if public else '/api/sdk'
    for item in catalog['packages']:
        for asset in item['assets']:
            asset['download_url'] = base+'/assets/'+item['id']+'/'+asset['path']
    if parts == ['catalog']:
        return handler.json(200,catalog)
    if len(parts) == 2 and parts[0] == 'packages':
        item = next((item for item in catalog['packages'] if item['id']==parts[1]),None)
        if item is None: raise HTTPError(404,'sdk_not_found')
        return handler.json(200,item)
    if len(parts) == 3 and parts[0] == 'assets':
        item = next((item for item in catalog['packages'] if item['id']==parts[1]),None)
        asset = next((asset for asset in (item or {}).get('assets',[]) if asset['path']==parts[2]),None)
        if asset is None: raise HTTPError(404,'sdk_asset_not_found')
        folder = under(Path(storage)/'SDK/Packages',identifier(item['id']))/'Payload'
        file = under(folder,sdk_packages.asset_name(asset['path']))
        if not file.is_file() or file.stat().st_size != asset['bytes'] or sdk_packages.digest(file)!=asset['sha256']: raise HTTPError(503,'sdk_asset_unavailable')
        return file_response(handler,file,asset['path'],immutable=True,etag=asset['sha256'])
    raise HTTPError(404,'not_found')


def dispatch(handler):
    store = handler.server.workspace
    path = urlsplit(handler.path).path
    parts = [unquote(value) for value in path.removeprefix('/api/sdk/').split('/') if value]
    read = handler.command in ('GET','HEAD')
    try:
        if parts == ['selection'] and handler.command == 'POST':
            from .sdk_install import request
            return handler.json(202, store.enqueue('sdk_select', 'sdk', request(data(handler))))
        if parts == ['releases'] and read:
            from .sdk_catalog import overview
            query = parse_qs(urlsplit(handler.path).query)
            custom = query.get('release_url', [None])[0]
            if custom:
                sdk_packages.release_base(custom)
            return handler.json(200, overview(store, refresh=query.get('refresh') == ['1'], custom_url=custom))
        if read and parts and parts[0] in ('catalog','assets','packages'):
            return client_download(handler,store.root/'BuildTools',path)
        if parts == ['imports']:
            if read: return handler.json(200,{'packages':[sdk_packages.summary(item) for item in sdk_packages.packages(store)],'jobs':store.jobs('sdk')})
            if handler.command == 'POST':
                value=data(handler)
                if set(value)!={'release_url'}: raise ValueError('Specify release_url.')
                sdk_packages.release_base(value['release_url'])
                return handler.json(202,store.enqueue('sdk_import','sdk',value))
        if parts == ['release-links'] and handler.command=='POST':
            return handler.json(202,store.enqueue('sdk_release_link','sdk',data(handler)))
        if parts == ['tools'] and read: return handler.json(200,toolsets.overview(store))
        if len(parts)==2 and parts[0]=='projects':
            if read: return handler.json(200,sdk_projects.get(store,parts[1]))
            if handler.command=='PUT':
                result=sdk_projects.save(store,parts[1],data(handler));store.enqueue('check',parts[1],{'profile_refresh':True})
                return handler.json(200,result)
        if parts==['development'] and handler.command=='POST':
            return handler.json(202,store.enqueue('sdk_develop','sdk',data(handler)))
        if len(parts)==3 and parts[:1]==['packages'] and parts[2]=='verify' and handler.command=='POST':
            sdk_packages.get(store,parts[1]);return handler.json(202,store.enqueue('sdk_verify','sdk',{'sdk_id':parts[1]}))
        if len(parts)==3 and parts[:1]==['profiles'] and parts[2] in ('describe','resolve'):
            sdk_packages.get(store,parts[1])
            if handler.command=='POST':
                value=data(handler)
                if set(value)!={'profile'}: raise ValueError('Specify the SDK profile ID.')
                return handler.json(202,store.enqueue('sdk_profile','sdk',{'sdk_id':parts[1],'action':parts[2],'profile':value['profile']}))
        if parts == ['cleanup'] and read:
            from .sdk_cleanup import inventory
            return handler.json(200,inventory(store))
        if parts == ['cleanup'] and handler.command=='POST':
            value=data(handler)
            if value!={'confirmed':True}: raise ValueError('Confirm removal of obsolete local SDK environments.')
            return handler.json(202,store.enqueue('sdk_cleanup','sdk',value))
        raise HTTPError(404,'not_found')
    except Conflict as error: raise HTTPError(409,'sdk_conflict',str(error)) from None
    except (ValueError,KeyError,TypeError) as error: raise HTTPError(422,'sdk_validation_error',str(error)) from None
