"""Versioned, lossless import contract. No R4OS files are read here."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
from uuid import UUID

AREAS = {
    "scheduler_sync": "Scheduler and synchronisation",
    "memory": "Memory management",
    "ipc": "Kernel communication",
    "lifecycle": "Program lifecycle",
    "filesystem_io": "File and I/O core",
}
SCORE_AREAS = (*AREAS, "overall")
RUN_STATUSES = ("completed", "failed", "incomplete", "cancelled", "running")
MEASUREMENT_STATUSES = ("ok", "failed", "incomplete", "skipped")
SCORE_STATUSES = ("valid", "invalid", "pending", "unavailable", "unscored")
VERSION_FIELDS = ("release_version", "kernel_version", "spectra_version", "suite_version", "scoring_version")
MAX_U64 = 2**64 - 1
SAFE_INTEGER = 2**53 - 1


class InvalidData(ValueError):
    def __init__(self, path: str, message: str):
        self.path = path
        self.message = message
        super().__init__(f"{path}: {message}")


def text(value, path: str, *, nullable=False, maximum=256):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise InvalidData(path, f"Non-empty text with at most {maximum} characters required.")
    return value


def enum(value, choices, path: str):
    if value not in tuple(choices):
        raise InvalidData(path, "Erlaubte Werte: " + ", ".join(str(item) for item in choices))
    return value


def uuid(value, path: str, *, nullable=False):
    if value is None and nullable:
        return None
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", value):
        raise InvalidData(path, "A UUID in 8-4-4-4-12 format is required.")
    return str(UUID(value))


def host_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", value):
        raise InvalidData("host_id", "Use 1 to 64 lowercase letters, digits, hyphens or underscores.")
    return value


def integer(value, path: str, *, nullable=True):
    if value is None and nullable:
        return None
    if type(value) is int:
        number = value
    elif isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,19}", value):
        number = int(value)
    else:
        raise InvalidData(path, "A non-negative integer or decimal integer string is required.")
    if not 0 <= number <= MAX_U64:
        raise InvalidData(path, "Value is outside uint64 range.")
    return str(number)


def decimal(value, path: str, *, nullable=True, nonnegative=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise InvalidData(path, "A finite number or decimal string is required.")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise InvalidData(path, "Invalid decimal number.") from None
    if not number.is_finite() or (number and abs(number.adjusted()) > 300):
        raise InvalidData(path, "A finite number with an exponent between -300 and 300 is required.")
    if nonnegative and number < 0:
        raise InvalidData(path, "Value must be non-negative.")
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return "0" if number == 0 else result


def timestamp(value, path: str, *, nullable=True):
    if value is None and nullable:
        return None
    if not isinstance(value, str):
        raise InvalidData(path, "A UTC timestamp is required.")
    match = re.fullmatch(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?(?:Z|\+00:00)", value)
    if match is None:
        raise InvalidData(path, "An ISO 8601 UTC timestamp with Z or +00:00 is required.")
    try:
        datetime.fromisoformat(match[1] + "+00:00")
    except ValueError:
        raise InvalidData(path, "Invalid UTC timestamp.") from None
    return match[1] + "." + (match[2] or "").ljust(9, "0") + "Z"


def now():
    return timestamp(datetime.now(timezone.utc).isoformat(timespec="microseconds"), "time", nullable=False)


def json_value(value, path="document", depth=0):
    if depth > 32:
        raise InvalidData(path, "JSON structure is nested too deeply.")
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return str(value) if abs(value) > SAFE_INTEGER else value
    if isinstance(value, (float, Decimal)):
        return decimal(value, path, nullable=False)
    if isinstance(value, list):
        return [json_value(item, path, depth + 1) for item in value]
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise InvalidData(path, "JSON object keys must be text.")
        return {key: json_value(item, path + "." + key, depth + 1) for key, item in value.items()}
    raise InvalidData(path, "Invalid JSON value.")


def object_value(value, path):
    if not isinstance(value, dict):
        raise InvalidData(path, "A JSON object is required.")
    return json_value(value, path)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def loads(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise InvalidData("document", "Duplicate JSON key: " + key)
            result[key] = value
        return result

    def constant(_value):
        raise InvalidData("document", "NaN and Infinity are not allowed.")

    try:
        return json.loads(data, parse_float=Decimal, parse_constant=constant, object_pairs_hook=pairs)
    except InvalidData:
        raise
    except (ValueError, UnicodeError, RecursionError):
        raise InvalidData("document", "Invalid UTF-8 JSON.") from None


def normalize_host(identifier, document):
    identifier = host_id(identifier)
    if not isinstance(document, dict):
        raise InvalidData("host", "A JSON object is required.")
    data = document
    unknown = set(data) - {"host_id", "display_name", "description", "hardware", "metadata", "tags"}
    if unknown:
        raise InvalidData("host", "Unknown fields: " + ", ".join(sorted(unknown)))
    if "host_id" in data and data["host_id"] != identifier:
        raise InvalidData("host_id", "Path and record identify different hosts.")
    tags = data.get("tags", [])
    if not isinstance(tags, list) or len(tags) > 32:
        raise InvalidData("tags", "A list of at most 32 tags is required.")
    hardware = object_value(data.get("hardware", {}), "hardware")
    for key in ("logical_cpus", "ram_bytes"):
        if key in hardware:
            hardware[key] = integer(data["hardware"][key], "hardware." + key)
    return {
        "host_id": identifier,
        "display_name": text(data.get("display_name"), "display_name", maximum=128),
        "description": text(data.get("description"), "description", nullable=True, maximum=4096),
        "hardware": hardware,
        "metadata": object_value(data.get("metadata", {}), "metadata"),
        "tags": sorted(set(text(tag, "tags", maximum=64) for tag in tags)),
    }


def normalize_run(identifier, document):
    identifier = uuid(identifier, "run_id")
    if not isinstance(document, dict):
        raise InvalidData("document", "A JSON object is required.")
    data = document
    if uuid(data.get("run_id"), "run_id") != identifier:
        raise InvalidData("run_id", "Path and record identify different runs.")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise InvalidData("schema_version", "Supported schema version: 1.")
    run = {
        "run_id": identifier,
        "schema_version": 1,
        "host_id": host_id(data.get("host_id")),
        "started_at_utc": timestamp(data.get("started_at_utc"), "started_at_utc"),
        "finished_at_utc": timestamp(data.get("finished_at_utc"), "finished_at_utc"),
        "status": enum(data.get("status"), RUN_STATUSES, "status"),
        "failure_reason": text(data.get("failure_reason"), "failure_reason", nullable=True, maximum=8192),
        **{key: text(data.get(key), key, nullable=True, maximum=128) for key in VERSION_FIELDS},
        "baseline_run_id": uuid(data.get("baseline_run_id"), "baseline_run_id", nullable=True),
        "environment_id": text(data.get("environment_id"), "environment_id", maximum=128),
        "comparison_key": text(data.get("comparison_key"), "comparison_key", nullable=True),
        "instrumentation_mode": text(data.get("instrumentation_mode"), "instrumentation_mode", nullable=True, maximum=128),
        "notes": text(data.get("notes"), "notes", nullable=True, maximum=65536),
        "environment": object_value(data.get("environment", {}), "environment"),
    }
    if run["status"] == "completed" and (run["started_at_utc"] is None or run["finished_at_utc"] is None):
        raise InvalidData("status", "Completed runs require start and end timestamps.")
    if run["started_at_utc"] and run["finished_at_utc"] and run["finished_at_utc"] < run["started_at_utc"]:
        raise InvalidData("finished_at_utc", "End time precedes start time.")
    environment = run["environment"]
    for key in ("vcpus", "ram_bytes", "warmup_runs", "repetitions"):
        if key in environment:
            environment[key] = integer(data["environment"][key], "environment." + key)
    for key in ("cpu", "host", "qemu", "workloads"):
        if key in environment and environment[key] is not None:
            environment[key] = object_value(environment[key], "environment." + key)
    if "storage" in environment and environment["storage"] is not None and not isinstance(environment["storage"], (dict, list)):
        raise InvalidData("environment.storage", "A structured object or list is required.")
    artifacts = data.get("artifacts", [])
    if not isinstance(artifacts, list) or len(artifacts) > 1000:
        raise InvalidData("artifacts", "A list of at most 1000 artefacts is required.")
    run["artifacts"] = [object_value(item, "artifacts") for item in artifacts]

    def records(key, maximum):
        values = data.get(key, [])
        if not isinstance(values, list) or len(values) > maximum:
            raise InvalidData(key, f"A list of at most {maximum} entries required.")
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise InvalidData(f"{key}[{index}]", "A JSON object is required.")
            if "run_id" in value and uuid(value["run_id"], key + ".run_id") != identifier:
                raise InvalidData(key + ".run_id", "The child record belongs to a different run.")
            fields = {
                "metrics": {"run_id", "area_id", "metric_id", "metric_version", "unit", "direction", "primary", "weight", "status", "failure_reason", "sample_count", "median_value", "min_value", "max_value", "mad_value", "checks"},
                "samples": {"run_id", "area_id", "metric_id", "sample_index", "phase", "status", "failure_reason", "elapsed_ns", "operation_count", "bytes_processed", "value", "clock_source", "clock_generation", "clock_resolution_ns", "observer_overhead_ns", "worker_count", "completed_workers"},
                "scores": {"run_id", "area_id", "baseline_run_id", "scoring_version", "score", "status", "reason"},
            }
            if set(value) - fields[key]:
                raise InvalidData(key, "Unknown fields: " + ", ".join(sorted(set(value) - fields[key])))
        return values

    metrics = []
    metric_keys = set()
    for index, item in enumerate(records("metrics", 1000)):
        path = f"metrics[{index}]"
        area = enum(item.get("area_id"), AREAS, path + ".area_id")
        metric = text(item.get("metric_id"), path + ".metric_id", maximum=128)
        key = (area, metric)
        if key in metric_keys:
            raise InvalidData(path, "Duplicate metric in the same area.")
        metric_keys.add(key)
        primary = item.get("primary", False)
        if type(primary) is not bool:
            raise InvalidData(path + ".primary", "A boolean is required.")
        direction = item.get("direction")
        direction = {"lower": "lower_is_better", "higher": "higher_is_better"}.get(direction, direction) if isinstance(direction, str) else direction
        result = {
            "run_id": identifier, "area_id": area, "metric_id": metric,
            "metric_version": text(item.get("metric_version"), path + ".metric_version", nullable=True, maximum=128),
            "unit": text(item.get("unit"), path + ".unit", nullable=True, maximum=64),
            "direction": enum(direction, ("lower_is_better", "higher_is_better", None), path + ".direction"),
            "primary": primary,
            "weight": decimal(item.get("weight"), path + ".weight", nonnegative=True),
            "status": enum(item.get("status"), MEASUREMENT_STATUSES, path + ".status"),
            "failure_reason": text(item.get("failure_reason"), path + ".failure_reason", nullable=True, maximum=8192),
            "sample_count": integer(item.get("sample_count"), path + ".sample_count"),
            "checks": object_value(item["checks"], path + ".checks") if item.get("checks") is not None else None,
        }
        for key in ("median_value", "min_value", "max_value", "mad_value"):
            result[key] = decimal(item.get(key), path + "." + key, nonnegative=True)
        bounds = [result[key] for key in ("min_value", "median_value", "max_value")]
        if all(value is not None for value in bounds) and not Decimal(bounds[0]) <= Decimal(bounds[1]) <= Decimal(bounds[2]):
            raise InvalidData(path, "min_value <= median_value <= max_value is required.")
        metrics.append(result)
    run["metrics"] = sorted(metrics, key=lambda item: (SCORE_AREAS.index(item["area_id"]), item["metric_id"]))

    samples = []
    sample_keys = set()
    for index, item in enumerate(records("samples", 100000)):
        path = f"samples[{index}]"
        area = enum(item.get("area_id"), AREAS, path + ".area_id")
        metric = text(item.get("metric_id"), path + ".metric_id", maximum=128)
        if (area, metric) not in metric_keys:
            raise InvalidData(path, "A raw sample requires its metric in the same run.")
        sample_index = integer(item.get("sample_index"), path + ".sample_index", nullable=False)
        phase = enum(item.get("phase"), ("warmup", "measurement"), path + ".phase")
        key = (area, metric, phase, sample_index)
        if key in sample_keys:
            raise InvalidData(path, "Duplicate raw sample identity.")
        sample_keys.add(key)
        result = {
            "run_id": identifier, "area_id": area, "metric_id": metric,
            "sample_index": sample_index, "phase": phase,
            "status": enum(item.get("status"), MEASUREMENT_STATUSES, path + ".status"),
            "failure_reason": text(item.get("failure_reason"), path + ".failure_reason", nullable=True, maximum=8192),
            "value": decimal(item.get("value"), path + ".value", nonnegative=True),
            "clock_source": text(item.get("clock_source"), path + ".clock_source", nullable=True, maximum=128),
        }
        for key in ("elapsed_ns", "operation_count", "bytes_processed", "clock_generation", "clock_resolution_ns", "observer_overhead_ns"):
            result[key] = integer(item.get(key), path + "." + key)
        for key in ("worker_count", "completed_workers"):
            if key in item:
                result[key] = integer(item[key], path + "." + key, nullable=False)
        if run["suite_version"] == "spectra-standard-3" and result["status"] == "ok":
            cpus = environment.get("vcpus")
            if cpus is None or int(cpus) < 1 or result.get("worker_count") != cpus or result.get("completed_workers") != cpus:
                raise InvalidData(path, "Suite 3 braucht die bestaetigte Arbeit aller verfuegbaren CPUs.")
        samples.append(result)
    run["samples"] = sorted(samples, key=lambda item: (
        SCORE_AREAS.index(item["area_id"]), item["metric_id"], item["phase"], int(item["sample_index"])))

    scores = []
    score_keys = set()
    for index, item in enumerate(records("scores", 6)):
        path = f"scores[{index}]"
        area = enum(item.get("area_id"), SCORE_AREAS, path + ".area_id")
        if area in score_keys:
            raise InvalidData(path, "Duplicate area score.")
        score_keys.add(area)
        scores.append({
            "run_id": identifier, "area_id": area,
            "baseline_run_id": uuid(item.get("baseline_run_id"), path + ".baseline_run_id", nullable=True),
            "scoring_version": text(item.get("scoring_version"), path + ".scoring_version", nullable=True, maximum=128),
            "score": decimal(item.get("score"), path + ".score", nonnegative=True),
            "status": enum(item.get("status"), SCORE_STATUSES, path + ".status"),
            "reason": text(item.get("reason"), path + ".reason", nullable=True, maximum=8192),
        })
    run["scores"] = sorted(scores, key=lambda item: SCORE_AREAS.index(item["area_id"]))
    known_fields = set(run)
    unknown = set(data) - known_fields
    if unknown:
        raise InvalidData("document", "Unknown fields for schema 1: " + ", ".join(sorted(unknown)))
    return run
