"""Focused new-service integration checks using the inherited artifact fixtures."""
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection, HTTPSConnection
import json
import hashlib
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from r4os_server.common.auth import password_hash
from r4os_server.common.config import Config
from r4os_server.registration.service import create_server as registration_server
from r4os_server.updates.service import create_server as updates_server
from r4u_fixture import create_r4u
from release_fixture import create_release_manifest


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config=Config(data_dir=Path(self.temp.name),registration_port=0,updates_port=0,
            registration_secret="test-registration-secret-00000001",
            publisher_token="test-publisher-token-000000000001",
            benchmark_token="test-benchmark-token-000000000001",
            admin_token="test-administrator-token-00000001",
            session_secret="test-session-secret-000000000001",
            admin_password_hash=password_hash("test-operator-password"))
        self.registration=self.start(registration_server(self.config))
        self.updates=self.start(updates_server(self.config))
        self.updates.catalog.base_url=f"http://127.0.0.1:{self.updates.server_port}"

    def start(self,server):
        thread=threading.Thread(target=server.serve_forever,kwargs={"poll_interval":.01},daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self,method,path,data=None,token=None,headers=None,raw=None,server=None):
        connection=HTTPConnection("127.0.0.1",(server or self.updates).server_port,timeout=5)
        request_headers=dict(headers or {})
        if token:
            request_headers["Authorization"]="Bearer "+token
        if data is not None:
            request_headers["Content-Type"]="application/json"
            raw=json.dumps(data).encode()
        connection.request(method,path,body=raw,headers=request_headers)
        response=connection.getresponse()
        body=response.read();status=response.status;response_headers=dict(response.getheaders())
        connection.close()
        if response_headers.get("Content-Type","").startswith("application/json") and body:
            body=json.loads(body)
        return status,body,response_headers

    def register(self):
        with socket.create_connection(self.registration.server_address,timeout=5) as client:
            client.sendall(self.config.registration_secret.encode())
            self.assertEqual(client.recv(2),b"OK")

    def software_fixture(self):
        from test_apps import AppTests
        from r4os_server.software.service import create_server
        case=AppTests();case.setUp();self.addCleanup(case.doCleanups)
        case.test_library_and_kernel_use_release_types_and_explicit_runtime_roles()
        fake=case.remote_app();case.download_release()
        server=self.start(create_server(replace(self.config,software_port=0,workspace_dir=str(case.store.root))))
        self.register()
        return server,case,fake

    def test_software_catalog_types_dependencies_etag_and_local_availability(self):
        from r4os_server.workspace import apps
        server,case,fake=self.software_fixture()
        status,value,headers=self.request('GET','/api/v1/packages',server=server)
        self.assertEqual(status,200);self.assertEqual(value['schema_version'],1)
        self.assertEqual({p['package_type'] for p in value['packages']},{'application','kernel','library'})
        application=next(p for p in value['packages'] if p['package_type']=='application')
        library=next(p for p in value['packages'] if p['package_type']=='library')
        kernel=next(p for p in value['packages'] if p['package_type']=='kernel')
        self.assertEqual(application['runtime_dependencies'][0]['package_id'],library['package_id'])
        self.assertEqual(library['installation']['path'],'/R4OS/LIBS/R4IMG.R4L')
        self.assertIsNone(kernel['installation']['path']);self.assertTrue(kernel['installation']['requires_client_plan'])
        self.assertTrue(all(p['compatibility']['architecture'] is None for p in value['packages']))
        self.assertTrue(all(len(p['assets'])==3 for p in value['packages']))
        self.assertNotIn(str(case.root),json.dumps(value));self.assertNotIn('connection_id',json.dumps(value))
        self.assertEqual(self.request('GET','/api/v1/packages',headers={'If-None-Match':headers['ETag']},server=server)[0],304)
        status,body,head=self.request('HEAD','/api/v1/packages',server=server)
        self.assertEqual((status,body),(200,b''));self.assertGreater(int(head['Content-Length']),0)
        apps.save(case.store,'minesweeper',case.settings(display_name='New name'))
        self.assertNotEqual(self.request('GET','/api/v1/packages',server=server)[2]['ETag'],headers['ETag'])
        # Idle Stores retain WAL sidecars for a service with a read-only state
        # mount, without holding a snapshot that blocks writer checkpoints.
        self.assertTrue(Path(str(case.store.database)+'-wal').is_file())
        self.assertTrue(Path(str(case.store.database)+'-shm').is_file())
        with case.store.connect() as db:
            self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)
        self.assertEqual(self.request('GET','/api/v1/packages',server=server)[0],200)
        with case.store.connect() as db:
            db.execute("UPDATE repositories SET present=0 WHERE name='typed-2'")
        self.assertEqual(len(self.request('GET','/api/v1/packages',server=server)[1]['packages']),3)
        apps.publish(case.store,'minesweeper',{'published':False})
        self.assertEqual(len(self.request('GET','/api/v1/packages',server=server)[1]['packages']),2)
        import sqlite3
        with server.catalog.connect() as db,self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM software_packages')

    def test_software_catalog_rejects_corrupt_symlink_assets_and_conflicting_identities(self):
        from r4os_server.workspace import app_storage,apps,connections
        server,case,_=self.software_fixture()
        status,value,_=self.request('GET','/api/v1/packages',server=server);self.assertEqual(status,200)
        library=next(p for p in value['packages'] if p['package_type']=='library')
        conn=connections.save(case.store,{'url':'https://github.com/Other'})
        repo=case.seed('duplicate',2,conn['id']);root=case.store.root/'Repositories'/repo['directory']
        (root/'module.R4MF').write_text('R4OS_MODULE_MANIFEST=2\nKIND=R4L\nNAME=R4IMG\nVERSION=0.1.0\nTARGET=/R4OS/LIBS/R4IMG.R4L\n')
        apps.publish(case.store,'duplicate',{'published':True})
        with case.store.connect() as db:db.execute('INSERT INTO app_release_refs VALUES(?,?,?,1)',('duplicate',2,11))
        apps.save(case.store,'duplicate',case.settings(display_name='Different name'))
        self.assertEqual(self.request('GET','/api/v1/packages',server=server)[0],409)
        apps.publish(case.store,'duplicate',{'published':False})
        item=next(p for p in apps.overview(case.store) if p['package_type']=='library')
        folder=app_storage.path(case.store,item['release']['local']['directory']);binary=folder/'R4IMG.R4L';original=binary.read_bytes()
        binary.write_bytes(b'x'*len(original))
        value=self.request('GET','/api/v1/packages',server=server)[1]
        self.assertNotIn(library['package_id'],[p['package_id'] for p in value['packages']]);self.assertEqual(len(value['unavailable']),1)
        binary.unlink();outside=case.root/'outside';outside.write_bytes(original);binary.symlink_to(outside)
        self.assertEqual(len(self.request('GET','/api/v1/packages',server=server)[1]['unavailable']),1)

    def test_software_file_types_ranges_conditionals_and_open_descriptor_removal(self):
        from r4os_server.workspace import app_storage
        server,case,_=self.software_fixture()
        values=self.request('GET','/api/v1/packages',server=server)[1]['packages']
        for package in values:
            artifact=next(a for a in package['assets'] if a['role']=='artifact');url=artifact['download_url']
            status,raw,headers=self.request('GET',url,server=server)
            self.assertEqual(status,200);self.assertEqual(len(raw),artifact['size_bytes'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(),artifact['sha256'])
            self.assertEqual(headers['X-Checksum-SHA256'],artifact['sha256'])
            for value,expected in [('bytes=0-0',raw[:1]),('bytes=1-',raw[1:]),('bytes=-7',raw[-7:]),('bytes=0-999999',raw)]:
                status,body,partial=self.request('GET',url,headers={'Range':value,'If-Range':headers['ETag']},server=server)
                self.assertEqual((status,body),(206,expected));self.assertEqual(int(partial['Content-Length']),len(expected))
            for value in ('bytes=999999-','bytes=-0','bytes=0-0,2-3'):
                status,body,partial=self.request('GET',url,headers={'Range':value},server=server)
                self.assertEqual(status,416);self.assertEqual(partial['Content-Range'],'bytes */'+str(len(raw)))
            self.assertEqual(self.request('GET',url,headers={'Range':'bytes=1-','If-Range':'"different"'},server=server)[:2],(200,raw))
            self.assertEqual(self.request('GET',url,headers={'If-None-Match':headers['ETag']},server=server)[:2],(304,b''))
            status,body,head=self.request('HEAD',url,headers={'Range':'bytes=1-'},server=server)
            self.assertEqual((status,body),(200,b''));self.assertEqual(int(head['Content-Length']),len(raw))
        application=next(p for p in values if p['package_type']=='application')
        url=next(a['download_url'] for a in application['assets'] if a['role']=='artifact')
        original=server.catalog.open_asset
        def remove_after_open(row,asset):
            stream=original(row,asset);app_storage.remove(case.store,{'minesweeper'});return stream
        with patch.object(server.catalog,'open_asset',side_effect=remove_after_open):
            status,raw,_=self.request('GET',url,server=server)
        self.assertEqual(status,200)
        self.assertEqual(self.request('GET',url,server=server)[0],404)
        self.assertEqual(self.request('GET','/api/v1/packages/gh-1-..%2Fetc/files/1',server=server)[0],404)

    def test_software_downloads_keep_release_urls_and_recheck_publication_and_bans(self):
        from r4os_server.workspace import apps
        server,case,fake=self.software_fixture()
        item=next(p for p in self.request('GET','/api/v1/packages',server=server)[1]['packages'] if p['package_type']=='application')
        old=next(a for a in item['assets'] if a['role']=='artifact')
        old_body=self.request('GET',old['download_url'],server=server)[1]
        fake.set_release('0.2.0',12);case.refresh_release();case.download_release('update')
        current=next(p for p in self.request('GET','/api/v1/packages',server=server)[1]['packages'] if p['package_type']=='application')
        new=next(a for a in current['assets'] if a['role']=='artifact')
        self.assertNotEqual(new['download_url'],old['download_url'])
        self.assertEqual(self.request('GET',old['download_url'],server=server)[1],old_body)
        apps.publish(case.store,'minesweeper',{'published':False})
        self.assertEqual(self.request('GET',old['download_url'],server=server)[0],404)
        self.assertEqual(self.request('GET',new['download_url'],server=server)[0],404)
        server.policy.store.block_ip('127.0.0.1','fixture',60)
        self.assertEqual(self.request('GET',old['download_url'],headers={'If-None-Match':'"'+old['sha256']+'"'},server=server)[0],403)

    def test_software_uses_shared_registration_revocation_ban_and_rate_limit(self):
        from r4os_server.software.service import create_server
        software=self.start(create_server(replace(self.config,software_port=0)))
        self.assertEqual(self.request('GET','/api/health',server=software)[1]['service'],'software')
        self.assertEqual(self.request('GET','/api/v1/packages',server=software)[0],401)
        self.register()
        self.assertEqual(self.request('GET','/api/v1/packages',server=software)[0],503)
        software.policy.store.set_rate('software',1)
        with patch.object(software.policy.limiter,'allow',return_value=False):
            self.assertEqual(self.request('HEAD','/api/v1/packages',server=software)[0],429)
        software.policy.store.set_rate('software',100)
        # Direct shared-state revocation is also what the existing settings API uses.
        software.policy.store.revoke('127.0.0.1')
        self.assertEqual(self.request('HEAD','/api/v1/packages',server=software)[0],401)
        self.assertEqual(self.request('GET','/api/v1/packages',server=software)[0],401)
        self.assertEqual(self.request('GET','/api/v1/packages',server=software)[0],403)
        self.assertEqual(self.request('GET','/catalog',server=self.updates)[0],403)

    def test_global_jobs_include_both_queues_without_recent_job_limit(self):
        from WebApp.server import create_server
        web=self.start(create_server(replace(self.config,updates_port=self.updates.server_port),port=0))
        for number in range(40):web.workspace.enqueue('github_status','fixture-'+str(number))
        with self.updates.uploads.store._connect() as db:
            for number,status in enumerate(('receiving','queued','validating','valid')):
                db.execute('INSERT INTO uploads VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (str(number)*32,'package',status,None,1,None,None,None,'2026-11-04T00:00:00Z','2026-11-04T00:00:00Z','127.0.0.1','fixture'))
        self.assertEqual(self.request('GET','/api/updates/jobs')[0],401)
        status,value,_=self.request('GET','/api/jobs',server=web)
        self.assertEqual(status,200);self.assertTrue(value['complete']);self.assertEqual(value['count'],43)
        self.assertEqual(len({job['key'] for job in value['jobs']}),43)
        self.assertEqual(sum(job['source']=='updates' for job in value['jobs']),3)
        self.assertEqual(self.request('GET','/api/jobs',headers={'Host':'invalid.example'},server=web)[0],403)
        with patch.object(self.updates.uploads,'active',side_effect=ValueError('Fixture unavailable')):
            partial=self.request('GET','/api/jobs',server=web)[1]
        self.assertFalse(partial['complete']);self.assertIsNone(partial['count']);self.assertEqual(partial['known_count'],40)
        import sqlite3
        with patch.object(web.workspace,'active_jobs',side_effect=sqlite3.OperationalError('Fixture unavailable')):
            partial=self.request('GET','/api/jobs',server=web)[1]
        self.assertFalse(partial['sources']['workspace']);self.assertEqual(partial['known_count'],3)

    def test_shared_registration_roles_and_untrusted_forwarded_ip(self):
        self.assertEqual(self.request("GET","/catalog")[0],401)
        self.assertEqual(self.request("GET","/catalog",headers={"X-Real-IP":"10.20.30.40"})[0],401)
        self.register()
        status,body,_=self.request("GET","/catalog")
        self.assertEqual(status,200)
        self.assertEqual(body["releases"],[])
        self.assertEqual(self.request("GET","/api/settings/state")[0],401)
        self.assertEqual(self.request("GET","/api/settings/state",token=self.config.publisher_token)[0],403)
        status,body,_=self.request("GET","/api/settings/state",token=self.config.admin_token)
        self.assertEqual(status,200)
        self.assertEqual(body["allowed"][0]["ip"],"127.0.0.1")
        self.assertEqual(self.request("PUT","/api/settings/rates",data={"benchmarks":75},token=self.config.admin_token)[0],200)
        self.assertEqual(self.registration.store.get_rate("benchmarks"),75)

    def test_sdk_downloads_require_registration_and_preserve_original_bytes(self):
        root=self.updates.sdk_storage/'SDK';payload=root/'Packages/sdk-readback/Payload';payload.mkdir(parents=True)
        raw=b'original SDK asset';sha=hashlib.sha256(raw).hexdigest();(payload/'original.bin').write_bytes(raw)
        (root/'catalog.json').write_text(json.dumps({'schema':1,'interface':'r4os-sdk-package','packages':[{'id':'sdk-readback','assets':[{'path':'original.bin','role':'tools','bytes':len(raw),'sha256':sha}]}]}))
        self.assertEqual(self.request('GET','/sdk/catalog')[0],401);self.register()
        status,catalog,_=self.request('GET','/sdk/catalog');self.assertEqual(status,200)
        self.assertIn('/sdk/assets/sdk-readback/original.bin',catalog['packages'][0]['assets'][0]['download_url'])
        status,body,headers=self.request('GET','/sdk/assets/sdk-readback/original.bin');self.assertEqual((status,body),(200,raw));self.assertEqual(headers['ETag'],'"'+sha+'"')
        status,body,headers=self.request('GET','/sdk/assets/sdk-readback/original.bin',headers={'Range':'bytes=0-3'});self.assertEqual((status,body),(206,raw[:4]))
        self.assertEqual(self.request('HEAD','/sdk/assets/sdk-readback/original.bin')[1],b'')
        self.assertEqual(self.request('GET','/sdk/assets/sdk-readback/unknown.bin')[0],404)
        (payload/'original.bin').write_bytes(b'X'*len(raw))
        self.assertEqual(self.request('GET','/sdk/assets/sdk-readback/original.bin')[0],503)

    def test_invalid_magic_blocks_and_admin_can_unblock(self):
        with socket.create_connection(self.registration.server_address,timeout=5,source_address=("127.0.0.2",0)) as client:
            client.sendall(b"invalid\n")
            self.assertEqual(client.recv(2),b"")
        self.assertTrue(self.updates.store.is_blocked("127.0.0.2"))
        self.assertEqual(self.request("POST","/api/settings/unblock",data={"ip":"127.0.0.2"},token=self.config.admin_token)[0],200)
        self.assertFalse(self.registration.store.is_blocked("127.0.0.2"))

    def test_public_tls_registration_shares_access_but_not_write_rights(self):
        from r4os_server.registration.service import create_public_server as public_registration
        from r4os_server.updates.service import create_public_server as public_updates
        from r4os_server.benchmarks.service import create_server as benchmarks, create_public_server as public_benchmarks
        from r4os_server.software.service import create_server as software_server,create_public_server as public_software
        from r4os_server.workspace.store import Store
        folder=Path(self.temp.name)
        certificate,key=folder/'cert.pem',folder/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=isolated-test','-addext','subjectAltName=IP:127.0.0.1,IP:127.0.0.2',
                        '-out',str(certificate),'-keyout',str(key)],capture_output=True,check=True)
        Store(folder/'workspace')
        config=replace(self.config,public_api_host='127.0.0.2',benchmarks_port=0,software_port=0,workspace_dir=str(folder/'workspace'),
                       tls_cert=str(certificate),tls_key=str(key),tls_ca=str(certificate))
        registration=self.start(public_registration(config,self.registration))
        updates=self.start(public_updates(config,self.updates))
        private_benchmarks=self.start(benchmarks(config))
        benchmark=self.start(public_benchmarks(config,private_benchmarks))
        private_software=self.start(software_server(config));software=self.start(public_software(config,private_software))
        context=ssl.create_default_context(cafile=str(certificate))
        def request(server,path,method='GET',body=None,token=None,hostname='127.0.0.2'):
            client=HTTPSConnection(hostname,server.server_port,context=context,timeout=3,
                                   source_address=('127.0.0.3',0))
            headers={'Content-Type':'application/json','X-Real-IP':'198.51.100.12'}
            if token:headers['Authorization']='Bearer '+token
            try:
                client.request(method,path,body=None if body is None else json.dumps(body),headers=headers)
                response=client.getresponse()
                payload=response.read()
                return response.status,(json.loads(payload) if response.getheader('Content-Type','').startswith('application/json') else payload)
            finally:
                client.close()
        from test_benchmarks import CLAIM_ID, claim_document
        claim_route='/api/v1/hosts/'+CLAIM_ID+'/claim'
        self.assertEqual(request(updates,'/catalog.json')[0],401)
        self.assertEqual(request(software,'/api/v1/packages')[0],401)
        self.assertEqual(request(benchmark,claim_route,'POST',claim_document(),config.benchmark_token)[0],401)
        bootstrap='/api/v1/registration/bootstrap'
        self.assertEqual(request(private_software,bootstrap,'POST',hostname='127.0.0.1')[0],404)
        self.assertEqual(request(software,bootstrap)[0],405)
        status,document=request(software,bootstrap,'POST')
        self.assertEqual(status,201)
        fields=dict(line.split('=',1) for line in document.decode('ascii').splitlines())
        self.assertEqual((fields['SCHEMA'],fields['VERSION'],fields['SCOPE']),
                         ('R4OS_PUBLIC_REGISTRATION','1','REGISTRATION_READ'))
        token=fields['REGISTRATION_SECRET']
        self.assertEqual(len(token),69)
        self.assertNotEqual(token,config.registration_secret)
        # Issuance alone does not register an address or confer any write role.
        self.assertNotIn('127.0.0.3',{item['ip'] for item in self.registration.store.list_allowed()})
        with socket.create_connection(registration.server_address,source_address=('127.0.0.3',0),timeout=3) as raw:
            with context.wrap_socket(raw,server_hostname='127.0.0.2') as client:
                for fragment in (token[:2],token[2:31],token[31:]):
                    client.sendall(fragment.encode('ascii'));time.sleep(.01)
                self.assertEqual(client.recv(2),b'OK')
        self.assertEqual(request(benchmark,claim_route,'POST',claim_document(),token)[0],401)
        # A deliberately overlapping legacy prefix remains unframed-compatible.
        registration.magic_number=b'R4C1_'+b'1'*80
        with socket.create_connection(registration.server_address,source_address=('127.0.0.3',0),timeout=3) as raw:
            with context.wrap_socket(raw,server_hostname='127.0.0.2') as client:
                for fragment in (registration.magic_number[:3],registration.magic_number[3:69],registration.magic_number[69:]):
                    client.sendall(fragment);time.sleep(.01)
                self.assertEqual(client.recv(2),b'OK')
        with patch.object(software.bootstrap_limiter,'allow',return_value=False):
            self.assertEqual(request(software,bootstrap,'POST')[0],429)
        self.assertEqual(request(software,bootstrap,'POST',{'unexpected':'body'})[0],413)
        self.assertEqual(request(updates,'/catalog.json')[0],200)
        self.assertEqual(request(software,'/api/v1/packages')[0],200)
        self.assertEqual(request(private_software,'/api/v1/packages',hostname='127.0.0.1')[0],200)
        self.assertEqual(request(software,'/api/v1/packages','POST',{})[0],405)
        allowed={item['ip'] for item in self.registration.store.list_allowed()}
        self.assertIn('127.0.0.3',allowed)
        self.assertNotIn('198.51.100.12',allowed)
        self.assertEqual(request(benchmark,'/api/v1/hosts/public-client','PUT',{'display_name':'Client'})[0],401)
        self.assertEqual(request(benchmark,'/api/v1/hosts/public-client','PUT',{'display_name':'Client'},config.benchmark_token)[0],201)
        self.assertEqual(request(benchmark,'/api/v1/hosts')[0],200)
        self.assertEqual(request(benchmark,claim_route,'POST',claim_document())[0],401)
        self.assertEqual(request(benchmark,claim_route,'POST',claim_document(),config.publisher_token)[0],403)
        status,claimed=request(benchmark,claim_route,'POST',claim_document(),config.benchmark_token)
        self.assertEqual((status,claimed['created']),(201,True))
        status,repeated=request(private_benchmarks,claim_route,'POST',claim_document('New suggestion'),config.benchmark_token,hostname='127.0.0.1')
        self.assertEqual((status,repeated['created'],repeated['host']),(200,False,claimed['host']))
        self.assertEqual(request(benchmark,claim_route,'POST',claim_document(digest='c'*64),config.benchmark_token)[0],409)
        with socket.create_connection(registration.server_address,timeout=3) as client:
            client.sendall(b'plain-magic-is-not-tls\n')
            try:reply=client.recv(2)
            except OSError:reply=b''
            self.assertNotEqual(reply,b'OK')
        insecure=HTTPSConnection('127.0.0.2',updates.server_port,timeout=3)
        try:
            with self.assertRaises(ssl.SSLCertVerificationError):
                insecure.request('GET','/api/health')
        finally:
            insecure.close()
        self.registration.store.block_ip('127.0.0.3','fixture',60)
        self.assertEqual(request(software,bootstrap,'POST')[0],403)

    def wait_upload(self,identifier):
        deadline=time.monotonic()+5
        while time.monotonic()<deadline:
            status,item,_=self.request("GET","/api/updates/uploads/"+identifier,token=self.config.publisher_token)
            self.assertEqual(status,200)
            if item['status'] not in ('receiving','queued','validating'):
                return item
            time.sleep(.01)
        self.fail('Prüfung nicht innerhalb des Zeitlimits beendet')

    def upload(self,kind,path):
        status,item,_=self.request("PUT","/api/updates/uploads/"+kind,raw=path.read_bytes(),
            token=self.config.publisher_token,headers={"Content-Type":"application/octet-stream" if kind=='package' else 'application/json'})
        self.assertEqual(status,202,item)
        return self.wait_upload(item['id'])

    def test_upload_publish_manifest_last_and_exact_range_downloads(self):
        package=Path(self.temp.name)/'fixture.R4U'
        create_r4u(package)
        uploaded=self.upload('package',package)
        self.assertEqual(uploaded['status'],'valid',uploaded)
        self.assertEqual(uploaded['sha256'],hashlib.sha256(package.read_bytes()).hexdigest())
        manifest=Path(self.temp.name)/'release.json'
        create_release_manifest(manifest)
        release=self.upload('manifest',manifest)
        self.assertEqual(release['status'],'valid',release)
        def publish(item,token):
            return self.request('POST','/api/updates/uploads/'+item['id']+'/publish',data={},token=token)
        self.assertEqual(publish(release,self.config.admin_token)[0],409)
        self.assertEqual(publish(uploaded,self.config.publisher_token)[0],403)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(lambda _: publish(uploaded,self.config.admin_token)[0],range(2)))
        self.assertEqual(results,[200,200])
        self.assertEqual(publish(uploaded,self.config.admin_token)[0],200)
        self.assertEqual(publish(release,self.config.admin_token)[0],200)
        self.assertEqual((self.updates.catalog.release_root/release['filename']).read_bytes(),manifest.read_bytes())
        self.register()
        status,catalog,_=self.request('GET','/catalog.json')
        self.assertEqual(status,200)
        entry=catalog['releases'][0]['packages'][0]
        self.assertTrue(entry['download_url'].startswith(self.updates.catalog.base_url+'/'))
        route='/packages/'+entry['filename']
        original=package.read_bytes()
        self.assertEqual(self.request('GET',route)[1],original)
        self.assertEqual(self.upload('package',package)['status'],'published')
        status,body,headers=self.request('HEAD',route)
        self.assertEqual((status,body,int(headers['Content-Length'])),(200,b'',len(original)))
        for spec,expected in (('bytes=4-19',original[4:20]),('bytes=10-',original[10:]),('bytes=-11',original[-11:])):
            status,body,headers=self.request('GET',route,headers={'Range':spec})
            self.assertEqual((status,body),(206,expected))
            self.assertIn('Content-Range',headers)
        self.assertEqual(self.request('GET',route,headers={'Range':'bytes=99999999-'})[0],416)
        changed=Path(self.temp.name)/'changed.R4U'
        create_r4u(changed,title='Different bytes')
        self.assertEqual(self.upload('package',changed)['status'],'invalid')
        self.assertEqual(self.request('GET',route)[1],original)

    def test_invalid_oversized_and_interrupted_uploads_stay_outside_catalog(self):
        status,_,_=self.request('PUT','/api/updates/uploads/package',raw=b'bad',headers={'Content-Type':'application/octet-stream'})
        self.assertEqual(status,401)
        bad=Path(self.temp.name)/'invalid.R4U';bad.write_bytes(b'bad')
        self.assertEqual(self.upload('package',bad)['status'],'invalid')
        connection=HTTPConnection('127.0.0.1',self.updates.server_port,timeout=5)
        connection.putrequest('PUT','/api/updates/uploads/package')
        connection.putheader('Authorization','Bearer '+self.config.publisher_token)
        connection.putheader('Content-Type','application/octet-stream')
        connection.putheader('Content-Length','1000')
        connection.endheaders();connection.send(b'short');connection.sock.shutdown(socket.SHUT_WR)
        response=connection.getresponse();self.assertEqual(response.status,400);response.read();connection.close()
        self.assertEqual(list(self.updates.uploads.staging.glob('*.part')),[])
        self.assertEqual(self.updates.catalog.list_incoming(),[])
        self.assertEqual(self.request('PUT','/api/updates/uploads/package',raw=b'',token=self.config.publisher_token,
            headers={'Content-Length':str(self.config.upload_limit+1),'Content-Type':'application/octet-stream'})[0],413)
