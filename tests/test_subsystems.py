"""SNES imports/migration use isolated databases, never the running service."""

from copy import deepcopy
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from uuid import uuid4

from r4os_server.benchmarks.api import Application


from r4os_server.benchmarks.models import MAX_U64, normalize_host, normalize_run
from r4os_server.benchmarks.store import Conflict, Store
from r4os_server.benchmarks.subsystems import normalize, validate, compare, PHASES
from test_benchmarks import fixture as spectra_fixture
from test_benchmarks import configuration
from r4os_server.benchmarks.service import create_server as benchmark_server


def fixture(*, speed=1, mode="normal", region="ntsc", host="host-a"):
    hz = 21477272 if region == "ntsc" else 21281370
    start = hz // 4
    wall = 3000000000 // speed
    raw = {"schema_version": 1, "benchmark_family": "snes", "subsystem_id": "r4os.snes",
           "run_id": str(uuid4()), "host_id": host,
           "started_at_utc": "2026-10-21T00:10:00.1234567Z", "finished_at_utc": "2026-10-21T00:10:20Z",
           "status": "completed", "failure_reason": None, "profile": "core-throughput", "region": region,
           "instrumentation_mode": mode,
           "workload": {"scene_id": "r4snes.active-scene.v2", "work_version": "snes-fixed-guest-v2",
                        "rom_sha256": "a"*64, "initial_state_sha256": "c"*64, "input_contract": "no-events-v1",
                        "power_on": "instance-1-semantic-ipl-no-save-v1", "warmup_guest_ns": "250000000",
                        "measured_guest_ns": "3000000000", "sample_count": 5, "enhancement": "none"},
           "environment": {"execution": "host-owner", "conditions": {"contract": "test-v2", "pacing": "unthrottled"},
                           "backend": "no-window-pcm-drain", "test_counter": MAX_U64},
           "versions": {"release_version": "0.86.14", "emulator_version": "0.22.14", "kernel_version": None},
           "artifacts": [{"name": "test-only-host", "sha256": "b"*64, "role": "host-owner"}], "samples": [],
           "summary": {"wall_ns": {"median_value": str(wall), "min_value": str(wall), "max_value": str(wall), "mad_value": "0"},
                       "realtime_factor": {"median_value": str(speed), "min_value": str(speed), "max_value": str(speed), "mad_value": "0"}}}
    for index in range(1, 6):
        raw["samples"].append({"sample": index, "qualification_only": False, "cleanup_verified": True,
            "instrumented": mode == "detail", "scene": raw["workload"]["scene_id"], "region": region,
            "rom_sha256": "a"*64, "initial_state_sha256": "c"*64, "state_sha256": "d"*64,
            "frame_sha256": "e"*64, "pcm_sha256": "f"*64,
            "warmup": {"wall_ns": "250000000", "guest_ns": "250000000", "master_cycles": str(start)},
            "work_start_master": str(start), "work_end_master": str(start+hz*3), "master_hz": str(hz),
            "wall_ns": str(wall), "pending_master_cycles": "0", "profile_clock_regressions": "0",
            "maximum_operation_overshoot": "0", "generated_frames": "180", "apu_native_frames": "96000",
            "phase_time_is_cpu_accounting": False, "phase_order": PHASES,
            "sampled_exclusive_wall_ns": ["1"]*6 if mode == "detail" else None,
            "actually_presented_frames": None, "audio_playback_frames": None})
    return raw


class SubsystemTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="devcenter-snes-", dir=tempfile.gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/"test.sqlite3"
        self.store = Store(self.path)
        self.store.put_host(normalize_host("host-a", {"display_name": "Original"}))

    def put(self, raw):
        return self.store.put_subsystem_run(normalize(raw["run_id"], raw))

    def test_full_roundtrip_idempotency_conflict_and_host_snapshot(self):
        raw = fixture()
        for sample in raw["samples"]:
            sample["dropped_presents"] = str(MAX_U64)
        self.assertTrue(self.put(raw)[0])
        same = deepcopy(raw)
        same["samples"][0]["wall_ns"] = 3000000000
        same["environment"]["test_counter"] = str(MAX_U64)
        self.assertFalse(self.put(same)[0])
        changed = deepcopy(raw)
        changed["samples"][0]["wall_ns"] = "3000000001"
        with self.assertRaises(Conflict):
            self.put(changed)
        self.store.put_host(normalize_host("host-a", {"display_name": "Renamed"}))
        saved = self.store.subsystem_run(raw["run_id"])
        self.assertEqual(saved["host_snapshot"]["display_name"], "Original")
        self.assertEqual(saved["environment"]["test_counter"], str(MAX_U64))
        self.assertEqual(saved["started_at_utc"], "2026-10-21T00:10:00.123456700Z")
        self.assertEqual(saved["sample_total"], 5)
        self.assertEqual(saved["validation"]["status"], "valid")
        self.assertIsNone(saved["samples"][0]["actually_presented_frames"])
        self.assertNotIn("scores", saved)
        listed = self.store.subsystem_runs({})["items"][0]
        self.assertEqual(listed["observations"]["dropped_presents"]["median_value"], str(MAX_U64))
        self.assertIsNone(listed["observations"]["audio_failures"])
        self.assertNotIn("samples", listed)

    def test_failed_partial_and_invalid_completed_are_retained(self):
        raw = fixture()
        raw.update(status="failed", failure_reason="test-only timeout")
        raw["samples"] = raw["samples"][:2]
        self.put(raw)
        saved = self.store.subsystem_run(raw["run_id"])
        self.assertEqual(saved["sample_total"], 2)
        self.assertEqual(saved["validation"]["status"], "unavailable")
        raw = fixture()
        raw["samples"][0]["qualification_only"] = True
        self.assertEqual(self.put(raw)[1]["status"], "invalid")

    def test_work_mode_region_and_backend_comparison(self):
        current = normalize((raw := fixture(speed=2))["run_id"], raw)
        baseline = normalize((raw := fixture())["run_id"], raw)
        result = compare(current, baseline)
        self.assertTrue(result["compatible"])
        self.assertEqual(result["metrics"]["wall_ns"]["ratio"], "0.5")
        self.assertIsNone(result["score"])
        for other in (fixture(mode="detail"), fixture(region="pal")):
            self.assertFalse(compare(current, normalize(other["run_id"], other))["compatible"])
        other = deepcopy(baseline)
        other["host_id"] = "host-b"
        self.assertTrue(compare(current, other)["different_hosts"])
        self.assertTrue(compare(current, other)["compatible"])
        other["environment"]["conditions"]["pacing"] = "different"
        self.assertFalse(compare(current, other)["compatible"])

    def test_raw_pagination_and_filters(self):
        raw = fixture()
        self.put(raw)
        self.put(fixture(mode="detail"))
        result = self.store.subsystem_runs({"instrumentation_mode": "normal", "date_from": "2026-10-21", "date_to": "2026-10-21"})
        self.assertEqual(result["total"], 1)
        self.assertNotIn("samples", result["items"][0])
        page = self.store.subsystem_samples(raw["run_id"], {"offset": "2", "limit": "1"})
        self.assertEqual(page["total"], 5)
        self.assertEqual(page["items"][0]["sample"], "3")

    def test_schema1_migration_preserves_spectra_bytes_and_scores(self):
        legacy = spectra_fixture()
        self.store.put_run(normalize_run(legacy["run_id"], legacy))
        with self.store.connection() as connection:
            before = connection.execute("SELECT payload_json FROM benchmark_runs").fetchone()[0]
            connection.execute("DROP TABLE subsystem_runs")
            connection.execute("PRAGMA user_version=1")
        reopened = Store(self.path)
        with reopened.connection() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 4)
            self.assertEqual(connection.execute("SELECT payload_json FROM benchmark_runs").fetchone()[0], before)
        self.assertEqual(reopened.run(legacy["run_id"])["score_validation"]["status"], "valid")
        self.assertTrue(reopened.put_subsystem_run(normalize((raw := fixture())["run_id"], raw))[0])
        with reopened.connection() as connection:
            original=connection.execute('SELECT payload_json FROM subsystem_runs').fetchone()[0]
            connection.execute('ALTER TABLE subsystem_runs DROP COLUMN summary_json')
            connection.execute('PRAGMA user_version=2')
        migrated=Store(self.path)
        with migrated.connection() as connection:
            self.assertEqual(connection.execute('SELECT payload_json FROM subsystem_runs').fetchone()[0],original)
        self.assertEqual(migrated.subsystem_runs({})['items'][0]['validation']['status'],'valid')

    def test_http_contract_uses_shared_authentication(self):
        config=configuration(self.path.parent)
        server=benchmark_server(config,store=self.store)
        server.policy.store.register_magic('127.0.0.1')
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        raw=fixture();route='/api/v1/subsystem-benchmark-runs/'+raw['run_id']
        connection=HTTPConnection('127.0.0.1',server.server_port,timeout=5)
        self.addCleanup(connection.close)
        connection.request('PUT',route,json.dumps(raw),{'Content-Type':'application/json'})
        response=connection.getresponse();response.read();self.assertEqual(response.status,401)
        connection.request('PUT',route,json.dumps(raw),{'Content-Type':'application/json','Authorization':'Bearer '+config.benchmark_token})
        response=connection.getresponse();saved=json.loads(response.read())
        self.assertEqual(response.status,201)
        self.assertEqual(saved['validation']['status'],'valid')
        connection.request('GET',route)
        response=connection.getresponse();saved=json.loads(response.read())
        self.assertEqual(response.status,200)
        self.assertEqual(len(saved['samples']),5)
        connection.request('GET',route+'/comparison?reference_run_id='+str(uuid4()))
        response=connection.getresponse();response.read();self.assertEqual(response.status,404)

