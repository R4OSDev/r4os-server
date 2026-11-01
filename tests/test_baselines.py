"""Automatic references use isolated measurements and never rewrite reported imports."""

from concurrent.futures import ProcessPoolExecutor
from copy import deepcopy
from http.client import HTTPConnection
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest

import test_benchmarks as benchmarks
from r4os_server.benchmarks.baseline import POLICY
from r4os_server.benchmarks.models import AREAS, normalize_host, normalize_run
from r4os_server.benchmarks.service import create_server
from r4os_server.benchmarks.store import Store


MISSING = "00000000-0000-4000-8000-000000000001"
DERIVED = {"benchmark_baselines", "benchmark_baseline_candidates"}


def standard(*, speed=1, cpus=4, host="host-a"):
    raw = benchmarks.fixture(host_id=host)
    raw.update(suite_version="spectra-standard-3", scoring_version="spectra-geomean-1",
               baseline_run_id=MISSING, instrumentation_mode="base")
    raw["environment"].update(vcpus=cpus, cpu_policy="all-available-v1",
                              measurement="parallel-wall-ns-per-operation-v3", filesystem="ntfs",
                              warmup_runs=1, warmup_min_ns="1000000000", warmup_min_batches="3",
                              repetitions=5, settle_ms="200")
    raw["samples"] = []
    for index, metric in enumerate(raw["metrics"]):
        value = str((index+1)*1000//speed)
        metric.update(metric_id="elapsed_ns_per_operation", metric_version="2", unit="ns/operation",
                      direction="lower_is_better", sample_count=5, median_value=value,
                      min_value=value, max_value=value, mad_value="0",
                      checks={"clock_valid": True, "work_complete": True, "cleanup_confirmed": True})
        for sample_index in range(6):
            raw["samples"].append({"area_id": metric["area_id"], "metric_id": metric["metric_id"],
                                   "sample_index": sample_index, "phase": "warmup" if sample_index == 0 else "measurement",
                                   "status": "ok", "value": value, "elapsed_ns": int(value)*100,
                                   "operation_count": 100, "worker_count": cpus, "completed_workers": cpus,
                                   "clock_source": "tsc", "clock_generation": 1})
    raw["scores"] = [{"area_id": area, "baseline_run_id": MISSING, "scoring_version": "spectra-geomean-1",
                      "score": "123", "status": "valid", "reason": None} for area in (*AREAS, "overall")]
    return raw


def import_in_process(arguments):
    path, raw = arguments
    store = Store(path)
    store.put_run(normalize_run(raw["run_id"], raw))
    return store.server_scoring(raw["run_id"])["baseline_run_id"]


def original_tables(store):
    with store.connection() as db:
        names = [row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name") if row[0] not in DERIVED]
        return {name: [tuple(row) for row in db.execute('SELECT * FROM "'+name+'" ORDER BY rowid')] for name in names}


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name)/"benchmarks.sqlite3")
        self.store.put_host(normalize_host("host-a", {"display_name": "Host A"}))

    def put(self, raw):
        return self.store.put_run(normalize_run(raw["run_id"], raw))

    def test_empty_server_rebases_measurements_without_validating_old_reports(self):
        first, second = standard(), standard(speed=2)
        self.assertTrue(self.put(first)[0])
        self.assertTrue(self.put(second)[0])
        for raw, expected in ((first,"100"),(second,"200")):
            saved = self.store.run(raw["run_id"])
            assessment = saved["server_scoring"]
            self.assertEqual((assessment["policy"], assessment["status"], assessment["baseline_run_id"]), (POLICY,"valid",first["run_id"]))
            self.assertEqual(assessment["areas"]["overall"]["score"], expected)
            self.assertFalse(assessment["reported_scores_validated"])
            self.assertTrue(all("reported_matches" not in value and "reported_score" not in value for value in assessment["areas"].values()))
            self.assertEqual(saved["baseline_run_id"], MISSING)
            self.assertEqual(saved["score_validation"]["status"], "pending")
            self.assertEqual(saved["score_validation"]["comparison"]["reason"], "unknown_baseline")
            self.assertTrue(all(score["score"] == "123" and score["baseline_run_id"] == MISSING for score in saved["scores"]))
        before = original_tables(self.store)
        self.assertFalse(self.put(first)[0])
        self.store = Store(self.store.path)
        self.assertEqual(original_tables(self.store), before)
        self.assertEqual(self.store.server_scoring(second["run_id"])["baseline_run_id"], first["run_id"])

    def test_invalid_or_incomplete_first_runs_never_reserve_a_reference(self):
        invalid = []
        raw = standard();raw["samples"].pop();invalid.append((raw,"standard_raw_samples_incomplete"))
        raw = standard();raw["status"]="incomplete";invalid.append((raw,"run_not_completed"))
        raw = standard();raw["metrics"][0]["median_value"]="999";raw["metrics"][0]["min_value"]="999";invalid.append((raw,"standard_metric_summary_mismatch"))
        raw = standard();raw["samples"][0]["status"]="failed";invalid.append((raw,"standard_raw_sample_invalid"))
        raw = standard();raw["metrics"][0]["checks"]={};invalid.append((raw,"standard_metric_checks_incomplete"))
        raw = standard();raw["failure_reason"]="reported failure";invalid.append((raw,"run_failure_reported"))
        for raw, reason in invalid:
            self.put(raw)
            result=self.store.server_scoring(raw["run_id"])
            self.assertEqual(result["status"],"invalid")
            self.assertEqual(result["comparison"]["reason"],reason)
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM benchmark_baselines").fetchone()[0],0)
        good=standard();self.put(good)
        self.assertTrue(self.store.server_scoring(good["run_id"])["is_baseline"])

    def test_groups_keep_method_contracts_and_allow_existing_cpu_host_differences(self):
        first=standard();self.put(first)
        self.store.put_host(normalize_host("host-b",{"display_name":"Host B"}))
        other=standard(speed=2,cpus=8,host="host-b")
        other["environment"]["ram_bytes"]=4294967296
        self.put(other)
        result=self.store.server_scoring(other["run_id"])
        self.assertEqual(result["baseline_run_id"],first["run_id"])
        self.assertTrue(result["comparison"]["cross_host"])
        changed=standard();changed["environment"]["cache_state"]="other-method"
        self.put(changed)
        self.assertTrue(self.store.server_scoring(changed["run_id"])["is_baseline"])
        with self.store.connection() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM benchmark_baselines").fetchone()[0],2)

    def test_parallel_process_imports_choose_one_persistent_reference(self):
        runs=[standard() for _ in range(8)]
        with ProcessPoolExecutor(max_workers=4) as pool:
            references=list(pool.map(import_in_process,[(str(self.store.path),raw) for raw in runs]))
        self.assertEqual(len(set(references)),1)
        self.assertIn(references[0],{raw["run_id"] for raw in runs})
        self.store=Store(self.store.path)
        self.assertFalse(self.put(runs[-1])[0])
        self.assertEqual(self.store.server_scoring(runs[-1]["run_id"])["baseline_run_id"],references[0])
        with self.assertRaisesRegex(sqlite3.IntegrityError,"automatic_baseline_immutable"):
            with self.store.connection(write=True) as db:
                db.execute("UPDATE benchmark_baselines SET baseline_run_id=baseline_run_id")

    def test_failed_election_rolls_back_run_samples_reports_and_candidates(self):
        before=original_tables(self.store)
        with self.store.connection(write=True) as db:
            db.execute("CREATE TRIGGER fail_baseline BEFORE INSERT ON benchmark_baselines BEGIN SELECT RAISE(ABORT,'test-election-failure'); END")
        with self.assertRaisesRegex(sqlite3.IntegrityError,"test-election-failure"):
            self.put(standard())
        self.assertEqual(original_tables(self.store),before)
        with self.store.connection() as db:
            for table in DERIVED:
                self.assertEqual(db.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_schema4_backfill_uses_import_order_and_keeps_original_rows(self):
        first,second=standard(),standard(speed=2)
        second["started_at_utc"]="2026-10-01T00:00:00Z"
        self.put(first);self.put(second)
        with self.store.connection(write=True) as db:
            db.execute('DROP TRIGGER benchmark_baseline_no_update')
            db.execute('DROP TRIGGER benchmark_candidate_no_update')
            db.execute('DROP TABLE benchmark_baselines')
            db.execute('DROP TABLE benchmark_baseline_candidates')
            db.execute('PRAGMA user_version=4')
        before=original_tables(self.store)
        self.store=Store(self.store.path)
        self.assertEqual(original_tables(self.store),before)
        self.assertEqual(self.store.server_scoring(second["run_id"])["baseline_run_id"],first["run_id"])
        self.store=Store(self.store.path)
        self.assertEqual(original_tables(self.store),before)

    def test_bad_reported_scores_remain_invalid_while_server_measurements_are_valid(self):
        raw=standard();raw["baseline_run_id"]=raw["run_id"]
        for score in raw["scores"]:score.update(baseline_run_id=raw["run_id"],score="999")
        _,legacy=self.put(raw)
        self.assertEqual(legacy["status"],"invalid")
        self.assertEqual(legacy["areas"]["memory"]["reason"],"reported_score_mismatch")
        computed=self.store.server_scoring(raw["run_id"])
        self.assertEqual((computed["status"],computed["areas"]["overall"]["score"]),("valid","100"))
        self.assertFalse(computed["reported_scores_validated"])


class BaselineHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.server=create_server(benchmarks.configuration(self.temp.name))
        self.addCleanup(self.server.server_close);self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever,kwargs={"poll_interval":.01},daemon=True).start()
        self.server.policy.store.register_magic('127.0.0.1')
        self.server.app.store.put_host(normalize_host('host-a',{'display_name':'Host A'}))

    def request(self,method,path,body=None):
        client=HTTPConnection('127.0.0.1',self.server.server_port,timeout=5)
        try:
            client.request(method,path,None if body is None else json.dumps(body),
                           {'Authorization':'Bearer '+benchmarks.TOKEN,'Content-Type':'application/json'})
            response=client.getresponse()
            return response.status,json.loads(response.read())
        finally:client.close()

    def test_put_retry_get_lists_and_distinct_reference_filters(self):
        first,second=standard(),standard(speed=2)
        for raw in (first,second):
            status,body=self.request('PUT','/api/v1/benchmark-runs/'+raw['run_id'],raw)
            self.assertEqual(status,201)
            self.assertEqual(body['score_validation']['status'],'pending')
            self.assertEqual(body['server_scoring']['status'],'valid')
        status,body=self.request('PUT','/api/v1/benchmark-runs/'+second['run_id'],second)
        self.assertEqual((status,body['duplicate']),(200,True))
        self.assertEqual(body['server_scoring']['baseline_run_id'],first['run_id'])
        status,saved=self.request('GET','/api/v1/benchmark-runs/'+second['run_id'])
        self.assertEqual((status,saved['baseline_run_id']),(200,MISSING))
        self.assertEqual(saved['server_scoring']['areas']['overall']['score'],'200')
        status,page=self.request('GET','/api/v1/benchmark-runs?server_baseline_run_id='+first['run_id'])
        self.assertEqual((status,page['total']),(200,2))
        self.assertTrue(all(run['server_scoring']['status']=='valid' for run in page['items']))
        self.assertEqual(self.request('GET','/api/v1/benchmark-runs?baseline_run_id='+first['run_id'])[1]['total'],0)
        inventory=self.request('GET','/api/v1/benchmark-filters')[1]
        self.assertEqual(inventory['server_baselines'][0]['baseline_run_id'],first['run_id'])
        self.assertEqual(inventory['baselines'][0]['baseline_run_id'],MISSING)
        changed=deepcopy(second);changed['notes']='changed original'
        self.assertEqual(self.request('PUT','/api/v1/benchmark-runs/'+second['run_id'],changed)[0],409)
