"""Import, persistence and scoring contracts using isolated temporary databases."""

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from uuid import uuid4

from r4os_server.benchmarks.api import Application
from r4os_server.common.config import Config

from r4os_server.benchmarks.models import AREAS, InvalidData, MAX_U64, loads, normalize_host, normalize_run
from r4os_server.benchmarks.identity import IDENTITY_METHOD, normalize_claim
from r4os_server.benchmarks.store import Conflict, HostClaimConflict, Store

ROOT = Path(__file__).resolve().parent / "fixtures/benchmarks"
TOKEN = "test-only-token-not-a-real-credential"
CLAIM_ID = "hw1-" + "a" * 60


def claim_document(name="Automatic host", digest="b" * 64):
    return {"display_name": name, "identity": {"method": IDENTITY_METHOD, "hash": digest}}


def process_claim(arguments):
    path, identifier, document = arguments
    store = Store(path)
    try:
        created, _ = store.claim_host(*normalize_claim(identifier, document))
        return 201 if created else 200
    except HostClaimConflict:
        return 409


def host(identifier="host-a", name="Host A"):
    return normalize_host(identifier, {"display_name": name, "hardware": {"cpu": {"model": "Test CPU"}}})


def fixture(identifier=None, baseline=None, host_id="host-a", speed=1):
    identifier = identifier or str(uuid4())
    run = {
        "schema_version": 1, "run_id": identifier, "host_id": host_id,
        "started_at_utc": "2026-10-12T10:00:00Z", "finished_at_utc": "2026-10-12T10:00:10Z",
        "status": "completed", "failure_reason": None,
        "release_version": "0.83.1", "kernel_version": "0.83.1", "spectra_version": "1.0.0",
        "suite_version": "standard-1", "scoring_version": "geomean-1",
        "baseline_run_id": baseline if baseline is not None else identifier,
        "environment_id": "qemu-smp4-2g", "comparison_key": "standard-smp4", "instrumentation_mode": "standard",
        "notes": None,
        "environment": {
            "execution": "qemu", "cpu": {"model": "Test CPU"}, "vcpus": 4, "ram_bytes": "2147483648",
            "qemu": {"version": "10.1.0", "backend": "kvm"}, "storage": {"driver": "virtio", "cache": "none"},
            "workloads": {"items": 1000}, "cache_state": "warm", "warmup_runs": 1, "repetitions": 1,
        },
        "artifacts": [{"component_id": "kernel", "commit": "test-commit", "build_options": {"mode": "ReleaseSmall"}, "hashes": {"sha256": "a" * 64}}],
        "metrics": [], "samples": [], "scores": [],
    }
    for area in AREAS:
        higher = area == "filesystem_io"
        value = 1000 * speed if higher else 1000 / speed
        metric = {
            "area_id": area, "metric_id": "main", "metric_version": "1", "unit": "bytes/s" if higher else "ns/op",
            "direction": "higher_is_better" if higher else "lower_is_better", "primary": True, "weight": 1,
            "status": "ok", "failure_reason": None, "sample_count": 1,
            "median_value": value, "min_value": value, "max_value": value, "mad_value": 0, "checks": {"functional": True},
        }
        run["metrics"].append(metric)
        run["samples"].append({
            "area_id": area, "metric_id": "main", "sample_index": 0, "phase": "measurement", "status": "ok",
            "failure_reason": None, "elapsed_ns": str(MAX_U64), "operation_count": str(MAX_U64),
            "bytes_processed": str(MAX_U64), "value": value, "clock_source": "tsc", "clock_generation": str(MAX_U64),
            "clock_resolution_ns": 1, "observer_overhead_ns": 0,
        })
    return run


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="devcenter-test-", dir=tempfile.gettempdir())
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "benchmarks.sqlite3")
        self.store.put_host(host())

    def put(self, data):
        return self.store.put_run(normalize_run(data["run_id"], data))

    def test_reference_is_100_and_persistent(self):
        data = fixture()
        created, validation = self.put(data)
        self.assertTrue(created)
        self.assertEqual(validation["status"], "valid")
        self.assertTrue(all(score["score"] == "100" for score in validation["areas"].values()))
        reopened = Store(self.store.path)
        self.assertEqual(reopened.run(data["run_id"])["score_validation"], validation)

    def test_identical_retry_and_immutable_conflict(self):
        data = fixture()
        self.put(data)
        retry = deepcopy(data)
        retry["metrics"].reverse()
        retry["samples"].reverse()
        retry["samples"][0]["elapsed_ns"] = MAX_U64
        self.assertFalse(self.put(retry)[0])
        changed = deepcopy(data)
        changed["notes"] = "changed"
        with self.assertRaises(Conflict):
            self.put(changed)
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT count(*) FROM benchmark_runs").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM samples").fetchone()[0], 5)

    def test_u64_and_decimal_precision_round_trip(self):
        data = fixture()
        data["metrics"][0]["median_value"] = "123.12345678901234567890123456789"
        data["metrics"][0]["min_value"] = None
        data["metrics"][0]["max_value"] = None
        self.put(data)
        stored = self.store.run(data["run_id"])
        self.assertEqual(stored["samples"][0]["elapsed_ns"], str(MAX_U64))
        self.assertEqual(stored["samples"][0]["operation_count"], str(MAX_U64))
        self.assertEqual(stored["metrics"][0]["median_value"], data["metrics"][0]["median_value"])
        with self.store.connection() as connection:
            self.assertEqual(connection.execute("SELECT typeof(elapsed_ns) FROM samples LIMIT 1").fetchone()[0], "text")

    def test_atomic_rollback_of_run_and_children(self):
        with self.store.connection(write=True) as connection:
            connection.execute("CREATE TRIGGER sample_abort BEFORE INSERT ON samples BEGIN SELECT RAISE(ABORT, 'test-failure'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.put(fixture())
        with self.store.connection() as connection:
            for table in ("benchmark_runs", "metrics", "samples", "scores", "artifacts", "environments"):
                self.assertEqual(connection.execute("SELECT count(*) FROM " + table).fetchone()[0], 0, table)

    def test_concurrent_identical_imports_have_one_creation(self):
        data = normalize_run((raw := fixture())["run_id"], raw)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.store.put_run(data)[0], range(12)))
        self.assertEqual(sum(results), 1)

    def test_time_and_throughput_formulas_across_hosts(self):
        base = fixture()
        self.put(base)
        self.store.put_host(host("host-b", "Host B"))
        current = fixture(baseline=base["run_id"], host_id="host-b", speed=2)
        current["environment"]["cpu"]["model"] = "Different CPU"
        current["environment_id"] = "another-host-same-conditions"
        current["release_version"] = "0.83.2"
        created, validation = self.put(current)
        self.assertTrue(created)
        self.assertTrue(validation["comparison"]["cross_host"])
        self.assertTrue(validation["comparison"]["cpu_changed"])
        self.assertTrue(all(score["score"] == "200" for score in validation["areas"].values()))

    def test_suite3_native_all_cpu_comparison_and_worker_proof(self):
        def all_cpu(data, cpus):
            data["suite_version"] = "spectra-standard-3"
            data["environment"].update(
                vcpus=cpus, cpu_policy="all-available-v1",
                measurement="parallel-wall-ns-per-operation-v3", filesystem="ntfs",
                warmup_min_ns="1000000000", warmup_min_batches="3", settle_ms="200",
            )
            for sample in data["samples"]:
                sample.update(worker_count=str(cpus), completed_workers=str(cpus))
            for metric in data["metrics"]:
                if metric["primary"]:
                    metric["metric_version"] = "2"
            return data

        base = all_cpu(fixture(), 4)
        self.put(base)
        self.store.put_host(host("host-b", "Native host"))
        current = all_cpu(fixture(baseline=base["run_id"], host_id="host-b", speed=2), 16)
        current["environment"].update(execution="native", qemu=None, ram_bytes="25769803776", storage={"driver": "sata"})
        created, validation = self.put(current)
        self.assertTrue(created)
        self.assertEqual(validation["status"], "valid")
        self.assertTrue(validation["comparison"]["cross_host"])
        self.assertTrue(all(area["score"] == "200" for area in validation["areas"].values()))
        stored = self.store.run(current["run_id"])
        self.assertTrue(all(sample["worker_count"] == sample["completed_workers"] == "16" for sample in stored["samples"]))
        wrong = deepcopy(current)
        wrong["run_id"] = str(uuid4())
        wrong["samples"][0]["completed_workers"] = "15"
        with self.assertRaises(InvalidData):
            self.put(wrong)
        changed = deepcopy(current)
        changed["run_id"] = str(uuid4())
        changed["environment"]["warmup_min_ns"] = "1"
        self.assertEqual(self.put(changed)[1]["comparison"]["reason"], "environment_mismatch:warmup_min_ns")
        legacy_metric = all_cpu(fixture(), 4)
        for metric in legacy_metric["metrics"]:
            metric["metric_version"] = "1"
        created, validation = self.put(legacy_metric)
        self.assertTrue(created)
        self.assertEqual(validation["comparison"]["reason"], "metric_contract_mismatch:suite3_primary_metric_version")
        self.assertEqual({metric["metric_version"] for metric in self.store.run(legacy_metric["run_id"])["metrics"]}, {"1"})

    def test_weighted_geometric_mean_and_equal_area_weight(self):
        base = fixture()
        extra = deepcopy(base["metrics"][0])
        extra.update(metric_id="second", weight=1)
        base["metrics"][0]["weight"] = 3
        base["metrics"].append(extra)
        self.put(base)
        current = deepcopy(base)
        current["run_id"] = str(uuid4())
        current["baseline_run_id"] = base["run_id"]
        current["samples"] = []
        for metric in current["metrics"]:
            if metric["area_id"] == "scheduler_sync":
                value = 500 if metric["metric_id"] == "main" else 2000
                for key in ("median_value", "min_value", "max_value"):
                    metric[key] = value
        validation = self.put(current)[1]
        self.assertEqual(validation["areas"]["scheduler_sync"]["score"], "141.421356")
        self.assertEqual(validation["areas"]["overall"]["score"], "107.177346")

    def test_different_method_is_stored_but_not_scored(self):
        base = fixture()
        self.put(base)
        for key, value in (("vcpus", 8), ("ram_bytes", "4294967296"), ("cache_state", "cold"), ("workloads", {"items": 2})):
            current = fixture(baseline=base["run_id"])
            current["environment"][key] = value
            created, validation = self.put(current)
            self.assertTrue(created)
            self.assertEqual(validation["status"], "invalid")
            self.assertEqual(validation["comparison"]["reason"], "environment_mismatch:" + key)

    def test_unknown_baseline_resolves_when_it_arrives(self):
        base = fixture()
        current = fixture(baseline=base["run_id"], speed=2)
        self.assertEqual(self.put(current)[1]["status"], "pending")
        self.put(base)
        self.assertEqual(self.store.run(current["run_id"])["score_validation"]["areas"]["overall"]["score"], "200")
        self.assertFalse(self.put(current)[0])

    def test_failed_run_and_partial_samples_are_preserved(self):
        base = fixture()
        self.put(base)
        failed = fixture(baseline=base["run_id"])
        failed.update(status="failed", failure_reason="memory timeout", finished_at_utc=None)
        failed["metrics"][1].update(status="failed", median_value=None, min_value=None, max_value=None, mad_value=None, sample_count=0)
        failed["samples"][1].update(status="failed", value=None, elapsed_ns=None, failure_reason="timeout")
        validation = self.put(failed)[1]
        self.assertIsNone(validation["areas"]["overall"]["score"])
        self.assertEqual(validation["areas"]["scheduler_sync"]["status"], "valid")
        stored = self.store.run(failed["run_id"])
        self.assertEqual(len(stored["samples"]), 5)
        self.assertIsNone(stored["metrics"][1]["median_value"])

    def test_missing_area_or_failed_check_prevents_overall(self):
        base = fixture()
        self.put(base)
        current = fixture(baseline=base["run_id"])
        current["metrics"][0]["checks"]["functional"] = False
        validation = self.put(current)[1]
        self.assertEqual(validation["areas"]["overall"]["reason"], "five_valid_areas_required")

    def test_reported_scores_are_checked_and_preserved(self):
        data = fixture()
        data["scores"] = [{"area_id": "memory", "baseline_run_id": data["run_id"], "scoring_version": data["scoring_version"], "score": 999, "status": "valid", "reason": None}]
        validation = self.put(data)[1]
        self.assertEqual(validation["areas"]["memory"]["reason"], "reported_score_mismatch")
        self.assertEqual(self.store.run(data["run_id"])["scores"][0]["score"], "999")
        self.assertIsNone(validation["areas"]["overall"]["score"])

    def test_host_edits_keep_historical_snapshot(self):
        data = fixture()
        self.put(data)
        updated = host(name="Renamed host")
        updated["hardware"]["cpu"]["model"] = "New CPU"
        self.store.put_host(updated)
        self.assertEqual(self.store.run(data["run_id"])["host_snapshot"]["display_name"], "Host A")
        self.assertEqual(self.store.hosts("host-a")["display_name"], "Renamed host")
        self.assertFalse(self.put(data)[0])

    def test_filters_and_sample_pagination(self):
        data = fixture()
        self.put(data)
        query = {"host_id": "host-a", "release_version": "0.83.1", "environment_id": "qemu-smp4-2g", "date_from": "2026-10-12", "date_to": "2026-10-12"}
        self.assertEqual(self.store.runs(query)["total"], 1)
        self.assertEqual(self.store.runs({"date_from": "2026-10-13"})["total"], 0)
        self.assertEqual(self.store.samples(data["run_id"], {"limit": "2", "offset": "2"})["total"], 5)
        self.assertEqual(len(self.store.samples(data["run_id"], {"limit": "2", "offset": "2"})["items"]), 2)
        self.assertNotIn("samples", self.store.run(data["run_id"], include_samples=False))

    def test_invalid_schema_counters_and_children_are_rejected(self):
        for mutation in (
            lambda data: data.update(schema_version=2),
            lambda data: data["samples"][0].update(elapsed_ns=str(MAX_U64 + 1)),
            lambda data: data["samples"][0].update(operation_count=1.5),
            lambda data: data["environment"].update(vcpus=4.0),
            lambda data: data["metrics"][0].update(area_id={}),
            lambda data: data["metrics"][0].update(extra_field="must-not-disappear"),
            lambda data: data["samples"][0].update(metric_id="unknown"),
        ):
            data = fixture()
            mutation(data)
            with self.assertRaises(InvalidData):
                normalize_run(data["run_id"], data)
        for document in ('{"value":NaN}', '{"value":1,"value":2}'):
            with self.assertRaises(InvalidData):
                loads(document)
        for invalid in ({"display_name": 123}, {"display_name": "Host", "hardware": {"ram_bytes": 1.5}}):
            with self.assertRaises(InvalidData):
                normalize_host("invalid", invalid)

    def test_documented_examples_are_valid_including_cross_host_scores(self):
        examples = ROOT / "examples"
        for name in ("host.json", "host-b.json"):
            data = loads((examples / name).read_bytes())
            self.store.put_host(normalize_host(data["host_id"], data))
        for name, expected in (("benchmark-run.json", "100"), ("cross-host-run.json", "200")):
            data = loads((examples / name).read_bytes())
            validation = self.put(data)[1]
            self.assertEqual(validation["status"], "valid")
            self.assertEqual(validation["areas"]["overall"]["score"], expected)

    def test_small_positive_scores_do_not_round_to_zero(self):
        base = fixture()
        self.put(base)
        current = fixture(baseline=base["run_id"])
        for metric in current["metrics"]:
            value = "1e-20" if metric["direction"] == "higher_is_better" else "1e20"
            for key in ("median_value", "min_value", "max_value"):
                metric[key] = value
        validation = self.put(current)[1]
        self.assertEqual(validation["status"], "valid")
        self.assertGreater(Decimal(validation["areas"]["overall"]["score"]), 0)

    def test_version_mode_and_metric_contract_drift_prevent_scoring(self):
        base = fixture()
        self.put(base)
        for key in ("suite_version", "scoring_version", "instrumentation_mode"):
            current = fixture(baseline=base["run_id"])
            current[key] = "different"
            self.assertEqual(self.put(current)[1]["comparison"]["reason"], "comparison_mismatch:" + key)
        current = fixture(baseline=base["run_id"])
        current["metrics"][0]["metric_version"] = "different"
        self.assertEqual(self.put(current)[1]["areas"]["scheduler_sync"]["reason"], "metric_contract_mismatch:main")




