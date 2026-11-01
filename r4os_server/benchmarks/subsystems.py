"""SNES benchmark contract. Independent of Spectra areas and score references."""

from copy import deepcopy
from decimal import Decimal, localcontext
import hashlib
import re
from statistics import median

from .models import (InvalidData, RUN_STATUSES, enum, host_id, integer,
                     json_value, object_value, text, timestamp, uuid, canonical)

SCHEMA = """
CREATE TABLE IF NOT EXISTS subsystem_runs (
    run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL,
    benchmark_family TEXT NOT NULL, subsystem_id TEXT NOT NULL,
    host_id TEXT NOT NULL REFERENCES hosts(host_id),
    started_at_utc TEXT, finished_at_utc TEXT, status TEXT NOT NULL,
    profile TEXT NOT NULL, region TEXT NOT NULL, instrumentation_mode TEXT NOT NULL,
    scene_id TEXT, rom_sha256 TEXT, emulator_version TEXT, release_version TEXT,
    backend TEXT, content_hash TEXT NOT NULL, payload_json TEXT NOT NULL,
    host_snapshot_json TEXT NOT NULL, imported_at_utc TEXT NOT NULL, summary_json TEXT
);
CREATE INDEX IF NOT EXISTS subsystem_date ON subsystem_runs(started_at_utc DESC,run_id);
CREATE INDEX IF NOT EXISTS subsystem_host ON subsystem_runs(host_id,profile,region,instrumentation_mode);
"""
PHASES = "cpu_bus,dma,ppu,smp,dsp,coprocessor"
OBSERVATIONS = ("maximum_pending_master_cycles", "dropped_presents", "audio_failures", "audio_discarded_bytes")
FIELDS = {
    "schema_version", "benchmark_family", "subsystem_id", "run_id", "host_id",
    "started_at_utc", "finished_at_utc", "status", "failure_reason", "profile",
    "region", "instrumentation_mode", "workload", "environment", "artifacts",
    "versions", "samples", "summary",
}


def lossless(value, path):
    value = json_value(value, path)
    def walk(item):
        if type(item) is int:
            if item < 0:
                if item < -(1 << 63):
                    raise InvalidData(path, "Metadata is outside int64 range.")
                return str(item)
            return integer(item, path, nullable=False)
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, dict):
            return {k: walk(v) for k, v in item.items()}
        return item
    return walk(value)


def normalize(identifier, document):
    data = deepcopy(object_value(document, "run"))
    unknown = set(data) - FIELDS
    if unknown:
        raise InvalidData("run", "Unknown fields: " + ", ".join(sorted(unknown)))
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise InvalidData("schema_version", "Supported subsystem schema version: 1.")
    data["run_id"] = uuid(data.get("run_id"), "run_id")
    if data["run_id"] != uuid(identifier, "run_id"):
        raise InvalidData("run_id", "Path and document differ.")
    enum(data.get("benchmark_family"), ("snes",), "benchmark_family")
    enum(data.get("subsystem_id"), ("r4os.snes",), "subsystem_id")
    data["host_id"] = host_id(data.get("host_id"))
    data["status"] = enum(data.get("status"), RUN_STATUSES, "status")
    data["failure_reason"] = text(data.get("failure_reason"), "failure_reason", nullable=True, maximum=16384)
    for field in ("started_at_utc", "finished_at_utc"):
        data[field] = timestamp(data.get(field), field)
    if data["started_at_utc"] and data["finished_at_utc"] and data["finished_at_utc"] < data["started_at_utc"]:
        raise InvalidData("finished_at_utc", "End precedes start.")
    enum(data.get("profile"), ("core-throughput", "window-audio"), "profile")
    enum(data.get("region"), ("ntsc", "pal"), "region")
    enum(data.get("instrumentation_mode"), ("normal", "detail"), "instrumentation_mode")
    for field in ("workload", "environment", "versions", "summary"):
        data[field] = lossless(object_value(data.get(field), field), field)
    for field, limit in (("artifacts", 128), ("samples", 20)):
        values = data.get(field)
        if not isinstance(values, list) or len(values) > limit:
            raise InvalidData(field, f"A list of at most {limit} entries required.")
        data[field] = [lossless(object_value(item, field), field) for item in values]
    # Older first exports carry the same no-event contract without the derived
    # hash/child status. Normalize optional metadata, never change measurements.
    workload = data["workload"]
    if workload.get("input_contract") == "no-events-v1":
        workload.setdefault("input_sha256", hashlib.sha256(b"no-events-v1").hexdigest())
    for sample in data["samples"]:
        sample.setdefault("status", "ok")
        sample.setdefault("failure_reason", None)
        if isinstance(sample.get("warmup"), dict):
            sample["warmup"].setdefault("status", "ok")
    return data


