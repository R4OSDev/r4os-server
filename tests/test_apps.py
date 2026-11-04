"""Application metadata, private sources and LAN-only editing contracts."""
import base64
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from http.client import HTTPConnection
import json
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from r4os_server.workspace import apps, catalog, connections, layout, worker
from r4os_server.workspace.store import Store, Conflict
from test_benchmarks import configuration
from WebApp.server import create_server


MANIFEST = 'R4OS_MODULE_MANIFEST=2\nKIND=R4X\nNAME=MINESWEEPER_665F9DD0\nVERSION=0.1.0\nLANGUAGE=Rust\n'


class AppTests(unittest.TestCase):
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
                     'version': '0.1.0', 'target': 'GAME', 'commit': 'a'*40,
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
            db.execute('DROP TABLE apps')
            tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
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
        source.write_text(MANIFEST.replace('MINESWEEPER_665F9DD0', 'NEW_MODULE').replace('0.1.0', '0.2.0'))
        apps.refresh(self.store, self.store.repository('minesweeper'))
        apps.publish(self.store, 'minesweeper', {'published': False})
        self.assertEqual(apps.overview(self.store), [])
        apps.publish(self.store, 'minesweeper', {'published': True})
        restored = apps.get(Store(self.store.root), 'minesweeper')
        self.assertEqual((restored['display_name'], restored['install_path'], restored['description']),
                         ('Minesweeper', 'D:\\Games\\Mines', 'A game.\nSecond line.'))
        self.assertEqual(restored['default_display_name'], 'NEW_MODULE')
        reset = apps.save(self.store, 'minesweeper', self.settings())
        self.assertEqual((reset['display_name'], reset['install_path'], reset['description']), ('NEW_MODULE', 'C:\\Software\\NEW_MODULE', ''))

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
            self.assertEqual(request('/api/apps/overview')[1], {'apps': [], 'publishing': 0, 'active_jobs': []})
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
