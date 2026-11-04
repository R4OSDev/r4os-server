"""Application metadata, private sources and LAN-only editing contracts."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.client import HTTPConnection
import json
import hashlib
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from r4os_server.workspace import apps, catalog, connections, layout, worker, app_releases, app_storage, repo_metadata, packages
from r4os_server.workspace.github_api import GitHubFailure
from r4os_server.workspace.process import Context
from r4os_server.workspace.store import Store, Conflict
from test_benchmarks import configuration
from WebApp.server import create_server


MANIFEST = 'R4OS_MODULE_MANIFEST=2\nKIND=R4X\nNAME=MINESWEEPER_665F9DD0\nVERSION=0.1.0\nLANGUAGE=Rust\n'


class AppGitHub:
    """Published release fixture; writes or source requests fail the test."""
    target = 'MINESWEEPER_665F9DD0'

    def __init__(self, repository_id=1, target=None, format='R4X'):
        self.repository_id = repository_id
        self.target=target or self.target;self.format=format
        self.source_documents=None
        self.credentials, self.reads, self.downloads = [], [], []
        self.check = lambda: None
        self.during_download = lambda: None
        self.set_release('0.1.0', 11)

    def factory(self, repo, token, check=lambda: None):
        self.credentials.append((repo['id'], token));self.check = check
        return self

    def set_release(self, version, number):
        self.release = {'id': number, 'tag_name': 'v'+version, 'draft': False, 'prerelease': False,
                        'published_at': '2026-11-04T10:00:00Z'}
        self.commit = hashlib.sha1(version.encode()).hexdigest()
        binary = b'downloaded-from-github-'+version.encode()
        artifact='r4os.elf' if self.format=='ELF' else self.target+'.'+self.format
        checksum = (hashlib.sha256(binary).hexdigest()+'  '+artifact+'\n').encode()
        self.data, self.items = {}, []
        files = []
        for name, raw in ((artifact, binary), (self.target+'.sha256', checksum)):
            self.add(name, raw, number)
            files.append({'name': name, 'size': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()})
        receipt = {'schema': 2, 'repository_id': self.repository_id, 'target': self.target,
                   'version': version, 'commit': self.commit, 'technical': False,
                   'sdk_profile': {'sdk_id': 'fixture-sdk', 'profile_sources': {'Build': 'a'*64}}, 'assets': files}
        self.add('r4os-build-'+self.target+'.json', json.dumps(receipt).encode(), number)

    def add(self, name, raw, number):
        key = number*1000000+int(hashlib.sha256(self.target.encode()).hexdigest()[:4],16)*10+len(self.items);self.data[key] = raw
        self.items.append({'id': key, 'name': name, 'size': len(raw), 'state': 'uploaded',
                           'digest': 'sha256:'+hashlib.sha256(raw).hexdigest(), 'updated_at': '2026-11-04T10:00:00Z'})

    def request(self, method, path, *args, **kwargs):
        self.check();assert method == 'GET', 'App downloads must never mutate GitHub'
        if path == '':return {'id': self.repository_id}
        if path == '/releases/latest':return self.release
        if path.startswith('/releases?'):return [self.release] if self.release else []
        if path.startswith('/git/trees/'):
            docs=self.source_documents
            if docs is None:
                docs={'Cargo.toml':'[package]\nname="kernel"\nversion="'+self.release['tag_name'][1:]+'"\n'} if self.format=='ELF' else {'module.R4MF':MANIFEST.replace('MINESWEEPER_665F9DD0',self.target).replace('KIND=R4X','KIND='+self.format).replace('VERSION=0.1.0','VERSION='+self.release['tag_name'][1:])+'IMAGE_SCOPE=none\n'+('TARGET=/R4OS/LIBS/'+self.target+'.R4L\nEXPORT=Query:fixture_query:1\n' if self.format=='R4L' else 'IMPORT=R4IMG:Query:1\n')}
            self.documents=list(docs.items())
            return {'truncated':False,'tree':[{'path':name,'type':'blob','mode':'100644','sha':'fixture-'+str(index)} for index,(name,_) in enumerate(self.documents)]}
        if path.startswith('/git/blobs/fixture-'):
            text=self.documents[int(path.rsplit('-',1)[1])][1]
            return {'encoding':'base64','size':len(text),'content':base64.b64encode(text.encode()).decode()}
        raise AssertionError('Unexpected app request '+path)

    def assets(self, release):self.check();return self.items
    def tag_commit(self, tag):self.check();return self.commit
    def asset_bytes(self, item, limit=131072):
        self.check();self.reads.append(item['name']);return self.data[item['id']]
    def asset_digest(self, item):return item['digest'][7:] if item.get('digest') else hashlib.sha256(self.data[item['id']]).hexdigest()
    def download(self, item, destination, expected, progress=None):
        self.check();self.downloads.append(item['name']);raw = self.data[item['id']]
        destination.write_bytes(raw)
        if hashlib.sha256(raw).hexdigest() != expected:raise GitHubFailure(502, 'Checksum mismatch')
        if progress:progress(len(raw))
        self.during_download();self.check()


class AppTests(unittest.TestCase):
    def remote_app(self):
        apps.publish(self.store, 'minesweeper', {'published': True})
        fake = AppGitHub()
        patcher = patch.object(app_releases, 'Client', side_effect=fake.factory)
        patcher.start();self.addCleanup(patcher.stop)
        self.refresh_release()
        return fake

    def refresh_release(self):
        job = app_releases.request(self.store, 'minesweeper', 'refresh')
        self.store.change_job(job['id'], status='running')
        context = Context(self.store, self.store.job(job['id']))
        try:
            selected = app_releases.refresh(context, self.store.repository('minesweeper'))
            self.store.change_job(job['id'], status='completed')
            return selected
        except Exception:
            self.store.change_job(job['id'], status='failed')
            raise

    def download_release(self, action='download'):
        job = app_releases.request(self.store, 'minesweeper', action)
        self.assertTrue(worker.once(self.store))
        return self.store.job(job['id'])

    def staged_release(self, name='minesweeper', release_id=11, content=b'github-release'):
        import hashlib
        from r4os_server.workspace import app_storage
        from r4os_server.workspace.process import Context
        apps.publish(self.store, name, {'published': True})
        repo = self.store.repository(name)
        job = self.store.enqueue('app_download', name)
        self.store.change_job(job['id'], status='running')
        context = Context(self.store, self.store.job(job['id']))
        staged = app_storage.path(self.store, 'Apps/.staging/'+job['id'])
        staged.mkdir(parents=True)
        (staged/'GAME.R4X').write_bytes(content)
        selection = {'repository_id': repo['id'], 'release_id': release_id, 'tag': 'v0.1.0',
                     'version': '0.1.0', 'target': AppGitHub.target, 'commit': 'a'*40,
                     'files': [{'id': 31, 'name': 'GAME.R4X', 'size': len(content),
                                'sha256': hashlib.sha256(content).hexdigest()}]}
        return context, repo, selection, staged

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.store = Store(self.root/'workspace')
        connections.prepare_credentials(self.store.state, os.getuid(), os.getgid())
        self.seed('minesweeper', 1)

    def seed(self, name, number, connection='main', *, local=True, private=False):
        conn = connections.get(self.store, connection)
        meta = {'id': number, 'name': name, 'display_name': 'r4os-games-minesweeper',
                'owner': conn['owner'], 'default_branch': 'main', 'description': 'Do not import this description',
                'private': private, 'read_access': True, 'access_revision': conn['revision']}
        directory = conn['directory']+'/'+name
        with self.store.connect() as db:
            db.execute('INSERT INTO repositories(name,github_id,metadata,connection_id,enabled,directory) VALUES(?,?,?,?,1,?)',
                       (name, number, json.dumps(meta), connection, directory))
        if local:
            layout.ensure_folder(self.store, conn)
            folder = layout.path(self.store, directory)
            folder.mkdir()
            (folder/'module.R4MF').write_text(MANIFEST)
        return self.store.repository(name)

    def settings(self, **values):
        return dict({'display_name': None, 'install_path': None, 'description': ''}, **values)

    def test_migration_preserves_repository_data_and_defaults_to_unpublished(self):
        with self.store.connect() as db:
            db.execute('''CREATE TABLE apps(repository_name TEXT PRIMARY KEY REFERENCES repositories(name) ON DELETE CASCADE,
                       published INTEGER NOT NULL DEFAULT 0,manifest_name TEXT NOT NULL,manifest_version TEXT NOT NULL,
                       display_name_override TEXT,install_path_override TEXT,description TEXT NOT NULL DEFAULT '',
                       source_error TEXT NOT NULL DEFAULT '',created TEXT NOT NULL,updated TEXT NOT NULL)''')
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name<>'apps'")]
            before = {name: [tuple(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in tables}
        migrated = Store(self.store.root)
        with migrated.connect() as db:
            after = {name: [tuple(row) for row in db.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in tables}
            self.assertEqual(before, after)
        self.assertFalse(migrated.repository('minesweeper')['published'])
        self.assertEqual(apps.overview(migrated), [])

    def test_publish_defaults_concurrency_and_manual_settings_survive_refresh(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            self.assertTrue(all(result['published'] for result in pool.map(lambda _: apps.publish(self.store, 'minesweeper', {'published': True}), range(4))))
        original = apps.get(self.store, 'minesweeper')
        self.assertEqual(original['description'], '')
        self.assertEqual(original['display_name'], 'MINESWEEPER_665F9DD0')
        self.assertEqual(original['install_path'], 'C:\\Software\\MINESWEEPER_665F9DD0')
        edited = apps.save(self.store, 'minesweeper', self.settings(display_name='Minesweeper', install_path='D:/Games/Mines', description='A game.\nSecond line.'))
        self.assertEqual(edited['install_path'], 'D:\\Games\\Mines')
        source = layout.repository_path(self.store, self.store.repository('minesweeper'))/'module.R4MF'
        source.write_text(MANIFEST.replace('0.1.0', '0.2.0'))
        apps.refresh(self.store, self.store.repository('minesweeper'))
        apps.publish(self.store, 'minesweeper', {'published': False})
        self.assertEqual(apps.overview(self.store), [])
        apps.publish(self.store, 'minesweeper', {'published': True})
        restored = apps.get(Store(self.store.root), 'minesweeper')
        self.assertEqual((restored['display_name'], restored['install_path'], restored['description']),
                         ('Minesweeper', 'D:\\Games\\Mines', 'A game.\nSecond line.'))
        self.assertEqual(restored['default_display_name'], AppGitHub.target)
        reset = apps.save(self.store, 'minesweeper', self.settings())
        self.assertEqual((reset['display_name'], reset['install_path'], reset['description']), (AppGitHub.target, 'C:\\Software\\'+AppGitHub.target, ''))

    def test_private_manifest_uses_only_its_connection_and_rejects_stale_connection(self):
        conn = connections.save(self.store, {'url': 'https://github.com/R4mboX', 'token': 'fake-app-source-token'})
        self.seed('private-game', 2, conn['id'], local=False, private=True)
        document = {'size': len(MANIFEST), 'encoding': 'base64', 'content': base64.b64encode(MANIFEST.encode()).decode()}
        with patch.object(catalog, 'public_json', return_value=(document, {})) as request:
            result = apps.publish(self.store, 'private-game', {'published': True})
        self.assertTrue(result['published'])
        self.assertIn('/repos/R4mboX/r4os-games-minesweeper/contents/module.R4MF', request.call_args.args[0])
        self.assertEqual(request.call_args.args[1], 'fake-app-source-token')
        self.assertTrue(apps.get(self.store, 'private-game')['private'])
        apps.publish(self.store, 'private-game', {'published': False})
        def changed(*args):
            with self.store.connect() as db:
                db.execute('UPDATE connections SET revision=revision+1 WHERE id=?', (conn['id'],))
            return document, {}
        with patch.object(catalog, 'public_json', side_effect=changed), self.assertRaises(Conflict):
            apps.publish(self.store, 'private-game', {'published': True})
        self.assertFalse(apps.get(self.store, 'private-game')['published'])

    def test_invalid_values_do_not_change_settings_or_publish(self):
        apps.publish(self.store, 'minesweeper', {'published': True})
        before = apps.get(self.store, 'minesweeper')
        invalid = [self.settings(display_name=['bad']), self.settings(display_name='Bad\nName'),
                   self.settings(description=None), self.settings(description='x'*8193)]
        invalid += [self.settings(install_path=value) for value in ('../bad', '/Software/app', 'C:\\', 'C:\\Games\\..\\Other', 'C:\\Games\\CON', '\\\\host\\app', 'C:\\App?')]
        for value in invalid:
            with self.subTest(value=str(value)[:80]), self.assertRaises(ValueError):
                apps.save(self.store, 'minesweeper', value)
        self.assertEqual(apps.get(self.store, 'minesweeper'), before)
        for value in ({'published': 'true'}, {'published': 1}, {'published': True, 'extra': 1}):
            with self.assertRaises(ValueError): apps.publish(self.store, 'minesweeper', value)
        for text in (MANIFEST.replace('KIND=R4X', 'KIND=R4D'), MANIFEST+'NAME=OTHER\n', MANIFEST.replace('MINESWEEPER_665F9DD0', '../other')):
            with self.assertRaises(ValueError): apps.manifest(text)

    def test_remote_publication_runs_in_worker_and_unpublish_cancels_inflight_work(self):
        conn = connections.save(self.store, {'url': 'https://github.com/R4mboX', 'token': 'fake-app-source-token'})
        self.seed('queued-game', 2, conn['id'], local=False, private=True)
        document = {'size': len(MANIFEST), 'encoding': 'base64', 'content': base64.b64encode(MANIFEST.encode()).decode()}
        with patch.object(catalog, 'public_json', return_value=(document, {})) as fetch:
            status, result = apps.request_publish(self.store, 'queued-game', {'published': True})
            self.assertEqual(status, 202)
            self.assertTrue(result['publishing'])
            self.assertFalse(result['published'])
            fetch.assert_not_called()
            self.assertTrue(worker.once(self.store))
        self.assertTrue(apps.get(self.store, 'queued-game')['published'])
        apps.request_publish(self.store, 'queued-game', {'published': False})
        apps.request_publish(self.store, 'queued-game', {'published': True})
        def cancel_during_fetch(*args):
            apps.request_publish(self.store, 'queued-game', {'published': False})
            return document, {}
        with patch.object(catalog, 'public_json', side_effect=cancel_during_fetch):
            self.assertTrue(worker.once(self.store))
        self.assertFalse(apps.get(self.store, 'queued-game')['published'])
        self.assertFalse(self.store.repository('queued-game')['publishing'])
        apps.request_publish(self.store, 'queued-game', {'published': True})
        def cancel_and_republish(*args):
            apps.request_publish(self.store, 'queued-game', {'published': False})
            status, queued = apps.request_publish(self.store, 'queued-game', {'published': True})
            self.assertEqual(status, 202)
            self.assertTrue(queued['publishing'])
            return document, {}
        with patch.object(catalog, 'public_json', side_effect=cancel_and_republish):
            self.assertTrue(worker.once(self.store))
        self.assertFalse(apps.get(self.store, 'queued-game')['published'])
        with patch.object(catalog, 'public_json', return_value=(document, {})):
            self.assertTrue(worker.once(self.store))
        self.assertTrue(apps.get(self.store, 'queued-game')['published'])

    def test_source_failure_keeps_manual_values_and_recovers(self):
        apps.publish(self.store, 'minesweeper', {'published': True})
        apps.save(self.store, 'minesweeper', self.settings(display_name='Friendly', description='Keep this.'))
        source = layout.repository_path(self.store, self.store.repository('minesweeper'))/'module.R4MF'
        source.unlink()
        apps.refresh(self.store, self.store.repository('minesweeper'))
        failed = apps.get(self.store, 'minesweeper')
        self.assertTrue(failed['published'])
        self.assertFalse(failed['source_available'])
        self.assertEqual((failed['display_name'], failed['description']), ('Friendly', 'Keep this.'))
        source.write_text(MANIFEST)
        apps.refresh(self.store, self.store.repository('minesweeper'))
        self.assertTrue(apps.get(self.store, 'minesweeper')['source_available'])

    def test_connection_removal_deletes_its_apps_and_keeps_other_accounts(self):
        conn = connections.save(self.store, {'url': 'https://github.com/Other'})
        self.seed('other-game', 2, conn['id'])
        for name in ('minesweeper', 'other-game'): apps.publish(self.store, name, {'published': True})
        context = SimpleNamespace(store=self.store, job={'id': 'delete', 'subject': conn['id']}, phase=lambda *_: None)
        connections.remove(context)
        self.assertIsNone(apps.existing(self.store, 'other-game'))
        self.assertTrue(apps.get(self.store, 'minesweeper')['published'])
        with self.store.connect() as db: self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_webapp_api_enforces_host_origin_and_saves_all_three_fields(self):
        config = replace(configuration(self.root/'data'), workspace_dir=str(self.store.root), project_root=str(self.root))
        server = create_server(config, port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        thread.start()
        def request(path, method='GET', body=None, headers=None):
            connection = HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            connection.request(method, path, body=None if body is None else json.dumps(body), headers={'Content-Type': 'application/json', **(headers or {})})
            response = connection.getresponse();raw = response.read();connection.close()
            return response.status, json.loads(raw) if raw else None
        try:
            self.assertEqual(request('/api/apps/overview')[1], {'apps': [], 'publishing': 0, 'active_jobs': [], 'categories': list(repo_metadata.CATEGORIES)})
            route = '/api/repositories/items/minesweeper/publication'
            self.assertEqual(request(route, 'PUT', {'published': True})[0], 403)
            self.assertEqual(request(route, 'PUT', {'published': True}, {'Origin': 'https://other.invalid'})[0], 403)
            self.assertEqual(request('/api/apps/overview', headers={'Host': 'other.invalid'})[0], 403)
            headers = {'X-R4OS-Request': '1'}
            self.assertTrue(request(route, 'PUT', {'published': True}, headers)[1]['published'])
            value = self.settings(display_name='Minesweeper', description='<b>Literal text</b>')
            status, saved = request('/api/apps/items/minesweeper', 'PUT', value, headers)
            self.assertEqual(status, 200)
            self.assertEqual(saved['display_name'], 'Minesweeper')
            self.assertEqual(saved['install_path'], 'C:\\Software\\MINESWEEPER_665F9DD0')
            self.assertEqual(request('/api/apps/overview')[1]['apps'][0]['description'], '<b>Literal text</b>')
            self.assertEqual(request('/api/apps/items/minesweeper', 'PUT', self.settings(install_path='../bad'), headers)[0], 422)
            status, saved = request('/api/apps/items/minesweeper', 'PUT', dict(value,category='Game'), headers)
            self.assertEqual(status, 200);self.assertEqual(saved['category'], 'Game')
            self.assertEqual(self.store.repository('minesweeper')['server_metadata']['category'], 'Game')
            status, job = request('/api/repositories/actions/refresh', 'POST', {'name': 'minesweeper'}, headers)
            self.assertEqual(status, 202)
            overview = request('/api/apps/overview')[1]
            self.assertEqual(overview['active_jobs'][0]['id'], job['id'])
            self.assertEqual(overview['apps'][0]['refresh_job']['status'], 'queued')
            self.assertTrue(overview['apps'][0]['repository_available'])
            self.assertEqual(overview['apps'][0]['connection_id'], 'main')
            self.store.change_job(job['id'], status='failed', error='Fixture refresh failed')
            failed = request('/api/apps/overview')[1]
            self.assertEqual(failed['active_jobs'], [])
            self.assertEqual(failed['apps'][0]['refresh_job']['error'], 'Fixture refresh failed')
            self.assertEqual(failed['apps'][0]['display_name'], 'Minesweeper')
            self.assertEqual(failed['apps'][0]['description'], '<b>Literal text</b>')
            self.assertEqual(request('/api/apps/actions/refresh', 'POST', {'name':'minesweeper'})[0], 403)
            self.assertEqual(request('/api/apps/actions/download_all', 'POST', {'name':'minesweeper'}, headers)[0], 422)
            self.assertEqual(request('/api/apps/actions/download', 'POST', {'name':'minesweeper'}, headers)[0], 409)
            status, app_job = request('/api/apps/actions/refresh', 'POST', {'name':'minesweeper'}, headers)
            self.assertEqual(status, 202);self.assertEqual(app_job['kind'], 'app_refresh')
            overview = request('/api/apps/overview')[1]
            self.assertEqual(overview['apps'][0]['release']['job']['id'], app_job['id'])
            self.assertIsNone(overview['apps'][0]['version'])
            self.assertEqual(overview['apps'][0]['source_version'], '0.1.0')
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)

    def test_app_storage_is_stable_verified_and_recovers_a_rename_before_commit(self):
        from r4os_server.workspace import app_storage
        context, repo, selection, staged = self.staged_release()
        destination = app_storage.path(self.store, app_storage.directory(repo['id'], selection['release_id']))
        destination.parent.mkdir(parents=True)
        import shutil
        shutil.copytree(staged, destination)  # Crash after filesystem rename, before the SQLite commit.
        result = app_storage.install(context, repo, connections.get(self.store, 'main')['revision'], selection, staged)
        self.assertTrue(result['activation_committed'])
        self.assertTrue(app_storage.current(self.store, repo['name'], hashes=True)['valid'])
        self.store.change_job(context.job['id'], status='completed')
        apps.save(self.store, repo['name'], self.settings(display_name='Renamed app'))
        self.assertEqual(app_storage.current(Store(self.store.root), repo['name'])['directory'], 'Apps/1/Releases/11')
        (destination/'GAME.R4X').write_bytes(b'corrupted-file')
        self.assertFalse(app_storage.current(self.store, repo['name'], hashes=True)['valid'])
        with self.assertRaises(ValueError): app_storage.path(self.store, 'Apps/../Artifacts')
        (self.store.root/'Apps/link').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError): app_storage.path(self.store, 'Apps/link/other')

    def test_app_storage_keeps_previous_release_on_bad_download_and_recovers_staging(self):
        from r4os_server.workspace import app_storage
        context, repo, selection, staged = self.staged_release()
        revision = connections.get(self.store, 'main')['revision']
        app_storage.install(context, repo, revision, selection, staged)
        self.store.change_job(context.job['id'], status='completed')
        bad, repo, new, partial = self.staged_release(release_id=12)
        (partial/'GAME.R4X').write_bytes(b'broken')
        with self.assertRaises(ValueError): app_storage.install(bad, repo, revision, new, partial)
        self.assertEqual(app_storage.current(self.store, repo['name'])['release_id'], 11)
        self.store.recover();app_storage.recover(self.store)
        self.assertFalse(partial.exists())
        self.assertEqual(app_storage.current(self.store, repo['name'])['release_id'], 11)

    def test_shared_app_release_removal_preserves_other_connection_and_manual_fields(self):
        from r4os_server.workspace import app_storage
        context, repo, selection, staged = self.staged_release()
        app_storage.install(context, repo, connections.get(self.store, 'main')['revision'], selection, staged)
        self.store.change_job(context.job['id'], status='completed')
        conn = connections.save(self.store, {'url': 'https://github.com/Other'})
        self.seed('other-copy', 1, conn['id'])
        apps.publish(self.store, 'other-copy', {'published': True})
        with self.store.connect() as db:
            db.execute('INSERT INTO app_release_refs VALUES(?,?,?,1)', ('other-copy', 1, 11))
        apps.save(self.store, 'minesweeper', self.settings(description='Keep description'))
        app_storage.remove(self.store, {'minesweeper'})
        self.assertIsNone(app_storage.current(self.store, 'minesweeper'))
        self.assertTrue(app_storage.current(self.store, 'other-copy')['valid'])
        self.assertEqual(apps.get(self.store, 'minesweeper')['description'], 'Keep description')
        removal = SimpleNamespace(store=self.store, job={'id': 'delete', 'subject': conn['id']}, phase=lambda *_: None)
        connections.remove(removal)
        self.assertFalse(app_storage.path(self.store, 'Apps/1/Releases/11').exists())

    def test_github_app_download_uses_published_release_without_checkout_or_build_files(self):
        fake = self.remote_app()
        repo_metadata.save(self.store, 'minesweeper', {'category': 'Game'})
        self.assertTrue(all(name.endswith('.json') for name in fake.reads))
        self.assertEqual(fake.downloads, [])
        apps.save(self.store, 'minesweeper', self.settings(display_name='Friendly', description='Preserve this'))
        source = layout.repository_path(self.store, self.store.repository('minesweeper'))
        import shutil
        shutil.rmtree(source)
        job = self.download_release()
        self.assertEqual(job['status'], 'completed', job['error'])
        self.assertEqual(len(fake.downloads), 3)
        value = apps.get(self.store, 'minesweeper')
        self.assertEqual((value['version'], value['release']['state'], value['display_name'], value['description']),
                         ('0.1.0', 'current', 'Friendly', 'Preserve this'))
        self.assertTrue(app_storage.current(self.store, 'minesweeper', hashes=True)['valid'])
        self.assertFalse((self.store.root/'Artifacts').exists())
        self.assertTrue(all(token == '' for _, token in fake.credentials))
        self.assertFalse(value['release']['can_download']);self.assertFalse(value['release']['can_update'])
        self.assertEqual(value['category'], 'Game')

    def test_app_update_keeps_previous_release_and_only_updates_newer_available_versions(self):
        fake = self.remote_app();self.assertEqual(self.download_release()['status'], 'completed')
        fake.set_release('0.2.0', 12);self.refresh_release()
        self.assertTrue(apps.get(self.store, 'minesweeper')['release']['can_update'])
        job = self.download_release('update');self.assertEqual(job['status'], 'completed', job['error'])
        state = apps.get(self.store, 'minesweeper')['release']
        self.assertEqual((state['local']['release_id'], state['local_release_count']), (12, 2))
        self.assertTrue(app_storage.path(self.store, 'Apps/1/Releases/11').exists())
        fake.set_release('0.1.0', 11);self.refresh_release()
        state = apps.get(self.store, 'minesweeper')['release']
        self.assertEqual(state['state'], 'local_newer');self.assertFalse(state['can_update'])
        with self.assertRaises(Conflict):app_releases.request(self.store, 'minesweeper', 'update')

    def test_corrupt_or_changing_github_download_preserves_current_app(self):
        fake = self.remote_app();self.download_release()
        fake.set_release('0.2.0', 12);self.refresh_release()
        binary = next(item for item in fake.items if item['name'].endswith('.R4X'))
        fake.data[binary['id']] = b'bad-checksum'
        failed = self.download_release('update')
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('Checksum mismatch', failed['error'])
        self.assertEqual(app_storage.current(self.store, 'minesweeper')['release_id'], 11)
        self.assertFalse(app_storage.path(self.store, 'Apps/1/Releases/12').exists())
        fake.set_release('0.2.0', 12);self.refresh_release()
        def changed():
            if len(fake.downloads) == 7:
                fake.during_download = lambda: None
                fake.set_release('0.3.0', 13)
        fake.during_download = changed
        failed = self.download_release('update')
        self.assertEqual(failed['status'], 'failed')
        self.assertIn('changed during download', failed['error'])
        self.assertEqual(app_storage.current(self.store, 'minesweeper')['release_id'], 11)

    def test_app_refresh_rejects_missing_receipts_drafts_and_ambiguous_assets_honestly(self):
        fake = self.remote_app()
        for change in ('receipt', 'draft', 'duplicate', 'tag'):
            fake.set_release('0.1.0', 11)
            if change == 'receipt':fake.items = [a for a in fake.items if not a['name'].endswith('.json')]
            elif change == 'draft':fake.release['draft'] = True
            elif change == 'duplicate':fake.items.append(dict(fake.items[0], id=9999))
            else:fake.release['tag_name'] = 'v9.9.9'
            with self.subTest(change=change), self.assertRaises((ValueError, Conflict)):
                self.refresh_release()
            state = apps.get(self.store, 'minesweeper')['release']
            self.assertEqual(state['remote_status'], 'unknown');self.assertFalse(state['can_download'])
        fake.release = None;self.refresh_release()
        self.assertEqual(apps.get(self.store, 'minesweeper')['release']['remote_status'], 'missing')

    def test_app_jobs_deduplicate_bind_revision_and_remove_without_token(self):
        fake = self.remote_app()
        with ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda _: app_releases.request(self.store, 'minesweeper', 'download'), range(4)))
        self.assertEqual(len({job['id'] for job in jobs}), 1)
        self.store.cancel_job(jobs[0]['id'])
        self.assertIsNone(app_storage.current(self.store, 'minesweeper'))
        self.assertEqual(self.download_release()['status'], 'completed')
        fake.set_release('0.2.0', 12);self.refresh_release()
        update = app_releases.request(self.store, 'minesweeper', 'update')
        with self.store.connect() as db:db.execute("UPDATE connections SET revision=revision+1 WHERE id='main'")
        worker.once(self.store)
        self.assertEqual(self.store.job(update['id'])['status'], 'failed')
        self.assertEqual(app_storage.current(self.store, 'minesweeper')['release_id'], 11)
        removal = app_releases.request(self.store, 'minesweeper', 'remove_local');worker.once(self.store)
        self.assertEqual(self.store.job(removal['id'])['status'], 'completed')
        self.assertIsNone(app_storage.current(self.store, 'minesweeper'))
        self.assertTrue(apps.get(self.store, 'minesweeper')['published'])

    def test_private_app_release_uses_its_connection_token_and_cancels_during_download(self):
        conn = connections.save(self.store, {'url': 'https://github.com/R4mboX', 'token': 'fake-app-read-token'})
        self.seed('private-game', 2, conn['id'], private=True)
        apps.publish(self.store, 'private-game', {'published': True})
        fake = AppGitHub(2)
        with patch.object(app_releases, 'Client', side_effect=fake.factory):
            refresh = app_releases.request(self.store, 'private-game', 'refresh')
            self.store.change_job(refresh['id'], status='running')
            app_releases.refresh(Context(self.store, self.store.job(refresh['id'])), self.store.repository('private-game'))
            self.store.change_job(refresh['id'], status='completed')
            job = app_releases.request(self.store, 'private-game', 'download')
            fake.during_download = lambda: self.store.cancel_job(job['id'])
            worker.once(self.store)
        self.assertEqual(self.store.job(job['id'])['status'], 'cancelled')
        self.assertTrue(all(token == 'fake-app-read-token' for _, token in fake.credentials))
        self.assertIsNone(app_storage.current(self.store, 'private-game'))
        self.assertFalse(app_storage.path(self.store, 'Apps/.staging/'+job['id']).exists())

    def test_app_and_repository_share_category_and_invalid_combined_edits_are_atomic(self):
        apps.publish(self.store, 'minesweeper', {'published': True})
        self.assertEqual(apps.get(self.store, 'minesweeper')['category'], 'Undefined')
        repo_metadata.save(self.store, 'minesweeper', {'category':'Game'})
        self.assertEqual(apps.get(self.store, 'minesweeper')['category'], 'Game')
        saved = apps.save(self.store, 'minesweeper', dict(self.settings(display_name='Friendly',description='Keep'),category='App'))
        self.assertEqual(saved['category'], 'App')
        self.assertEqual(self.store.repository('minesweeper')['server_metadata']['category'], 'App')
        for value in (dict(self.settings(display_name='Changed'),category='Unknown category'),
                      dict(self.settings(install_path='../bad'),category='Service')):
            with self.assertRaises(ValueError):apps.save(self.store, 'minesweeper', value)
            self.assertEqual(apps.get(self.store, 'minesweeper'), saved)
            self.assertEqual(self.store.repository('minesweeper')['server_metadata']['category'], 'App')
        apps.refresh(self.store, self.store.repository('minesweeper'))
        self.assertEqual(apps.get(self.store, 'minesweeper')['category'], 'App')

    def test_library_and_kernel_use_release_types_and_explicit_runtime_roles(self):
        for number,kind,target,slug in [(2,'R4L','R4IMG','r4os-libraries'),(3,'ELF','kernel','r4os-kernel')]:
            name='typed-'+str(number);repo=self.seed(name,number)
            root=layout.repository_path(self.store,repo);(root/'module.R4MF').unlink()
            with self.store.connect() as db:
                data=json.loads(db.execute('SELECT metadata FROM repositories WHERE name=?',(name,)).fetchone()[0]);data['display_name']=slug
                db.execute('UPDATE repositories SET metadata=? WHERE name=?',(json.dumps(data),name))
            if kind=='R4L':
                folder=root/target;folder.mkdir();(folder/'module.R4MF').write_text(MANIFEST.replace(AppGitHub.target,target).replace('KIND=R4X','KIND=R4L')+'TARGET=/R4OS/LIBS/'+target+'.R4L\nIMAGE_SCOPE=none\n')
            else:(root/'Cargo.toml').write_text('[package]\nname="kernel"\nversion="0.1.0"\n')
            apps.publish(self.store,name,{'published':True})
            with self.store.connect() as db:item=db.execute('SELECT repository_name FROM software_packages WHERE source_repository=?',(name,)).fetchone()[0]
            fake=AppGitHub(number,target,kind)
            with patch.object(app_releases,'Client',side_effect=fake.factory):
                job=app_releases.request(self.store,item,'refresh');self.store.change_job(job['id'],status='running')
                app_releases.refresh(Context(self.store,self.store.job(job['id'])),self.store.repository(name));self.store.change_job(job['id'],status='completed')
                job=app_releases.request(self.store,item,'download');worker.once(self.store)
            self.assertEqual(self.store.job(job['id'])['status'],'completed',self.store.job(job['id'])['error'])
            value=apps.get(self.store,item);self.assertEqual(value['package_type'],'library' if kind=='R4L' else 'kernel')
            self.assertEqual(value['format'],kind)
            self.assertEqual(value['default_install_path'],'/R4OS/LIBS/R4IMG.R4L' if kind=='R4L' else None)
            local=value['release']['local'];self.assertEqual(local['package']['artifact_name'],'R4IMG.R4L' if kind=='R4L' else 'r4os.elf')

    def test_multiple_library_targets_share_release_without_sharing_publication_or_removal(self):
        name='library-set';repo=self.seed(name,2);root=layout.repository_path(self.store,repo);(root/'module.R4MF').unlink();documents={}
        for target in ('LIBA','LIBB'):
            text=MANIFEST.replace(AppGitHub.target,target).replace('KIND=R4X','KIND=R4L')+'TARGET=/R4OS/LIBS/'+target+'.R4L\nIMAGE_SCOPE=none\n'
            folder=root/target;folder.mkdir();(folder/'module.R4MF').write_text(text);documents[target+'/module.R4MF']=text
        with self.assertRaises(Conflict):apps.publish(self.store,name,{'published':True})
        apps.publish(self.store,name,{'published':True,'targets':['LIBA']})
        self.assertEqual([item['target'] for item in apps.overview(self.store)],['LIBA'])
        apps.publish(self.store,name,{'published':True,'targets':['LIBA','LIBB']})
        fake=AppGitHub(2,'LIBA','R4L');second=AppGitHub(2,'LIBB','R4L')
        fake.data.update(second.data);fake.items+=second.items;fake.source_documents=documents
        items=apps.overview(self.store)
        self.assertEqual(len({item['package_id'] for item in items}),2)
        with patch.object(app_releases,'Client',side_effect=fake.factory):
            for item in items:
                job=app_releases.request(self.store,item['repository'],'refresh');self.store.change_job(job['id'],status='running')
                app_releases.refresh(Context(self.store,self.store.job(job['id'])),self.store.repository(name));self.store.change_job(job['id'],status='completed')
            jobs=[app_releases.request(self.store,item['repository'],'download') for item in items]
            for _ in jobs:worker.once(self.store)
        self.assertTrue(all(self.store.job(job['id'])['status']=='completed' for job in jobs))
        first,second=sorted(items,key=lambda item:item['target'])
        job=app_releases.request(self.store,first['repository'],'remove_local');worker.once(self.store)
        self.assertEqual(self.store.job(job['id'])['status'],'completed')
        self.assertIsNone(app_storage.current(self.store,first['repository']))
        self.assertTrue(app_storage.current(self.store,second['repository'],hashes=True)['valid'])
        folder=app_storage.path(self.store,'Apps/2/Releases/11')
        self.assertFalse((folder/'LIBA.R4L').exists());self.assertTrue((folder/'LIBB.R4L').exists())
        # A newer repository release for LIBA must not hide LIBB's last release.
        fake.check=lambda:None
        old_release=dict(fake.release);new_release=dict(old_release,id=12,tag_name='v0.2.0')
        original_request=fake.request
        def request(method,path,*args,**kwargs):
            if path=='/releases/latest':return new_release
            if path.startswith('/releases?'):return [new_release,old_release]
            return original_request(method,path,*args,**kwargs)
        def assets(release):return [a for a in fake.items if 'LIBA' in a['name']] if release['id']==12 else fake.items
        with patch.object(fake,'request',side_effect=request),patch.object(fake,'assets',side_effect=assets):
            selected=app_releases.selection(fake,self.store.repository(name),'LIBB',package=apps.existing(self.store,second['repository']))
        self.assertEqual((selected['target'],selected['release_id']),('LIBB',11))