def number(value, field):
    return int(integer(value, field, nullable=False))


def digest(value):
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def statistics(values):
    if not values:
        return None
    middle = median(values)
    return {"median_value": format(middle, "f"), "min_value": format(min(values), "f"),
            "max_value": format(max(values), "f"), "mad_value": format(median([abs(v-middle) for v in values]), "f")}


def observations(run):
    """List/chart summaries of available raw counters; unknown stays unknown."""
    result = {}
    for field in OBSERVATIONS:
        values = [s.get(field) for s in run["samples"]]
        try:
            result[field] = statistics([Decimal(number(v, field)) for v in values]) if values and all(v is not None for v in values) else None
        except (InvalidData, ValueError, TypeError, ArithmeticError):
            result[field] = None
    return result


def validate(run):
    reasons = []
    wall, factors = [], []
    samples, workload = run["samples"], run["workload"]
    if run["status"] != "completed":
        return {"status": "unavailable", "reasons": ["run_" + run["status"]], "sample_total": len(samples)}
    try:
        if len(samples) != 5 or {number(s.get("sample"), "sample") for s in samples} != set(range(1, 6)):
            reasons.append("five_complete_batches_required")
        if workload.get("scene_id") != "r4snes.active-scene.v2" or workload.get("work_version") != "snes-fixed-guest-v2":
            reasons.append("unknown_work_contract")
        if workload.get("enhancement") != "none" or workload.get("power_on") != "instance-1-semantic-ipl-no-save-v1":
            reasons.append("unknown_power_on_or_enhancement")
        if workload.get("input_contract") != "no-events-v1" or workload.get("input_sha256") != hashlib.sha256(b"no-events-v1").hexdigest():
            reasons.append("unknown_input_contract")
        for key in ("rom_sha256", "initial_state_sha256"):
            if not digest(workload.get(key)):
                reasons.append("missing_" + key)
        if number(workload.get("warmup_guest_ns"), "warmup") != 250000000 or number(workload.get("measured_guest_ns"), "duration") != 3000000000 or number(workload.get("sample_count"), "sample_count") != 5:
            reasons.append("work_amount_mismatch")
        if not run["artifacts"] or not run["environment"].get("execution") or not run["environment"].get("conditions"):
            reasons.append("missing_execution_or_artifact_context")
        for index, sample in enumerate(samples):
            prefix = f"sample_{index+1}:"
            if sample.get("status") != "ok" or sample.get("qualification_only") is not False or sample.get("cleanup_verified") is not True:
                reasons.append(prefix + "not_qualified_measurement")
            if sample.get("instrumented") is not (run["instrumentation_mode"] == "detail"):
                reasons.append(prefix + "instrumentation_mismatch")
            if sample.get("scene") != workload.get("scene_id") or sample.get("region") != run["region"] or sample.get("rom_sha256") != workload.get("rom_sha256"):
                reasons.append(prefix + "scene_mismatch")
            if sample.get("initial_state_sha256") != workload.get("initial_state_sha256"):
                reasons.append(prefix + "initial_state_mismatch")
            for key in ("audio_failures", "dsp_dropped_frames"):
                if key in sample and number(sample[key], key):
                    reasons.append(prefix + key)
            warmup = object_value(sample.get("warmup"), "warmup")
            if warmup.get("status") != "ok" or number(warmup.get("guest_ns"), "warmup.guest_ns") != 250000000 or number(warmup.get("wall_ns"), "warmup.wall_ns") <= 0:
                reasons.append(prefix + "warmup_missing")
            start = number(sample.get("work_start_master"), "work_start_master")
            end = number(sample.get("work_end_master"), "work_end_master")
            hz = number(sample.get("master_hz"), "master_hz")
            elapsed = number(sample.get("wall_ns"), "wall_ns")
            if end <= start or not hz or not elapsed:
                reasons.append(prefix + "no_completed_work")
                continue
            if number(warmup.get("master_cycles"), "warmup.master_cycles") != start:
                reasons.append(prefix + "warmup_boundary_mismatch")
            overshoot = number(sample.get("maximum_operation_overshoot", "0"), "overshoot")
            if abs((end-start)*1000000000 - 3000000000*hz) > (2*overshoot+16)*1000000000:
                reasons.append(prefix + "measured_work_mismatch")
            if number(sample.get("pending_master_cycles"), "pending") or number(sample.get("profile_clock_regressions"), "clock_regressions"):
                reasons.append(prefix + "unfinished_work_or_clock_regression")
            if number(sample.get("generated_frames"), "generated_frames") < 100 or not number(sample.get("apu_native_frames"), "apu_native_frames"):
                reasons.append(prefix + "inactive_scene")
            if sample.get("phase_time_is_cpu_accounting") is not False or sample.get("phase_order") != PHASES:
                reasons.append(prefix + "phase_contract_mismatch")
            if run["instrumentation_mode"] == "normal" and sample.get("sampled_exclusive_wall_ns") is not None:
                reasons.append(prefix + "disabled_timings_not_null")
            if run["instrumentation_mode"] == "detail":
                spans = sample.get("sampled_exclusive_wall_ns")
                if not isinstance(spans, list) or len(spans) != 6:
                    reasons.append(prefix + "missing_phase_spans")
                else:
                    for span in spans:
                        number(span, "phase_span")
            for key in ("state_sha256", "frame_sha256", "initial_state_sha256"):
                if not digest(sample.get(key)):
                    reasons.append(prefix + "missing_" + key)
            if run["profile"] == "core-throughput" and not digest(sample.get("pcm_sha256")):
                reasons.append(prefix + "missing_pcm_digest")
            wall.append(Decimal(elapsed))
            with localcontext() as context:
                context.prec = 28
                factors.append(Decimal(end-start)*1000000000/Decimal(hz)/Decimal(elapsed))
        for key in ("work_start_master", "work_end_master", "master_hz", "initial_state_sha256", "state_sha256", "frame_sha256", "pcm_sha256"):
            if len({canonical(s.get(key)) for s in samples}) != 1:
                reasons.append("fixed_batches_differ:" + key)
    except (InvalidData, ValueError, TypeError, ArithmeticError) as error:
        reasons.append("invalid_sample:" + str(error))
    computed = {"wall_ns": statistics(wall), "realtime_factor": statistics(factors)}
    for metric, values in computed.items():
        if values is None:
            continue
        reported = run["summary"].get(metric)
        if not isinstance(reported, dict):
            reasons.append("missing_summary:" + metric)
            continue
        for key, value in values.items():
            try:
                difference = abs(Decimal(reported[key])-Decimal(value))
                if difference > (Decimal(0) if metric == "wall_ns" else Decimal("1e-20")):
                    reasons.append("summary_mismatch:" + metric + "." + key)
            except (KeyError, ValueError, TypeError, ArithmeticError):
                reasons.append("invalid_summary:" + metric + "." + key)
    return {"status": "invalid" if reasons else "valid", "reasons": reasons,
            "sample_total": len(samples), "computed_summary": computed}


