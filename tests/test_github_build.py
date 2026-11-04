"""Real Git source pins and simulated GitHub mutations; no production releases."""
from concurrent.futures import ThreadPoolExecutor
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from r4os_server.workspace import builds, build_targets, catalog, connections, github_build, worker
from r4os_server.workspace.github_api import Client, GitHubFailure
from r4os_server.workspace.process import Context
from r4os_server.workspace.store import Store, Conflict


class FakeGitHub:
    def __init__(self, commit):
        self.commit=commit;self.tags={};self.releases={};self.data={};self.calls=[]
        self.check=lambda:None;self.fail=None;self.after_upload=None;self.after_publish=None;self.counter=10;self.credentials=[]

    def factory(self,repo,token,check):
        self.credentials.append((repo['id'],token));self.check=check;return self

    def tag_commit(self,tag):self.check();return self.tags.get(tag)

    def request(self,method,path,body=None,**options):
        self.check();self.calls.append((method,path,body))
        if path.startswith('/commits/'):return {'sha':self.commit}
        if path.startswith('/git/trees/'):
            commit=path.split('/')[-1].split('?')[0]
            raw=subprocess.check_output(['git','-C',str(self.source),'ls-tree','-rz',commit]);entries=[]
            for item in raw.rstrip(b'\0').split(b'\0'):
                if not item:continue
                meta,name=item.split(b'\t',1);mode,kind,sha=meta.decode().split();entries.append({'path':name.decode(),'mode':mode,'type':kind,'sha':sha})
            return {'truncated':False,'tree':entries}
        if path.startswith('/git/blobs/'):
            raw=subprocess.check_output(['git','-C',str(self.source),'cat-file','blob',path.rsplit('/',1)[-1]])
            return {'encoding':'base64','content':base64.b64encode(raw).decode(),'size':len(raw)}
        if path.startswith('/releases/tags/'):
            tag=path.split('/')[-1]
            return self.releases.get(tag)
        if method=='POST' and path=='/releases':
            if body['tag_name'] in self.releases:raise GitHubFailure(422,'Tag occupied')
            self.counter+=1;value=dict(body,id=self.counter);self.releases[body['tag_name']]=value;return value
        if method=='PATCH':
            release=next(value for value in self.releases.values() if value['id']==int(path.split('/')[-1]))
            release.update(body)
            if not release['draft']:self.tags[release['tag_name']]=release['target_commitish']
            if self.after_publish:
                callback=self.after_publish;self.after_publish=None;callback()
            return release
        if method=='DELETE':
            del self.data[int(path.split('/')[-1])];return None
        raise AssertionError((method,path))

    def assets(self,release):return [value[0] for value in self.data.values() if value[0]['release_id']==release['id']]
    def asset_bytes(self,asset):return self.data[asset['id']][1]
    def asset_digest(self,asset):return hashlib.sha256(self.asset_bytes(asset)).hexdigest()

    def upload(self,release,name,path):
        self.check()
        if self.fail==name:self.fail=None;raise GitHubFailure(503,'Simulated upload interruption')
        if any(a['name']==name for a in self.assets(release)):raise GitHubFailure(422,'Asset occupied')
        self.counter+=1;raw=Path(path).read_bytes()
        asset={'id':self.counter,'name':name,'size':len(raw),'state':'uploaded','release_id':release['id']}
        self.data[asset['id']]=(asset,raw)
        if self.after_upload:
            callback=self.after_upload;self.after_upload=None;callback()
        return asset


class GitHubBuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.store=Store(self.root/'workspace')
        connections.prepare_credentials(self.store.state,os.getuid(),os.getgid())
        conn=connections.get(self.store,'main')
        connections.save(self.store,{'url':conn['url'],'revision':conn['revision'],'token':'fake-github-release-token'},'main')
        self.source=self.store.root/'Repositories/Main/unusual-project';self.source.mkdir(parents=True)
        self.git('init','-b','main');self.git('config','user.name','Test');self.git('config','user.email','test@example.invalid')
        (self.source/'module.R4MF').write_text('R4OS_MODULE_MANIFEST=2\nKIND=R4X\nNAME=GAME\nVERSION=0.1.0\nLANGUAGE=Zig\nIMAGE_SCOPE=none\n')
        self.git('add','.');self.git('commit','-m','Initial source');self.commit=self.git('rev-parse','HEAD')
        meta={'id':5,'name':'unusual-project','display_name':'unusual-project','owner':'R4OSDev','default_branch':'main','private':False}
        self.module={'id':'GAME','name':'GAME','kind':'R4X','version':'0.1.0','language':'Zig','manifest':'module.R4MF','source_repositories':[],'supported':True}
        self.local={'commit':self.commit,'branch':'main','dirty':False,'status':'current','modules':[self.module]}
        with self.store.connect() as db:
            db.execute('INSERT INTO repositories(name,github_id,metadata,local,directory) VALUES(?,?,?,?,?)',('unusual-project',5,json.dumps(meta),json.dumps(self.local),'Main/unusual-project'))
        from sdk_fixture import install
        self.proof=install(self.store)
        self.fake=FakeGitHub(self.commit);self.build_count=0
        self.fake.source=self.source
        self.addCleanup(patch.stopall)
        patch.object(github_build,'Client',side_effect=self.fake.factory).start()
        patch.object(builds,'build',side_effect=self.fake_build).start()
        def remote(context,repo,*args,**options):
            if '--tags' not in args:return self.fake.commit+'\trefs/heads/main\n',0
            values=[]
            for tag,commit in self.fake.tags.items():
                if 'refs/tags/'+tag in args:values.append(commit+'\trefs/tags/'+tag)
            return '\n'.join(values),0 if values else 2
        patch.object(Context,'repository_git',autospec=True,side_effect=remote).start()

    def git(self,*args):
        return subprocess.run(['git','-C',str(self.source),*args],check=True,capture_output=True,text=True).stdout.strip()

    def fake_build(self,context,name,payload):
        self.build_count+=1
        folder=self.store.root/'Artifacts'/context.job['id'];(folder/'files').mkdir(parents=True)
        (folder/'files/GAME.R4X').write_bytes(b'compiled-target-'+payload['commit'].encode())
        (folder/'build.json').write_text(json.dumps({'commit':payload['commit'],'module':payload['module']}))
        return {'repository':name,'target':'GAME','commit':payload['commit'],'toolset':payload['toolset'],
                'sdk_profile':payload['sdk_profile'],'technical':False,'output':str(folder.relative_to(self.store.root)),'files':builds.register_outputs(self.store,folder)}

    def warm(self):
        self.store.enqueue('github_status','unusual-project')
        self.assertTrue(worker.once(self.store))
        return github_build.view(self.store,self.store.repository('unusual-project'))

    def enqueue(self):return github_build.enqueue(self.store,{'name':'unusual-project','target':'GAME'})

    def publish(self):
        self.warm();job=self.enqueue();worker.once(self.store)
        result=self.store.job(job['id']);self.assertEqual(result['status'],'completed',result['error']);return result

    def test_generic_owner_version_and_production_target_selection(self):
        self.assertEqual(builds.owner_relative('arbitrary',self.module),Path('Projects/arbitrary'))
        self.assertEqual(builds.owner_relative('r4os-games-minesweeper'),Path('Apps/minesweeper'))
        self.assertEqual(builds.owner_relative('r4os-app-example',self.module,'connection.5'),Path('Projects/connection.5'))
        repo=self.store.repository('unusual-project')
        pinned=build_targets.pinned(self.store,repo,self.module,self.commit)
        self.assertEqual(pinned['version'],'0.1.0')
        repo['local']['modules'] += [dict(self.module,id='FIXTURE',manifest='Fixtures/Test/module.R4MF')]
        self.assertEqual([m['id'] for m in build_targets.targets(repo)],['GAME'])
        (self.source/'module.R4MF').write_text((self.source/'module.R4MF').read_text().replace('0.1.0','9.9.9'))
        self.assertEqual(build_targets.pinned(self.store,repo,self.module,self.commit)['version'],'0.1.0')

    def test_pinned_git_errors_missing_files_and_real_manifest_mismatches_are_distinct(self):
        repo=self.store.repository('unusual-project')
        self.assertIsNone(build_targets.blob(self.store,repo,self.commit,'Cargo.toml'))
        with self.assertRaisesRegex(ValueError,'missing from the selected source commit'):
            build_targets.pinned(self.store,repo,dict(self.module,manifest='missing.R4MF'),self.commit)
        with self.assertRaisesRegex(ValueError,'Could not read the pinned build source'):
            build_targets.pinned(self.store,repo,self.module,'f'*40)
        tree=self.git('rev-parse',self.commit+'^{tree}')
        with self.assertRaisesRegex(ValueError,'not a Git commit'):
            build_targets.pinned(self.store,repo,self.module,tree)
        for relative in ('../module.R4MF','/module.R4MF',''):
            with self.assertRaisesRegex(ValueError,'Invalid build manifest path'):
                build_targets.blob(self.store,repo,self.commit,relative)
        for key,value in [('id','OTHER'),('kind','R4L')]:
            with self.assertRaisesRegex(ValueError,'differs from its pinned manifest'):
                build_targets.pinned(self.store,repo,dict(self.module,**{key:value}),self.commit)
        failed=subprocess.CompletedProcess([],128,stdout=b'',stderr=b'fatal: Permission denied (ghp_ExampleSecret12345)')
        with patch.object(build_targets.subprocess,'run',return_value=failed):
            with self.assertRaisesRegex(ValueError,'Permission denied') as error:
                build_targets.pinned(self.store,repo,self.module,self.commit)
        self.assertNotIn('ghp_ExampleSecret12345',str(error.exception))
        with patch.object(build_targets.subprocess,'run',side_effect=subprocess.TimeoutExpired('git',20)):
            with self.assertRaisesRegex(ValueError,'Git timed out'):
                build_targets.pinned(self.store,repo,self.module,self.commit)
        (self.source/'linked.R4MF').symlink_to('module.R4MF')
        self.git('add','linked.R4MF');self.git('commit','-m','Manifest symlink')
        with self.assertRaisesRegex(ValueError,'regular Git file'):
            build_targets.pinned(self.store,repo,dict(self.module,manifest='linked.R4MF'),self.git('rev-parse','HEAD'))
        outside=self.store.root/'Repositories/Main/link';outside.symlink_to(self.source,target_is_directory=True)
        with self.assertRaisesRegex(ValueError,'symbolic link'):
            build_targets.blob(self.store,dict(repo,directory='Main/link'),self.commit,'module.R4MF')
        (self.source/'Cargo.toml').write_text('[package]\nname="game"\nversion="0.2.0"\n')
        (self.source/'build.zig.zon').write_text('.{ .version = "0.3.0" }\n')
        self.git('add','Cargo.toml','build.zig.zon');self.git('commit','-m','Native project versions')
        commit=self.git('rev-parse','HEAD')
        self.assertEqual(build_targets.pinned(self.store,repo,dict(self.module,manifest='',language='Rust'),commit)['version'],'0.2.0')
        self.assertEqual(build_targets.pinned(self.store,repo,dict(self.module,manifest='',language='Zig'),commit)['version'],'0.3.0')

    def test_full_publish_disables_button_with_exact_tooltip_and_is_idempotent(self):
        job=self.publish();release=self.fake.releases['v0.1.0']
        self.assertFalse(release['draft']);self.assertEqual(self.fake.tags['v0.1.0'],self.commit)
        self.assertEqual(self.build_count,1)
        info=self.warm();self.assertEqual(info['state'],'published');self.assertFalse(info['enabled'])
        self.assertEqual(info['reason'],github_build.PUBLISHED)
        self.assertTrue(job['result']['github_release']['published'])
        with self.assertRaises(Conflict):self.enqueue()
        self.assertEqual(len([call for call in self.fake.calls if call[0]=='POST']),1)
        self.assertTrue(all(token=='fake-github-release-token' for _,token in self.fake.credentials))

    def test_single_refresh_rechecks_only_its_release_and_suppresses_stale_checks(self):
        self.assertTrue(self.warm()['enabled'])
        with self.store.connect() as db:
            db.execute('INSERT INTO repositories(name,github_id,metadata,directory) VALUES(?,?,?,?)',('other',6,json.dumps({'id':6,'name':'other','owner':'R4OSDev','display_name':'other'}),'Main/other'))
            db.execute('INSERT INTO github_release_status VALUES(?,?,?,?)',('other','keep-signature',1,'{"keep":true}'))
        job=self.store.enqueue('refresh','unusual-project');context=Context(self.store,job)
        github_build.view(self.store,self.store.repository('unusual-project'),schedule=True,force=True)
        self.assertFalse(any(item['kind']=='github_status' for item in self.store.active_jobs()))
        self.fake.tags['v0.1.0']='a'*40
        item={'id':5,'name':'unusual-project','owner':{'login':'R4OSDev'},'default_branch':'main','private':False,'description':'New repository metadata'}
        with patch.object(catalog,'public_json',return_value=(item,{})),patch.object(catalog,'inspect',return_value=self.local):
            result=worker.execute(context)
        self.store.change_job(job['id'],status='completed',result=result)
        current=github_build.view(self.store,self.store.repository('unusual-project'))
        self.assertFalse(current['enabled']);self.assertIn('Increase',current['reason'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT payload FROM github_release_status WHERE repository_name=?',('other',)).fetchone()[0],'{"keep":true}')
        retry=self.store.enqueue('refresh','unusual-project');context=Context(self.store,retry);self.fake.tags.clear()
        with patch.object(catalog,'public_json',return_value=(item,{})),patch.object(catalog,'inspect',return_value=self.local),patch.object(self.fake,'request',side_effect=GitHubFailure(503,'Release endpoint down')):
            with self.assertRaisesRegex(GitHubFailure,'Release endpoint down'):worker.execute(context)

    def test_artifact_currency_uses_remote_manifest_while_checkout_is_older(self):
        self.publish()
        path=self.source/'module.R4MF';path.write_text(path.read_text().replace('0.1.0','0.2.0'))
        self.git('add','.');self.git('commit','-m','Version 0.2.0');remote=self.git('rev-parse','HEAD')
        self.fake.commit=remote
        self.store.local('unusual-project',dict(self.local,commit=remote,modules=[dict(self.module,version='0.2.0')]))
        self.publish()
        self.git('reset','--hard',self.commit)
        self.store.local('unusual-project',dict(self.local,status='outdated',remote_commit=remote))
        current=self.warm();self.assertFalse(current['enabled']);self.assertEqual(current['artifact_state'],'current')
        self.assertEqual(current['artifacts'][0]['version'],'0.2.0');self.assertFalse(current['source_current'])
        self.assertEqual(self.git('rev-parse','HEAD'),self.commit);self.assertIn('0.1.0',path.read_text())
        del self.fake.releases['v0.2.0']
        current=self.warm();self.assertEqual(current['artifact_state'],'outdated');self.assertFalse(current['enabled'])

    def test_public_artifact_can_be_verified_without_checkout_or_active_sdk(self):
        self.publish();origin=self.root/'remote-source';self.source.rename(origin);self.fake.source=origin
        self.store.local('unusual-project',{'status':'missing'})
        self.store.set('active_sdk',None);self.store.set('active_toolset',None)
        conn=connections.get(self.store,'main');connections.save(self.store,{'url':conn['url'],'revision':conn['revision'],'remove_token':True},'main')
        current=self.warm();self.assertEqual(current['artifact_state'],'current');self.assertFalse(current['enabled'])
        self.assertNotIn('Authorization',Client(self.store.repository('unusual-project'),'').headers())
        self.assertFalse(self.source.exists());self.assertEqual(self.fake.credentials[-1][1],'')
        del self.fake.releases['v0.1.0'];current=self.warm()
        self.assertEqual(current['artifact_state'],'outdated');self.assertFalse(current['enabled'])
        request=self.fake.request
        def truncated(method,path,*args,**options):
            return {'truncated':True,'tree':[]} if path.startswith('/git/trees/') else request(method,path,*args,**options)
        with patch.object(self.fake,'request',side_effect=truncated):current=self.warm()
        self.assertEqual(current['artifact_state'],'unknown');self.assertFalse(self.source.exists())

    def test_artifact_missing_and_unknown_are_separate_from_sdk_readiness(self):
        with self.store.connect() as db:
            original=db.execute("SELECT metadata FROM repositories WHERE name='unusual-project'").fetchone()[0]
            metadata=json.loads(original);metadata['permissions']={'push':False}
            db.execute("UPDATE repositories SET metadata=? WHERE name='unusual-project'",(json.dumps(metadata),))
        current=self.warm();self.assertEqual(current['artifact_state'],'outdated');self.assertFalse(current['enabled']);self.assertIn('cannot write',current['reason'])
        with self.store.connect() as db:db.execute("UPDATE repositories SET metadata=? WHERE name='unusual-project'",(original,))
        self.store.set('active_sdk',None);self.store.set('active_toolset',None)
        current=self.warm();self.assertEqual(current['artifact_state'],'outdated');self.assertFalse(current['enabled'])
        with patch.object(self.fake,'request',side_effect=GitHubFailure(503,'Fixture unavailable')):current=self.warm()
        self.assertEqual(current['artifact_state'],'unknown');self.assertFalse(current['enabled'])
        github_build.view(self.store,self.store.repository('unusual-project'),schedule=True)
        self.assertEqual(self.store.active_jobs(),[])
        from r4os_server.workspace import artifact_source
        with patch.object(artifact_source,'head',side_effect=artifact_source.EmptySource('Empty GitHub source')):current=self.warm()
        self.assertEqual(current['artifact_state'],'not_applicable');self.assertFalse(current['enabled'])

    def test_multiple_targets_expose_missing_artifact_and_only_buildable_target(self):
        root=self.source/'module.R4MF';root.write_text(root.read_text().replace('0.1.0','0.2.0'))
        second=self.source/'Second/module.R4MF';second.parent.mkdir();second.write_text(root.read_text().replace('NAME=GAME','NAME=GAME2').replace('0.2.0','0.3.0'))
        self.git('add','.');self.git('commit','-m','Two production targets');commit=self.git('rev-parse','HEAD');self.fake.commit=commit
        self.store.local('unusual-project',dict(self.local,commit=commit,modules=[dict(self.module,version='0.2.0'),dict(self.module,id='GAME2',name='GAME2',version='0.3.0',manifest='Second/module.R4MF')]))
        self.publish();current=self.warm()
        self.assertEqual(current['artifact_state'],'outdated');self.assertTrue(current['enabled'])
        targets={item['id']:item for item in current['targets']}
        self.assertFalse(targets['GAME']['enabled']);self.assertTrue(targets['GAME2']['enabled'])
        job=github_build.enqueue(self.store,{'name':'unusual-project','target':'GAME2'})
        self.assertEqual(job['payload']['module']['version'],'0.3.0')

    def test_existing_tag_with_different_source_is_never_changed(self):
        self.fake.tags['v0.1.0']='a'*40
        info=self.warm();self.assertFalse(info['enabled']);self.assertIn('Increase',info['reason'])
        with self.assertRaises(Conflict):self.enqueue()
        self.assertEqual(self.fake.tags['v0.1.0'],'a'*40);self.assertEqual(self.build_count,0)
        self.assertFalse(any(method!='GET' for method,_,_ in self.fake.calls))

    def test_upload_failure_reuses_local_build_and_existing_assets(self):
        self.warm();self.fake.fail='r4os-build-GAME.json';first=self.enqueue();worker.once(self.store)
        failed=self.store.job(first['id']);self.assertEqual(failed['status'],'failed')
        self.assertTrue(failed['result']['local_build']);self.assertTrue(self.fake.releases['v0.1.0']['draft'])
        # An upload failure must leave the sealed local artifact downloadable.
        from dataclasses import replace
        from http.client import HTTPConnection
        import threading
        from test_benchmarks import configuration
        from WebApp.server import create_server
        config=replace(configuration(self.root/'data'),workspace_dir=str(self.store.root),project_root=str(self.root))
        server=create_server(config,port=0)
        threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True).start()
        try:
            artifact=next(item for item in failed['result']['files'] if item['name']=='files/GAME.R4X')
            connection=HTTPConnection('127.0.0.1',server.server_port,timeout=3)
            connection.request('GET','/api/repositories/jobs/'+first['id']+'/files/'+artifact['id'])
            response=connection.getresponse()
            self.assertEqual(response.status,200)
            self.assertEqual(hashlib.sha256(response.read()).hexdigest(),artifact['sha256'])
            connection.close()
        finally:
            server.shutdown();server.server_close()
        existing={asset['id'] for asset in self.fake.assets(self.fake.releases['v0.1.0'])}
        second=self.enqueue();worker.once(self.store)
        self.assertEqual(self.store.job(second['id'])['status'],'completed')
        self.assertEqual(self.build_count,1)
        self.assertTrue(existing.issubset({asset['id'] for asset in self.fake.assets(self.fake.releases['v0.1.0'])}))

    def test_cancelled_upload_can_resume_without_rebuilding(self):
        self.warm();first=self.enqueue();self.fake.after_upload=lambda:self.store.cancel_job(first['id'])
        worker.once(self.store);self.assertEqual(self.store.job(first['id'])['status'],'cancelled')
        self.assertTrue(self.fake.releases['v0.1.0']['draft'])
        second=self.enqueue();self.fake.after_publish=lambda:self.store.cancel_job(second['id']);worker.once(self.store)
        self.assertEqual(self.store.job(second['id'])['status'],'completed');self.assertEqual(self.build_count,1)

    def test_missing_or_modified_asset_is_not_claimed_as_published(self):
        self.publish();release=self.fake.releases['v0.1.0']
        asset=next(asset for asset in self.fake.assets(release) if asset['name']=='GAME.R4X')
        del self.fake.data[asset['id']]
        info=self.warm();self.assertEqual(info['state'],'conflict');self.assertNotEqual(info['reason'],github_build.PUBLISHED)
        with self.assertRaises(Conflict):self.enqueue()
        self.assertFalse(release['draft'])

    def test_source_and_connection_changes_invalidate_status_and_reject_stale_jobs(self):
        self.publish();(self.source/'later.txt').write_text('Later remote source');self.git('add','.');self.git('commit','-m','Remote source advanced');self.fake.commit=self.git('rev-parse','HEAD')
        self.assertEqual(self.warm()['state'],'outdated')
        self.fake.commit=self.commit;self.warm()
        conn=connections.get(self.store,'main')
        connections.save(self.store,{'url':conn['url'],'revision':conn['revision'],'token':'changed-fake-github-token'},'main')
        info=github_build.view(self.store,self.store.repository('unusual-project'))
        self.assertEqual(info['state'],'checking');self.assertFalse(info['enabled'])

    def test_duplicate_requests_share_one_job_and_recovery_preserves_published_result(self):
        self.warm()
        with ThreadPoolExecutor(max_workers=3) as pool:jobs=list(pool.map(lambda _:self.enqueue(),range(3)))
        self.assertEqual(len({job['id'] for job in jobs}),1)
        self.assertEqual(self.enqueue()['id'],jobs[0]['id'])
        self.store.change_job(jobs[0]['id'],status='running',result={'github_release':{'published':True}})
        self.store.recover();self.assertEqual(self.store.job(jobs[0]['id'])['status'],'completed')

    def test_queued_older_source_is_rejected_before_publishing(self):
        self.warm();job=self.enqueue()
        (self.source/'new-source.txt').write_text('A later local checkout.')
        self.git('add','.');self.git('commit','-m','New checkout after enqueue')
        latest=self.git('rev-parse','HEAD')
        self.store.local('unusual-project',dict(self.local,commit=latest));self.fake.commit=latest
        worker.once(self.store)
        finished=self.store.job(job['id']);self.assertEqual(finished['status'],'failed')
        self.assertIn('GitHub source changed',finished['error']);self.assertNotIn('v0.1.0',self.fake.tags)
        self.assertEqual(self.build_count,0)
        info=github_build.view(self.store,self.store.repository('unusual-project'))
        self.assertNotEqual(info['state'],'published');self.assertFalse(info['enabled'])
        self.assertEqual(self.warm()['state'],'ready')

    def test_unknown_github_status_and_dirty_source_never_enable_build(self):
        with patch.object(self.fake,'request',side_effect=GitHubFailure(403,'Denied')):
            self.assertEqual(self.warm()['state'],'unknown')
        local=dict(self.local,dirty=True);self.store.local('unusual-project',local)
        info=github_build.view(self.store,self.store.repository('unusual-project'),schedule=True)
        self.assertFalse(info['enabled']);self.assertIn('Local changes',info['reason'])

    def test_old_receipt_and_changed_sdk_never_claim_published(self):
        self.publish();release=self.fake.releases['v0.1.0']
        asset=next(a for a in self.fake.assets(release) if a['name']=='r4os-build-GAME.json')
        receipt=json.loads(self.fake.asset_bytes(asset));receipt['schema']=1
        self.fake.data[asset['id']]=(asset,json.dumps(receipt).encode())
        self.assertEqual(self.warm()['state'],'conflict')
        receipt['schema']=2;receipt['sdk_profile']['profile_sources']['Build']='9'*64
        self.fake.data[asset['id']]=(asset,json.dumps(receipt).encode())
        self.assertEqual(self.warm()['state'],'conflict')
        self.assertFalse(any(call[0]=='DELETE' for call in self.fake.calls))

    def test_sdk_settings_invalidate_cache_and_technical_publish_is_rejected(self):
        self.publish();from r4os_server.workspace import sdk_projects
        with self.store.connect() as db:db.execute("INSERT INTO sdk_projects VALUES(?,?,?,?)",('unusual-project',None,'[]','explicit-technical'))
        info=github_build.view(self.store,self.store.repository('unusual-project'))
        self.assertFalse(info['enabled']);self.assertEqual(info['state'],'checking')
        with self.assertRaisesRegex(Conflict,'Technical'):self.enqueue()

    def test_asset_transport_drops_auth_on_signed_redirect_and_uploads_binary(self):
        raw=b'artifact';requests=[]
        class Response:
            def __init__(self,status,data=b'',headers=None):self.status=status;self.body=io.BytesIO(data);self.headers=headers or {}
            def read(self,size=-1):return self.body.read(size)
            def getheader(self,key,default=None):return self.headers.get(key,default)
        responses=[Response(302,headers={'Location':'https://release-assets.githubusercontent.com/object?signature=signed'}),Response(200,raw),Response(201,b'{"id":1}'),
                   Response(302,headers={'Location':'https://release-assets.githubusercontent.com/object?signature=signed'}),Response(200,raw),Response(200,b'corrupt!')]
        class Connection:
            def __init__(self,host,**kwargs):self.host=host
            def request(self,method,path,body=None,headers=None):
                if hasattr(body,'read'):
                    chunks=[]
                    while chunk:=body.read(8192):chunks.append(chunk)
                    body=b''.join(chunks)
                requests.append((self.host,method,path,body,headers))
            def getresponse(self):return responses.pop(0)
            def close(self):pass
        client=Client(self.store.repository('unusual-project'),'fake-http-token')
        asset={'id':1,'size':len(raw)}
        path=self.root/'artifact.R4X';path.write_bytes(raw)
        with patch('r4os_server.workspace.github_api.HTTPSConnection',Connection):
            self.assertEqual(client.asset_bytes(asset),raw)
            client.upload({'id':2},'artifact.R4X',path)
            downloaded=self.root/'downloaded.R4X';progress=[]
            client.download(asset,downloaded,hashlib.sha256(raw).hexdigest(),progress.append)
            self.assertEqual(downloaded.read_bytes(),raw);self.assertEqual(progress[-1],len(raw))
            with self.assertRaises(GitHubFailure):client.download(asset,self.root/'bad.R4X',hashlib.sha256(raw).hexdigest())
        self.assertIn('Authorization',requests[0][4]);self.assertNotIn('Authorization',requests[1][4])
        self.assertEqual(requests[2][0],'uploads.github.com');self.assertEqual(requests[2][3],raw)
        self.assertEqual(requests[2][4]['Content-Length'],str(len(raw)))
        self.assertIn('Authorization',requests[3][4]);self.assertNotIn('Authorization',requests[4][4])
