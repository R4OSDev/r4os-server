"""Minimal verified-package state for isolated server contract tests (no compiler)."""
import json
from r4os_server.workspace import sdk_packages
from r4os_server.workspace.store import now


def install(store, key='sdk-fixture'):
    hashes={name:letter*64 for name,letter in (('Build','a'),('Tools','b'),('Components','c'))}
    source={'repository':'https://github.com/R4OSDev/r4os-sdk.git','commit':'d'*40,'version':'0.64.145'}
    manifest={'schema':1,'interface':'r4os-sdk-package','sdk_version':'0.64.145','sdk_source':source,
              'profile_sources':{name:{'path':'Profiles/'+name+'.json','schema':1,'sha256':value} for name,value in hashes.items()},
              'profiles':{'r4os-zig-module-v1':{'language':'Zig','role':'module','toolset':'core','components':'system'}},
              'tools':{},'toolsets':{},'component_sets':{},'system_libraries':{},'policy':{},'assets':[],'sources':{'contract':dict(source,commit='e'*40),'libraries':dict(source,commit='f'*40)}}
    metadata={'manifest':manifest,'manifest_sha256':'1'*64,'release_url':'https://github.com/R4OSDev/r4os-sdk/releases/tag/v0.64.145','assets':[],'capabilities':{}}
    with store.connect() as db:db.execute('INSERT INTO sdk_packages VALUES(?,?,?,?,?,?,?)',(key,'0.64.145','ready',json.dumps(metadata),'',now(),now()))
    path=store.root/'BuildTools/Toolsets/test-tools';path.mkdir(parents=True,exist_ok=True)
    (path/'toolset.json').write_text(json.dumps({'schema':2,'id':'test-tools','status':'ready','sdk_id':key,'sdk_profile_sources':hashes,'versions':{},'sources':{},'installed_tools':['zig']}))
    store.set('active_toolset','test-tools');store.set('active_sdk',key)
    return sdk_packages.provenance(sdk_packages.get(store,key),'r4os-zig-module-v1')
