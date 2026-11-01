"""Persistent automatic references and independent scores; original imports stay immutable."""

from decimal import Decimal, localcontext
import json
from statistics import median

from .models import SCORE_AREAS, fingerprint, now
from .scoring import ALL_CPU_METHOD_FIELDS, calculate_from_measurements


POLICY = "first-valid-standard-v1"
SCHEMA = """
CREATE TABLE IF NOT EXISTS benchmark_baseline_candidates (
    run_id TEXT PRIMARY KEY REFERENCES benchmark_runs(run_id),
    group_id TEXT NOT NULL, policy TEXT NOT NULL,
    eligible INTEGER NOT NULL CHECK(eligible IN (0,1)), reason TEXT,
    UNIQUE(group_id, run_id)
);
CREATE INDEX IF NOT EXISTS baseline_candidates_group ON benchmark_baseline_candidates(group_id);
CREATE TABLE IF NOT EXISTS benchmark_baselines (
    group_id TEXT PRIMARY KEY, policy TEXT NOT NULL,
    baseline_run_id TEXT NOT NULL UNIQUE,
    selected_at_utc TEXT NOT NULL,
    FOREIGN KEY(group_id, baseline_run_id) REFERENCES benchmark_baseline_candidates(group_id, run_id)
);
CREATE TRIGGER IF NOT EXISTS benchmark_baseline_no_update BEFORE UPDATE ON benchmark_baselines
BEGIN SELECT RAISE(ABORT, 'automatic_baseline_immutable'); END;
CREATE TRIGGER IF NOT EXISTS benchmark_candidate_no_update BEFORE UPDATE ON benchmark_baseline_candidates
BEGIN SELECT RAISE(ABORT, 'automatic_candidate_immutable'); END;
"""


def supported(run):
    return run.get("suite_version") == "spectra-standard-3" and run.get("scoring_version") == "spectra-geomean-1"


def group_id(run):
    descriptor = {key: run[key] for key in ("comparison_key", "suite_version", "scoring_version", "instrumentation_mode")}
    descriptor.update(policy=POLICY,
                      method={key: run["environment"].get(key) for key in ALL_CPU_METHOD_FIELDS},
                      metrics=sorted((tuple(metric[key] for key in ("area_id", "metric_id", "metric_version", "unit", "direction", "weight"))
                                      for metric in run["metrics"] if metric["primary"]), key=lambda item: (item[0], item[1])))
    return fingerprint(descriptor)


def eligibility_reason(run):
    if run["status"] != "completed":
        return "run_not_completed"
    if run.get("failure_reason"):
        return "run_failure_reported"
    assessment = calculate_from_measurements(run, run)
    if assessment["status"] != "valid":
        return assessment["comparison"]["reason"] or next(
            (value["reason"] for value in assessment["areas"].values() if value["status"] != "valid"),
            "five_valid_areas_required")
    repetitions = int(run["environment"]["repetitions"])
    cpus = str(run["environment"]["vcpus"])
    with localcontext() as context:
        context.prec = 60
        for metric in (item for item in run["metrics"] if item["primary"]):
            if any(metric["checks"].get(key) is not True for key in ("clock_valid", "work_complete", "cleanup_confirmed")):
                return "standard_metric_checks_incomplete"
            samples = [item for item in run["samples"] if (item["area_id"], item["metric_id"]) == (metric["area_id"], metric["metric_id"])]
            measured = [item for item in samples if item["phase"] == "measurement"]
            if len(measured) != repetitions or int(metric["sample_count"]) != repetitions:
                return "standard_raw_samples_incomplete"
            if int(run["environment"]["warmup_runs"]) > 0 and not any(item["phase"] == "warmup" for item in samples):
                return "standard_warmup_missing"
            if any(item["status"] != "ok" or item["value"] is None or Decimal(item["value"]) <= 0
                   or int(item["operation_count"] or 0) <= 0 or int(item["elapsed_ns"] or 0) <= 0
                   or item.get("worker_count") != cpus or item.get("completed_workers") != cpus for item in samples):
                return "standard_raw_sample_invalid"
            values = [Decimal(item["value"]) for item in measured]
            middle = median(values)
            statistics = {"median_value": middle, "min_value": min(values), "max_value": max(values),
                          "mad_value": median([abs(value-middle) for value in values])}
            if any(metric[key] is not None and abs(Decimal(metric[key])-value) > Decimal("0.000001")
                   for key, value in statistics.items()):
                return "standard_metric_summary_mismatch"
    return None


def register(connection, run):
    if not supported(run):
        return
    if connection.execute("SELECT 1 FROM benchmark_baseline_candidates WHERE run_id=?", (run["run_id"],)).fetchone():
        return
    group = group_id(run)
    reason = eligibility_reason(run)
    connection.execute("INSERT INTO benchmark_baseline_candidates VALUES (?,?,?,?,?)",
                       (run["run_id"], group, POLICY, int(reason is None), reason))
    if reason is None:
        connection.execute("INSERT INTO benchmark_baselines VALUES (?,?,?,?) ON CONFLICT(group_id) DO NOTHING",
                           (group, POLICY, run["run_id"], now()))


def backfill(connection):
    rows = connection.execute("SELECT r.scoring_input_json FROM benchmark_runs r LEFT JOIN benchmark_baseline_candidates c ON c.run_id=r.run_id "
                              "WHERE c.run_id IS NULL AND r.suite_version=? AND r.scoring_version=? ORDER BY r.imported_at_utc, r.run_id",
                              ("spectra-standard-3", "spectra-geomean-1")).fetchall()
    for row in rows:
        run = json.loads(row[0])
        if supported(run):
            run["samples"] = [json.loads(item[0]) for item in connection.execute(
                "SELECT document_json FROM samples WHERE run_id=? ORDER BY ordinal", (run["run_id"],))]
            register(connection, run)


def assessment(connection, run):
    result = {"policy": POLICY, "applicable": supported(run), "requested_baseline_run_id": run["baseline_run_id"],
              "baseline_run_id": None, "baseline_group_id": None, "is_baseline": False,
              "reported_scores_validated": False}
    reason = "unsupported_automatic_scoring_contract"
    status = "unscored"
    if result["applicable"]:
        candidate = connection.execute("SELECT * FROM benchmark_baseline_candidates WHERE run_id=?", (run["run_id"],)).fetchone()
        status, reason = "pending", "server_baseline_unavailable"
        if candidate:
            result["baseline_group_id"] = candidate["group_id"]
            row = connection.execute("SELECT b.baseline_run_id,r.scoring_input_json FROM benchmark_baselines b "
                                     "JOIN benchmark_runs r ON r.run_id=b.baseline_run_id WHERE b.group_id=?", (candidate["group_id"],)).fetchone()
            if row:
                result.update(baseline_run_id=row["baseline_run_id"], is_baseline=row["baseline_run_id"] == run["run_id"])
            if not candidate["eligible"]:
                status, reason = "invalid", candidate["reason"]
            elif row:
                result.update(calculate_from_measurements(run, json.loads(row["scoring_input_json"])))
                return result
    result.update(status=status, comparison={"status": "pending" if status == "pending" else "not_comparable",
                                            "reason": reason, "cross_host": False, "cpu_changed": False},
                  areas={area: {"score": None, "status": status, "reason": reason, "metric_ratios": []} for area in SCORE_AREAS})
    return result