from r4os_server.common.auth import password_hash
from r4os_server.benchmarks.service import create_server as benchmark_server
from WebApp.server import create_server as web_server


class HostClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "claims.sqlite3")

    def snapshot(self):
        with self.store.connection() as db:
            return {name: [tuple(row) for row in db.execute("SELECT * FROM " + name + " ORDER BY 1")]
                    for name in ("hosts", "host_identities")}

    def test_claim_retry_preserves_manual_metadata_timestamps_and_binding(self):
        document = claim_document()
        created, first = self.store.claim_host(*normalize_claim(CLAIM_ID, document))
        self.assertTrue(created)
        edited = {key: value for key, value in first.items() if not key.endswith("_at_utc")}
        edited.update(display_name="Editorial name", description="Keep this", tags=["native"],
                      metadata={"owner_note": "preserve"}, hardware={"logical_cpus": 8})
        self.store.put_host(normalize_host(CLAIM_ID, edited))
        before = self.snapshot()
        request = claim_document("Changed creation suggestion")
        created, saved = self.store.claim_host(*normalize_claim(CLAIM_ID, request))
        self.assertFalse(created)
        self.assertEqual(saved["display_name"], "Editorial name")
        self.assertEqual(saved["tags"], ["native"])
        self.assertEqual(saved["metadata"], {"owner_note": "preserve"})
        self.assertEqual(self.snapshot(), before)
        self.store = Store(self.store.path)
        self.assertFalse(self.store.claim_host(*normalize_claim(CLAIM_ID, request))[0])
        self.assertEqual(self.snapshot(), before)

    def test_claim_conflicts_never_adopt_or_modify_occupied_hosts(self):
        self.store.claim_host(*normalize_claim(CLAIM_ID, claim_document()))
        occupied_id = "hw1-" + "c" * 60
        self.store.put_host(normalize_host(occupied_id, {"display_name": "Existing manual host", "tags": ["historic"]}))
        before = self.snapshot()
        for identifier, document, reason in (
            (CLAIM_ID, claim_document(digest="c" * 64), "host_identity_conflict"),
            ("hw1-" + "d" * 60, claim_document(), "host_identity_in_use"),
            (occupied_id, claim_document(digest="e" * 64), "host_id_in_use"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaisesRegex(HostClaimConflict, "^" + reason + "$"):
                    self.store.claim_host(*normalize_claim(identifier, document))
                self.assertEqual(self.snapshot(), before)

    def test_parallel_process_claims_have_one_creation_and_consistent_conflicts(self):
        with ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(process_claim, [(str(self.store.path), CLAIM_ID, claim_document())] * 8))
        self.assertEqual(sorted(results), [200] * 7 + [201])
        other_id = "hw1-" + "e" * 60
        with ProcessPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(process_claim, [
                (str(self.store.path), other_id, claim_document(digest="1" * 64)),
                (str(self.store.path), other_id, claim_document(digest="2" * 64)),
            ]))
        self.assertEqual(sorted(results), [201, 409])
        snapshot = self.snapshot()
        self.assertEqual(len(snapshot["hosts"]), 2)
        self.assertEqual(len(snapshot["host_identities"]), 2)

    def test_parallel_manual_put_and_claim_preserve_manual_document(self):
        barrier = threading.Barrier(2)
        def claim():
            barrier.wait()
            return process_claim((str(self.store.path), CLAIM_ID, claim_document()))
        def manual():
            barrier.wait()
            return self.store.put_host(normalize_host(CLAIM_ID, {"display_name": "Manual", "tags": ["keep"]}))[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            claimed = pool.submit(claim)
            edited = pool.submit(manual)
            status, manual_created = claimed.result(), edited.result()
        self.assertEqual(status, 409 if manual_created else 201)
        saved = self.store.hosts(CLAIM_ID)
        self.assertEqual((saved["display_name"], saved["tags"]), ("Manual", ["keep"]))
        self.assertEqual(len(self.snapshot()["host_identities"]), 0 if manual_created else 1)

    def test_claim_binding_is_immutable_and_failed_insert_rolls_back(self):
        self.store.claim_host(*normalize_claim(CLAIM_ID, claim_document()))
        before = self.snapshot()
        for query in ("UPDATE host_identities SET identity_hash='" + "c" * 64 + "'",
                      "DELETE FROM host_identities"):
            with self.assertRaisesRegex(sqlite3.IntegrityError, "host_identity_immutable"):
                with self.store.connection(write=True) as db:
                    db.execute(query)
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER fail_claim BEFORE INSERT ON host_identities BEGIN SELECT RAISE(ABORT, 'test-claim-failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError, "test-claim-failure"):
            self.store.claim_host(*normalize_claim("hw1-" + "d" * 60, claim_document(digest="e" * 64)))
        self.assertEqual(self.snapshot(), before)

    def test_version3_migration_keeps_manual_hosts_runs_snapshots_and_scores(self):
        from test_subsystems import fixture as subsystem_fixture
        self.store.put_host(host())
        raw = fixture()
        self.store.put_run(normalize_run(raw["run_id"], raw))
        from r4os_server.benchmarks.subsystems import normalize
        snes = subsystem_fixture()
        self.store.put_subsystem_run(normalize(snes["run_id"], snes))
        occupied_id = "hw1-" + "c" * 60
        self.store.put_host(normalize_host(occupied_id, {"display_name": "Historical"}))
        with self.store.connection(write=True) as db:
            db.execute("DROP TRIGGER host_identity_no_update")
            db.execute("DROP TRIGGER host_identity_no_delete")
            db.execute("DROP TABLE host_identities")
            db.execute("PRAGMA user_version=3")
        def contents():
            with self.store.connection() as db:
                tables = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name!='host_identities' ORDER BY name")]
                return {name: [tuple(row) for row in db.execute('SELECT * FROM "' + name + '" ORDER BY 1')] for name in tables}
        before = contents()
        for _ in range(2):
            self.store = Store(self.store.path)
            self.assertEqual(contents(), before)
        with self.store.connection() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)
            self.assertEqual(db.execute("SELECT count(*) FROM host_identities").fetchone()[0], 0)
        with self.assertRaisesRegex(HostClaimConflict, "host_id_in_use"):
            self.store.claim_host(*normalize_claim(occupied_id, claim_document()))
        self.assertTrue(self.store.claim_host(*normalize_claim(CLAIM_ID, claim_document()))[0])
        self.assertEqual(self.store.run(raw["run_id"])["score_validation"]["status"], "valid")
        self.assertEqual(self.store.subsystem_run(snes["run_id"])["validation"]["status"], "valid")


def configuration(root, token=TOKEN):
    return Config(data_dir=Path(root),benchmarks_port=0,web_port=0,
        registration_secret="benchmark-test-registration-secret-001",
        benchmark_token=token,publisher_token="benchmark-test-publisher-token-00001",
        admin_token="benchmark-test-admin-token-000000001",
        session_secret="benchmark-test-session-secret-00001",
        admin_password_hash=password_hash("benchmark-test-password"))


class HTTPTests(unittest.TestCase):
    """Direct API authorization and password-free private WebApp administration."""
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config=configuration(self.temp.name)
        self.api=self.start(benchmark_server(config))
        self.api.policy.store.register_magic('127.0.0.1')
        config=replace(config,benchmarks_port=self.api.server_port)
        self.app=self.api.app
        self.app.config=config
        self.api.config=config
        self.api.policy.config=config
        self.web=self.start(web_server(config,port=0))

    def start(self,server):
        thread=threading.Thread(target=server.serve_forever,kwargs={'poll_interval':.01},daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def request(self,method,path,data=None,token=TOKEN,*,web=False,raw=None,content_type='application/json',extra_headers=None):
        target=self.web if web else self.api
        if web:path=path.replace('/api/v1/','/api/benchmarks/',1)
        connection=HTTPConnection('127.0.0.1',target.server_port,timeout=5)
        headers={'Content-Type':content_type,'X-R4OS-Request':'1'}
        if not web and token is not None:headers['Authorization']='Bearer '+token
        headers.update(extra_headers or {})
        connection.request(method,path,body=raw if raw is not None else (json.dumps(data) if data is not None else None),headers=headers)
        response=connection.getresponse();body=response.read();status=response.status;headers=dict(response.getheaders());connection.close()
        if body and headers.get('Content-Type','').startswith('application/json'):body=json.loads(body)
        return status,body,headers

    def test_http_auth_import_retry_conflict_and_readback(self):
        route='/api/v1/hosts/host-a';document={'display_name':'Host A'}
        for token in (None,'wrong'):
            self.assertEqual(self.request('PUT',route,document,token=token)[0],401)
        self.assertEqual(self.request('PUT',route,document)[0],201)
        self.assertEqual(self.request('PUT',route,document,web=True)[0],200)
        run=fixture();route='/api/v1/benchmark-runs/'+run['run_id']
        self.assertEqual(self.request('PUT',route,run)[0],201)
        self.assertEqual(self.request('PUT',route,run)[0],200)
        run['notes']='changed'
        self.assertEqual(self.request('PUT',route,run)[0],409)
        status,saved,_=self.request('GET',route,web=True)
        self.assertEqual(status,200)
        self.assertEqual(saved['samples'][0]['elapsed_ns'],str(MAX_U64))
        self.assertEqual(self.request('HEAD',route)[0],200)

    def test_http_invalid_json_unknown_host_and_private_files(self):
        route='/api/v1/benchmark-runs/'+str(uuid4())
        self.assertEqual(self.request('PUT',route,raw='{broken')[0],422)
        self.assertEqual(self.request('PUT',route,raw='{}',content_type='text/plain')[0],415)
        run=fixture()
        self.assertEqual(self.request('PUT','/api/v1/benchmark-runs/'+run['run_id'],run)[0],422)
        for query in ('limit=0','host_id=a&host_id=b'):
            self.assertEqual(self.request('GET','/api/v1/benchmark-runs?'+query)[0],422)
        self.assertEqual(self.request('GET','/Private/server.json',web=True)[0],404)
        self.assertEqual(self.request('GET','/api/v1/unknown')[0],404)
        self.assertEqual(self.request('DELETE',route)[0],405)

    def test_web_host_create_and_edit_without_password(self):
        route='/api/v1/hosts/host-a'
        document={'display_name':'Host A','hardware':{'ram_bytes':str(MAX_U64)}}
        status,result,_=self.request('PUT',route,document,web=True,token=None)
        self.assertEqual(status,201)
        self.assertEqual(result['host']['hardware']['ram_bytes'],str(MAX_U64))
        document['display_name']='Host A edited'
        self.assertEqual(self.request('PUT',route,document,web=True,token=None)[0],200)
        self.assertEqual(self.request('GET',route,token=None)[1]['display_name'],'Host A edited')
        self.assertEqual(self.request('PUT',route,raw='{broken',web=True)[0],422)
        for asset in ('/','/app.js'):
            status,body,_=self.request('GET',asset,web=True)
            self.assertEqual(status,200)
            self.assertNotIn(TOKEN,body.decode())

    def test_web_host_access_does_not_bypass_api_auth(self):
        route='/api/v1/hosts/host-a';document={'display_name':'Host A'}
        for token in (None,'wrong'):
            self.assertEqual(self.request('PUT',route,document,token=token)[0],401)
        self.assertEqual(self.request('PUT',route,document)[0],201)
        for origin in ('http://example.invalid','null','http://['):
            self.assertEqual(self.request('PUT',route,document,web=True,extra_headers={'Origin':origin})[0],403)
        self.assertEqual(self.request('GET',route,web=True,extra_headers={'Host':'untrusted.invalid'})[0],403)
        self.assertEqual(self.request('PUT',route,document,web=True,extra_headers={'Host':'untrusted.invalid','Origin':'http://untrusted.invalid'})[0],403)
        self.assertFalse(self.web.verify_request(None,('203.0.113.20',12345)))
        for host in ('0.0.0.0','138.201.253.143'):
            with self.assertRaises(ValueError):web_server(self.web.config,host=host,port=0)
        with self.assertRaises(ValueError):
            web_server(replace(self.web.config,allowed_networks=('0.0.0.0/0',)),port=0)
        for path in ('/api/session','/api/login','/api/logout'):
            self.assertEqual(self.request('GET',path,web=True)[0],404)
        run=fixture();route='/api/v1/benchmark-runs/'+run['run_id']
        self.assertEqual(self.request('PUT',route,run,token=None)[0],401)
        self.assertEqual(self.request('PUT',route,run,web=True)[0],201)

    def test_oversized_bodies_and_oversized_numeric_literals(self):
        connection=HTTPConnection('127.0.0.1',self.api.server_port,timeout=5)
        connection.putrequest('PUT','/api/v1/hosts/a')
        connection.putheader('Authorization','Bearer '+TOKEN)
        connection.putheader('Content-Type','application/json')
        connection.putheader('Content-Length',str(self.api.config.benchmark_limit+1));connection.endheaders()
        response=connection.getresponse();self.assertEqual(response.status,413);response.read();connection.close()
        self.assertEqual(self.request('PUT','/api/v1/hosts/a',raw='{"value":'+ '9'*5000+'}')[0],422)

    def test_http_host_claim_create_retry_and_conflict_contract(self):
        route='/api/v1/hosts/'+CLAIM_ID+'/claim'
        document=claim_document()
        status,body,_=self.request('POST',route,document)
        self.assertEqual((status,body['created'],body['stored']),(201,True,True))
        self.assertEqual(body['identity'],document['identity'])
        host_route='/api/v1/hosts/'+CLAIM_ID
        edited={key:value for key,value in body['host'].items() if not key.endswith('_at_utc')}
        edited.update(display_name='Edited manually',tags=['keep'],metadata={'note':'preserve'})
        self.assertEqual(self.request('PUT',host_route,edited)[0],200)
        before=self.request('GET',host_route)[1]
        status,body,_=self.request('POST',route,claim_document('Another suggestion'))
        self.assertEqual((status,body['created'],body['host']['display_name']),(200,False,'Edited manually'))
        self.assertEqual(self.request('POST',route,claim_document(digest='c'*64))[0],409)
        self.assertEqual(self.request('GET',host_route)[1],before)
        self.assertNotIn('identity',before)
        status,body,_=self.request('POST','/api/v1/hosts/'+'hw1-'+'d'*60+'/claim',document)
        self.assertEqual((status,body['error']),(409,'host_identity_in_use'))

    def test_http_host_claim_validation_is_bounded_and_does_not_echo_raw_identity(self):
        route='/api/v1/hosts/'+CLAIM_ID+'/claim'
        raw_uuid='00112233-4455-6677-8899-aabbccddeeff'
        invalid=[]
        for field,value in (('method','unsupported-v2'),('hash','B'*64),('hash',raw_uuid),('hash','a'*63)):
            document=claim_document();document['identity'][field]=value;invalid.append(document)
        for field in ('smbios_uuid','serial_number','hardware','metadata'):
            document=claim_document();document[field]=raw_uuid;invalid.append(document)
        document=claim_document();document['identity']['uuid']=raw_uuid;invalid.append(document)
        with self.assertLogs(level='INFO') as captured:
            for document in invalid:
                status,body,_=self.request('POST',route,document)
                self.assertEqual(status,422)
                self.assertNotIn(raw_uuid,json.dumps(body))
            status,body,_=self.request('POST','/api/v1/hosts/'+raw_uuid+'/claim',claim_document())
            self.assertEqual(status,422);self.assertNotIn(raw_uuid,json.dumps(body))
            status,body,_=self.request('POST',route,raw='{"'+raw_uuid+'":1,"'+raw_uuid+'":2}')
            self.assertEqual(status,422);self.assertNotIn(raw_uuid,json.dumps(body))
        self.assertNotIn(raw_uuid,'\n'.join(captured.output))
        self.assertEqual(self.request('POST',route,raw='x'*4097)[0],413)
        self.assertEqual(self.request('POST',route,raw='{}',content_type='text/plain')[0],415)
        self.assertEqual(self.request('POST',route+'?extra=1',claim_document())[0],422)
        self.assertEqual(self.request('POST','/api/v1/hosts/'+CLAIM_ID,claim_document())[0],405)
        self.assertEqual(self.app.store.hosts()['total'],0)

    def test_http_host_claim_keeps_registration_roles_blocks_and_limits(self):
        route='/api/v1/hosts/'+CLAIM_ID+'/claim'
        document=claim_document()
        for token,status in ((None,401),('wrong',401),(self.api.config.publisher_token,403)):
            self.assertEqual(self.request('POST',route,document,token=token)[0],status)
        self.api.policy.store.revoke('127.0.0.1')
        status,body,_=self.request('POST',route,document)
        self.assertEqual((status,body['error']),(401,'registration_required'))
        self.api.policy.store.register_magic('127.0.0.1')
        self.assertEqual(self.request('POST',route,document)[0],201)
        self.api.policy.store.block_ip('127.0.0.1','isolated-test',3600)
        status,body,_=self.request('POST',route,document)
        self.assertEqual((status,body['error']),(403,'registration_required'))
        self.assertEqual(self.request('POST',route,document,token=self.api.config.admin_token)[0],200)
        self.api.policy.store.unblock('127.0.0.1','isolated-test')
        self.api.policy.store.register_magic('127.0.0.1')
        self.api.policy.store.set_rate('benchmarks',1)
        self.assertEqual(self.request('POST',route,document)[0],429)
