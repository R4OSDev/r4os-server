"""Boundary checks for immutable SDK packages and removal of legacy contracts."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import subprocess
import unittest
from unittest.mock import patch

from r4os_server.workspace import sdk_catalog, sdk_packages, sdk_projects, toolsets
from r4os_server.workspace.process import Context
from r4os_server.workspace.store import Store
from sdk_fixture import install


class SDKTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'workspace');self.proof=install(self.store)
        self.job=self.store.enqueue('sdk_verify','sdk');self.context=Context(self.store,self.job)

    def prepare_selection(self):
        package=sdk_packages.get(self.store,'sdk-fixture');manifest=package['manifest']
        manifest['tools']={'zig':{'version':'0.16.0'},'qemu':{'hosts':{}}}
        manifest['toolsets']={'core':{'tools':['zig','qemu']}}
        folder=sdk_packages.root(self.store,package['id'])
        for name,relative in sdk_packages.PROFILE_FILES.items():
            path=folder/'Content/SDK'/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_text('{"schema":1}')
            manifest['profile_sources'][name]['sha256']=sdk_packages.digest(path)
        for name in ('CONTRACT','LIBRARIES','RECOVERY','Tools'):(folder/'Content'/name).mkdir(parents=True,exist_ok=True)
        raw=b'original SDK asset';payload=folder/'Payload/original.bin';payload.parent.mkdir(exist_ok=True);payload.write_bytes(raw)
        package['assets']=[{'path':'original.bin','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]
        with self.store.connect() as db:db.execute('UPDATE sdk_packages SET metadata=? WHERE id=?',(json.dumps(package),package['id']))
        path=self.store.root/'BuildTools/Toolsets/test-tools';env=json.loads((path/'toolset.json').read_text())
        hashes=sdk_packages.provenance(package)['profile_sources'];env['sdk_profile_sources']=hashes
        (path/'toolset.json').write_text(json.dumps(env))
        executable=path/'Install/Toolchains/Zig/zig';executable.parent.mkdir(parents=True,exist_ok=True);executable.write_bytes(b'fixture, never executed')
        (path/'Install/.SDK-Tools.json').write_text(json.dumps({'schema':1,'sdk_version':'0.64.145','host':'Linux','profiles':hashes,'tools':{'zig':{'ready':True}}}))
        return sdk_packages.get(self.store,package['id'])

    def test_sdk_release_pagination_sorting_cache_and_offline_state(self):
        self.prepare_selection()
        def release(version,identity):
            return {'id':identity,'tag_name':'v'+version,'draft':False,'prerelease':False,
                    'assets':[{'name':name} for name in ('SDK-Release.json','SHA256SUMS.txt','r4os-sdk-capabilities-v3.json','R4OS-Recovery-0.1.67-source.tar.gz')]}
        first=[release('0.64.'+str(n),n+1) for n in range(98)]+[release('0.64.145',145),release('0.64.99',99)]
        draft=dict(release('9.9.9',999),draft=True)
        old=dict(release('0.63.9',6309),assets=[])
        with patch.object(catalog_module:=sdk_catalog.catalog,'public_json',side_effect=[(first,{'Link':'<next>; rel="next"'}),([release('0.64.146',146),draft,old],{})]) as request,patch.object(sdk_catalog,'_manifest',return_value='1'*64):
            value=sdk_catalog.overview(self.store,refresh=True)
        self.assertEqual(request.call_count,2)
        self.assertEqual([r['version'] for r in value['releases'][:3]],['0.64.146','0.64.145','0.64.99'])
        self.assertEqual(len(value['releases']),102)
        self.assertEqual(value['releases'][1]['state'],'active')
        self.assertFalse(value['releases'][-1]['supported'])
        with patch.object(catalog_module,'public_json',side_effect=AssertionError('cache bypassed')):
            self.assertEqual(sdk_catalog.overview(self.store)['releases'][1]['state'],'active')
        with patch.object(catalog_module,'public_json',side_effect=RuntimeError('unavailable')):
            offline=sdk_catalog.overview(self.store,refresh=True)
        self.assertTrue(offline['catalog']['stale']);self.assertTrue(offline['catalog']['error'])
        self.assertEqual(offline['catalog']['updated'],value['catalog']['updated'])
        self.assertEqual(offline['releases'][1]['state'],'active')
        self.store.set('active_toolset',None);self.store.set('active_sdk',None)
        self.assertEqual(sdk_catalog.overview(self.store)['releases'][1]['state'],'installed')
        executable=self.store.root/'BuildTools/Toolsets/test-tools/Install/Toolchains/Zig/zig';executable.unlink()
        self.assertEqual(sdk_catalog.overview(self.store)['releases'][1]['state'],'download')

    def test_sdk_prepared_state_requires_owner_receipt_originals_and_complete_tools(self):
        package=self.prepare_selection();env=toolsets.load(self.store,'test-tools')[1]
        self.assertTrue(sdk_catalog.prepared(self.store,package,env))
        self.assertFalse(sdk_catalog.prepared(self.store,package,dict(env,installed_tools=[])))
        receipt=self.store.root/'BuildTools/Toolsets/test-tools/Install/.SDK-Tools.json'
        value=json.loads(receipt.read_text());value['host']='Windows';receipt.write_text(json.dumps(value))
        self.assertFalse(sdk_catalog.prepared(self.store,package,env))
        value['host']='Linux';receipt.write_text(json.dumps(value))
        path=sdk_packages.root(self.store,'sdk-fixture')/'Payload/original.bin';path.write_bytes(b'modified SDK asset')
        self.assertFalse(sdk_catalog.prepared(self.store,package,env))
        self.store.set(sdk_catalog.CACHE_KEY,{'releases':[{'release_url':package['release_url'],'tag':'v0.64.145','version':'0.64.145','supported':True,'manifest_sha256':'2'*64}],'attempted':__import__('time').time(),'error':''})
        self.assertEqual(sdk_catalog.overview(self.store)['releases'][0]['state'],'unsupported')

    def test_strict_originals_and_unknown_schema(self):
        for raw in (b'{"schema":1,"Schema":1}',b'{"id":1,"id":2}',b'{"n":NaN}'):
            with self.assertRaises(ValueError):sdk_packages.strict_json(raw)
        package=sdk_packages.get(self.store,'sdk-fixture');manifest=package['manifest']
        for schema in (None,True,2):
            value=copy.deepcopy(manifest);value['schema']=schema
            with self.assertRaisesRegex(ValueError,'schema'):sdk_packages.validate_manifest(value,'0.64.145')
        with self.assertRaises(ValueError):sdk_packages.release_base('https://github.com/foreign/r4os-sdk/releases/tag/v0.64.145')

    def test_archive_traversal_alias_and_links_are_rejected(self):
        for names,link in ((['SDK/../escape'],False),(['SDK/a','SDK/A'],False),(['SDK/link'],True)):
            archive=Path(self.temp.name)/'unsafe.tar'
            with tarfile.open(archive,'w') as target:
                for name in names:
                    member=tarfile.TarInfo(name)
                    if link:member.type=tarfile.SYMTYPE;member.linkname='/etc/passwd'
                    target.addfile(member,io.BytesIO())
            with self.assertRaises(ValueError):sdk_packages.extract(archive,Path(self.temp.name)/'content','SDK')

    def test_partial_and_legacy_environments_are_not_activatable(self):
        with self.store.connect() as db:db.execute("UPDATE sdk_packages SET status='importing'")
        with self.assertRaisesRegex(ValueError,'complete verification'):toolsets.active(self.store)
        sdk_packages.publish_index(self.store)
        index=json.loads((self.store.root/'BuildTools/SDK/catalog.json').read_text());self.assertEqual(index['packages'],[])
        old=self.store.root/'BuildTools/Toolsets/old';old.mkdir()
        (old/'toolset.json').write_text('{"status":"ready","sources":{"SDK":{"version":"0.64.135"}}}')
        with self.assertRaisesRegex(ValueError,'removed'):toolsets.load(self.store,'old')

    def test_payload_mutation_is_stopped_before_owner_execution(self):
        package=sdk_packages.get(self.store,'sdk-fixture');raw=b'original'
        package['assets']=[{'path':'original.bin','bytes':len(raw),'sha256':hashlib.sha256(raw).hexdigest()}]
        with self.store.connect() as db:db.execute('UPDATE sdk_packages SET metadata=?',(json.dumps(package),))
        folder=sdk_packages.root(self.store,'sdk-fixture')/'Payload';folder.mkdir(parents=True)
        (folder/'original.bin').write_bytes(b'modified')
        with patch.object(self.context,'run') as run:
            with self.assertRaisesRegex(ValueError,'immutable registration'):sdk_packages.verify(self.context,'sdk-fixture')
            run.assert_not_called()

    def test_cleanup_removes_legacy_sources_and_keeps_current_recovery_and_receipts(self):
        from r4os_server.workspace import sdk_cleanup
        legacy=self.store.root/'BuildTools/Toolsets/legacy';legacy.mkdir()
        (legacy/'toolset.json').write_text(json.dumps({'id':'legacy','status':'ready','sources':{'SDK':{'version':'0.64.100'}}}))
        job=self.store.enqueue('build','old-source',{'toolset':'legacy'});self.store.change_job(job['id'],status='completed',result={'historical':True})
        old=self.store.root/'BuildTools/Workspaces'/job['id'];old.mkdir(parents=True);(old/'old-source').write_text('obsolete')
        recovery=sdk_packages.root(self.store,'sdk-fixture')/'Content/RECOVERY';recovery.mkdir(parents=True);(recovery/'frozen').write_text('current SDK component')
        retired=self.store.root/'Repositories/Main/r4os-devkit';retired.mkdir(parents=True)
        subprocess.run(['git','init',str(retired)],check=True,capture_output=True)
        with self.store.connect() as db:db.execute('INSERT INTO repositories(name,github_id,metadata,directory,enabled) VALUES(?,?,?,?,0)',('r4os-devkit',91,json.dumps({'id':91,'name':'r4os-devkit','display_name':'r4os-devkit','owner':'R4OSDev'}),'Main/r4os-devkit'))
        artifact=self.store.root/'Artifacts/saved.bin';artifact.parent.mkdir(exist_ok=True);artifact.write_bytes(b'user artifact')
        with patch.object(toolsets,'validate',return_value={'ready':True}):result=sdk_cleanup.cleanup(self.context)
        self.assertIn('0.64.100',result['obsolete_sdk_versions']);self.assertFalse(legacy.exists());self.assertFalse(old.exists())
        self.assertFalse(retired.exists());self.assertFalse(self.store.repository('r4os-devkit')['enabled'])
        self.assertTrue((recovery/'frozen').is_file());self.assertEqual(artifact.read_bytes(),b'user artifact')
        self.assertEqual(self.store.job(job['id'])['result'],{'historical':True})
        self.assertEqual(toolsets.active(self.store)[1]['sdk_id'],'sdk-fixture')

    def test_zig_catalog_null_rust_section_uses_offered_sdk_profile(self):
        from r4os_server.workspace import profiles
        discovered={'modules':[{'id':'EXAMPLE','name':'EXAMPLE','kind':'R4X','language':'Zig','manifest':'module.R4MF'}],'profile':{'errors':[]}}
        plan={'plan':{'kind':'R4X','name':'EXAMPLE','language':'Zig','rust_project':None}}
        with patch.object(toolsets,'prepare_metadata_parser',return_value=Path('/not-executed')),patch.object(self.context,'run',return_value=(json.dumps(plan),0)):
            result=profiles.validate(self.context,Path(self.temp.name),discovered)
        self.assertTrue(result['modules'][0]['supported']);self.assertEqual(result['modules'][0]['sdk_profile']['profile'],'r4os-zig-module-v1')

    def test_verified_repeat_reuses_immutable_package_offline(self):
        with patch.object(sdk_packages,'verify',return_value={'verified':True}) as verify,patch.object(sdk_packages,'fetch',side_effect=AssertionError('network used')):
            result=sdk_packages.import_release(self.context,{'release_url':'https://github.com/R4OSDev/r4os-sdk/releases/tag/v0.64.145'})
        self.assertTrue(result['unchanged']);verify.assert_called_once()
