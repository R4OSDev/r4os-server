"""Boundary checks for immutable SDK packages and removal of legacy contracts."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import subprocess
import time
import unittest
from unittest.mock import patch

from r4os_server.workspace import sdk_catalog, sdk_install, sdk_packages, sdk_projects, toolsets, worker
from r4os_server.workspace.process import Cancelled, Context
from r4os_server.workspace.store import Conflict, Store, now
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
        with patch.object(sdk_catalog.catalog,'public_json',side_effect=[(first,{'Link':'<next>; rel="next"'}),([release('0.64.146',146),draft,old],{})]) as request,patch.object(sdk_catalog,'_manifest',return_value='1'*64):
            sdk_catalog.remote_catalog(self.store,refresh=True)
            value=sdk_catalog.overview(self.store)
        self.assertEqual(request.call_count,2)
        self.assertEqual([r['version'] for r in value['releases'][:3]],['0.64.146','0.64.145','0.64.99'])
        self.assertEqual(len(value['releases']),102)
        self.assertEqual(value['releases'][1]['state'],'active')
        self.assertFalse(value['releases'][-1]['supported'])
        with patch.object(sdk_catalog.catalog,'public_json',side_effect=AssertionError('cache bypassed')):
            self.assertEqual(sdk_catalog.overview(self.store)['releases'][1]['state'],'active')
        custom='https://github.com/R4OSDev/r4os-sdk/releases/tag/v0.64.148'
        with patch.object(sdk_catalog.catalog,'public_json',return_value=(release('0.64.148',148),{})) as request,patch.object(sdk_catalog,'_manifest',return_value='1'*64):
            sdk_catalog.custom_release(self.store,custom)
            self.assertTrue(sdk_catalog.overview(self.store,custom_url=custom)['releases'][0]['custom'])
            self.assertTrue(sdk_catalog.overview(self.store,custom_url=custom)['releases'][0]['custom'])
            self.assertEqual(request.call_count,1)
        with patch.object(sdk_catalog.catalog,'public_json',side_effect=RuntimeError('unavailable')):
            sdk_catalog.remote_catalog(self.store,refresh=True)
            offline=sdk_catalog.overview(self.store)
        self.assertTrue(offline['catalog']['stale']);self.assertTrue(offline['catalog']['error'])
        self.assertEqual(offline['catalog']['updated'],value['catalog']['updated'])
        self.assertEqual(offline['releases'][1]['state'],'active')
        self.store.set('active_toolset',None);self.store.set('active_sdk',None)
        self.assertEqual(sdk_catalog.overview(self.store)['releases'][1]['state'],'installed')
        executable=self.store.root/'BuildTools/Toolsets/test-tools/Install/Toolchains/Zig/zig';executable.unlink()
        self.assertEqual(sdk_catalog.overview(self.store)['releases'][1]['state'],'download')

        # HTTP-side reads schedule work; they must never perform Internet I/O.
        with patch.object(sdk_catalog.catalog,'public_json',side_effect=AssertionError('WebApp attempted Internet access')):
            pending=sdk_catalog.overview(self.store,refresh=True)
            repeated=sdk_catalog.overview(self.store,refresh=True)
        self.assertTrue(pending['catalog']['refreshing'])
        self.assertEqual(pending['catalog']['job_id'],repeated['catalog']['job_id'])
        context=Context(self.store,self.store.job(pending['catalog']['job_id']))
        with patch.object(sdk_catalog.catalog,'public_json',return_value=([release('0.64.146',146)],{})),patch.object(sdk_catalog,'_manifest',return_value='1'*64):
            self.assertEqual(worker.execute(context)['release_count'],1)

    def test_sdk_prepared_state_requires_owner_receipt_originals_and_complete_tools(self):
        package=self.prepare_selection();env=toolsets.load(self.store,'test-tools')[1]
        self.assertTrue(sdk_catalog.prepared(self.store,package,env))
        # Optional tool setup can leave several ready environments for one SDK.
        other=self.store.root/'BuildTools/Toolsets/a-tools';other.mkdir()
        (other/'toolset.json').write_text(json.dumps(dict(env,id='a-tools')))
        (other/'Install').symlink_to(other.parent/'test-tools/Install',target_is_directory=True)
        self.store.set(sdk_catalog.CACHE_KEY,{'releases':[],'attempted':time.time(),'error':''})
        selected=sdk_catalog.overview(self.store)['releases'][0]
        self.assertEqual(selected['state'],'active');self.assertEqual(selected['environment_id'],'test-tools')
        self.assertFalse(sdk_catalog.prepared(self.store,package,dict(env,installed_tools=[])))
        receipt=self.store.root/'BuildTools/Toolsets/test-tools/Install/.SDK-Tools.json'
        value=json.loads(receipt.read_text());value['host']='Windows';receipt.write_text(json.dumps(value))
        self.assertFalse(sdk_catalog.prepared(self.store,package,env))
        value['host']='Linux';receipt.write_text(json.dumps(value))
        path=sdk_packages.root(self.store,'sdk-fixture')/'Payload/original.bin';path.write_bytes(b'modified SDK asset')
        self.assertFalse(sdk_catalog.prepared(self.store,package,env))
        self.store.set(sdk_catalog.CACHE_KEY,{'releases':[{'release_url':package['release_url'],'tag':'v0.64.145','version':'0.64.145','supported':True,'manifest_sha256':'2'*64}],'attempted':time.time(),'error':''})
        self.assertEqual(sdk_catalog.overview(self.store)['releases'][0]['state'],'unsupported')

    def new_selection_package(self):
        import shutil
        package=self.prepare_selection();package=copy.deepcopy(package)
        package.update(id='sdk-new',release_url=package['release_url'].replace('0.64.145','0.64.146'))
        package['manifest']['sdk_version']='0.64.146';package['manifest_sha256']='2'*64
        with self.store.connect() as db:db.execute('INSERT INTO sdk_packages VALUES(?,?,?,?,?,?,?)',('sdk-new','0.64.146','ready',json.dumps(package),'',now(),now()))
        shutil.copytree(sdk_packages.root(self.store,'sdk-fixture'),sdk_packages.root(self.store,'sdk-new'))
        return package

    def fake_setup(self, context, sdk_id, root, names, *, check=False):
        self.assertEqual(self.store.setting('active_sdk'),'sdk-fixture')
        if check:return '{}',0
        package=sdk_packages.get(self.store,sdk_id)
        for name in names:
            path=root/{'zig':'Toolchains/Zig/zig','rust':'Toolchains/Rust/cargo/bin/rustc','limine':'Boot/Limine/limine'}[name]
            path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'fixture, never executed')
        (root/'.SDK-Tools.json').write_text(json.dumps({'schema':1,'sdk_version':package['manifest']['sdk_version'],'host':'Linux','profiles':sdk_packages.provenance(package)['profile_sources'],'tools':{n:{'ready':True} for n in names}}))
        return '{}',0

    def test_sdk_selection_prepares_then_atomically_activates_and_repeats_offline(self):
        package=self.new_selection_package();value={'release_url':package['release_url']}
        job=self.store.enqueue('sdk_select','sdk',value);self.store.change_job(job['id'],status='running')
        context=Context(self.store,job)
        original=(self.store.root/'BuildTools/Toolsets/test-tools/toolset.json').read_bytes()
        with patch.object(sdk_packages,'verify',return_value={}),patch.object(sdk_packages,'fetch',side_effect=AssertionError('network download')),patch.object(toolsets,'run_setup',side_effect=self.fake_setup),patch.object(context,'run',return_value=('PowerShell fixture',0)):
            result=worker.execute(context)
        self.assertTrue(result['activation_committed']);self.assertEqual(result['sdk_id'],'sdk-new')
        self.assertEqual(self.store.setting('active_toolset'),result['active'])
        self.assertEqual(self.store.setting('active_sdk'),'sdk-new')
        self.assertEqual((self.store.root/'BuildTools/Toolsets/test-tools/toolset.json').read_bytes(),original)
        self.assertTrue(self.store.job(job['id'])['result']['activation_committed'])
        self.store.cancel_job(job['id']);self.assertFalse(self.store.job(job['id'])['cancel'])
        self.store.recover();self.assertEqual(self.store.job(job['id'])['status'],'completed')
        again=self.store.enqueue('sdk_select','sdk',value);again_context=Context(self.store,again)
        with patch.object(sdk_packages,'verify',return_value={}),patch.object(sdk_packages,'fetch',side_effect=AssertionError('network download')),patch.object(toolsets,'install',side_effect=AssertionError('unnecessary installation')),patch.object(toolsets,'run_setup',return_value=('{}',0)):
            repeated=worker.execute(again_context)
        self.assertEqual(repeated['active'],result['active'])

    def test_sdk_failed_setup_cancellation_retry_and_real_download_progress(self):
        package=self.new_selection_package();value={'release_url':package['release_url']}
        job=self.store.enqueue('sdk_select','sdk',value);context=Context(self.store,job)
        with patch.object(sdk_packages,'verify',return_value={}),patch.object(toolsets,'run_setup',side_effect=RuntimeError('setup failed')):
            with self.assertRaisesRegex(RuntimeError,'setup failed'):worker.execute(context)
        self.assertEqual(self.store.setting('active_sdk'),'sdk-fixture')
        self.store.change_job(job['id'],status='failed')
        cancelled=self.store.enqueue('sdk_select','sdk',value);context=Context(self.store,cancelled)
        def stop_after_setup(*args,**kwargs):
            result=self.fake_setup(*args,**kwargs)
            self.store.cancel_job(cancelled['id']);return result
        with patch.object(sdk_packages,'verify',return_value={}),patch.object(toolsets,'run_setup',side_effect=stop_after_setup),patch.object(context,'run',return_value=('PowerShell fixture',0)):
            with self.assertRaises(Cancelled):worker.execute(context)
        self.assertEqual(self.store.setting('active_sdk'),'sdk-fixture')
        self.store.change_job(cancelled['id'],status='cancelled')
        retry=self.store.enqueue('sdk_select','sdk',value);context=Context(self.store,retry)
        with patch.object(sdk_packages,'verify',return_value={}),patch.object(toolsets,'run_setup',side_effect=self.fake_setup),patch.object(context,'run',return_value=('PowerShell fixture',0)):
            result=worker.execute(context)
        self.assertEqual(result['sdk_id'],'sdk-new')
        self.store.change_job(retry['id'],status='completed')
        progress_job=self.store.enqueue('sdk_select','sdk',value);context=Context(self.store,progress_job)
        raw=b'a'*(1024*1024+7);response=io.BytesIO(raw);response.headers={'Content-Length':str(len(raw))}
        destination=Path(self.temp.name)/'download.bin'
        with patch.object(sdk_packages.urllib.request,'urlopen',return_value=response):
            sdk_packages.fetch(context,'https://example.test/asset',destination,expected=hashlib.sha256(raw).hexdigest(),size=len(raw))
        progress=self.store.job(progress_job['id'])['result']['progress']
        self.assertEqual(progress['downloaded_bytes'],len(raw));self.assertEqual(progress['total_bytes'],len(raw));self.assertEqual(progress['percent'],100)
        context.phase('Checking the package');self.assertTrue(self.store.job(progress_job['id'])['result']['progress']['indeterminate'])

    def test_sdk_parallel_selection_and_legacy_actions_share_one_mutation_slot(self):
        value={'release_url':'https://github.com/R4OSDev/r4os-sdk/releases/tag/v0.64.146'}
        with ThreadPoolExecutor(max_workers=6) as pool:
            jobs=list(pool.map(lambda _:self.store.enqueue('sdk_select','sdk',value),range(6)))
        self.assertEqual(len({j['id'] for j in jobs}),1)
        for kind,payload in [('sdk_select',{'release_url':value['release_url'].replace('146','147')}),('tools_activate',{'id':'test-tools'}),('tools_install',{'sdk_id':'sdk-fixture'}),('sdk_import',value),('sdk_cleanup',{'confirmed':True})]:
            with self.assertRaises(Conflict):self.store.enqueue(kind,'sdk' if kind.startswith('sdk_') else 'tools',payload)
        self.store.cancel_job(jobs[0]['id'])
        job=self.store.enqueue('tools_activate','tools',{'id':'test-tools'});context=Context(self.store,job)
        self.store.cancel_job(job['id'])
        with patch.object(toolsets,'validate',return_value={}):
            with self.assertRaises(Cancelled):toolsets.perform(context,'activate',{'id':'test-tools'})
        self.assertEqual(self.store.setting('active_sdk'),'sdk-fixture')

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
        self.assertIsNone(toolsets.overview(self.store)['active'])
        with patch.object(sdk_catalog,'remote_catalog',return_value={'releases':[],'stale':True,'error':'offline'}):
            self.assertEqual(sdk_catalog.overview(self.store)['releases'][0]['state'],'download')
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
