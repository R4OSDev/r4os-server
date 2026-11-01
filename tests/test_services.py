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
        folder=Path(self.temp.name)
        certificate,key=folder/'cert.pem',folder/'key.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=isolated-test','-addext','subjectAltName=IP:127.0.0.1,IP:127.0.0.2',
                        '-out',str(certificate),'-keyout',str(key)],capture_output=True,check=True)
        config=replace(self.config,public_api_host='127.0.0.2',benchmarks_port=0,
                       tls_cert=str(certificate),tls_key=str(key),tls_ca=str(certificate))
        registration=self.start(public_registration(config,self.registration))
        updates=self.start(public_updates(config,self.updates))
        private_benchmarks=self.start(benchmarks(config))
        benchmark=self.start(public_benchmarks(config,private_benchmarks))
        context=ssl.create_default_context(cafile=str(certificate))
        def request(server,path,method='GET',body=None,token=None,hostname='127.0.0.2'):
            client=HTTPSConnection(hostname,server.server_port,context=context,timeout=3,
                                   source_address=('127.0.0.3',0))
            headers={'Content-Type':'application/json','X-Real-IP':'198.51.100.12'}
            if token:headers['Authorization']='Bearer '+token
            try:
                client.request(method,path,body=None if body is None else json.dumps(body),headers=headers)
                response=client.getresponse()
                return response.status,json.loads(response.read())
            finally:
                client.close()
        from test_benchmarks import CLAIM_ID, claim_document
        claim_route='/api/v1/hosts/'+CLAIM_ID+'/claim'
        self.assertEqual(request(updates,'/catalog.json')[0],401)
        self.assertEqual(request(benchmark,claim_route,'POST',claim_document(),config.benchmark_token)[0],401)
        with socket.create_connection(registration.server_address,source_address=('127.0.0.3',0),timeout=3) as raw:
            with context.wrap_socket(raw,server_hostname='127.0.0.2') as client:
                client.sendall(config.registration_secret.encode())
                self.assertEqual(client.recv(2),b'OK')
        self.assertEqual(request(updates,'/catalog.json')[0],200)
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