def compare(run, reference):
    reasons = []
    if validate(run)["status"] != "valid" or validate(reference)["status"] != "valid":
        reasons.append("invalid_or_incomplete_run")
    for key in ("benchmark_family", "subsystem_id", "profile", "region", "instrumentation_mode", "workload"):
        if canonical(run[key]) != canonical(reference[key]):
            reasons.append("different_" + key)
    if canonical(run["environment"].get("conditions")) != canonical(reference["environment"].get("conditions")):
        reasons.append("different_measurement_conditions")
    if run["environment"].get("execution") != reference["environment"].get("execution"):
        reasons.append("different_execution_profile")
    metrics = {}
    if not reasons:
        current, previous = validate(run)["computed_summary"], validate(reference)["computed_summary"]
        for metric in ("wall_ns", "realtime_factor"):
            a, b = Decimal(current[metric]["median_value"]), Decimal(previous[metric]["median_value"])
            if b <= 0:
                reasons.append("invalid_reference_value")
                continue
            metrics[metric] = {"value": format(a, "f"), "reference_value": format(b, "f"),
                               "ratio": format(a/b, "f"), "change_percent": format((a/b-1)*100, "f"),
                               "direction": "lower_is_better" if metric == "wall_ns" else "higher_is_better"}
    return {"run_id": run["run_id"], "reference_run_id": reference["run_id"],
            "compatible": not reasons, "reasons": reasons, "metrics": metrics,
            "different_hosts": run["host_id"] != reference["host_id"], "score": None}
