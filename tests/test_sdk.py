"""Boundary checks for immutable SDK packages and removal of legacy contracts."""
import copy
import hashlib
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from r4os_server.workspace import sdk_packages, sdk_projects, toolsets
from r4os_server.workspace.process import Context
from r4os_server.workspace.store import Store
from sdk_fixture import install


class SDKTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.store=Store(Path(self.temp.name)/'workspace');self.proof=install(self.store)
        self.job=self.store.enqueue('sdk_verify','sdk');self.context=Context(self.store,self.job)

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
        artifact=self.store.root/'Artifacts/saved.bin';artifact.parent.mkdir(exist_ok=True);artifact.write_bytes(b'user artifact')
        with patch.object(toolsets,'validate',return_value={'ready':True}):result=sdk_cleanup.cleanup(self.context)
        self.assertIn('0.64.100',result['obsolete_sdk_versions']);self.assertFalse(legacy.exists());self.assertFalse(old.exists())
        self.assertTrue((recovery/'frozen').is_file());self.assertEqual(artifact.read_bytes(),b'user artifact')
        self.assertEqual(self.store.job(job['id'])['result'],{'historical':True})
        self.assertEqual(toolsets.active(self.store)[1]['sdk_id'],'sdk-fixture')

    def test_verified_repeat_reuses_immutable_package_offline(self):
        with patch.object(sdk_packages,'verify',return_value={'verified':True}) as verify,patch.object(sdk_packages,'fetch',side_effect=AssertionError('network used')):
            result=sdk_packages.import_release(self.context,{'release_url':'https://github.com/R4OSDev/r4os-sdk/releases/tag/v0.64.145'})
        self.assertTrue(result['unchanged']);verify.assert_called_once()
