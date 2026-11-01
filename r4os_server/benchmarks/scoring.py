"""Scores are derived from immutable measurements, never from sent indices."""

from decimal import Decimal, localcontext

from .models import AREAS, SCORE_AREAS, canonical

METHOD_FIELDS = ("execution", "vcpus", "ram_bytes", "qemu", "storage", "workloads", "cache_state", "warmup_runs", "repetitions")
ALL_CPU_METHOD_FIELDS = ("cpu_policy", "measurement", "filesystem", "workloads", "cache_state", "warmup_runs", "warmup_min_ns", "warmup_min_batches", "repetitions", "settle_ms")


def number(value):
    if value != 0 and abs(value) < Decimal("0.000001"):
        return format(value, ".12g")
    return format(value, ".6f").rstrip("0").rstrip(".")


def checks_ok(value):
    if value is False:
        return False
    if isinstance(value, dict):
        if value.get("status") in ("failed", "invalid", "incomplete"):
            return False
        return all(checks_ok(item) for item in value.values())
    if isinstance(value, list):
        return all(checks_ok(item) for item in value)
    return True


def comparable(run, baseline):
    for key in ("comparison_key", "suite_version", "scoring_version", "instrumentation_mode"):
        if not run.get(key) or not baseline.get(key) or run[key] != baseline[key]:
            return False, "comparison_mismatch:" + key
    all_cpu = run.get("suite_version") == "spectra-standard-3"
    method_fields = ALL_CPU_METHOD_FIELDS if all_cpu else METHOD_FIELDS
    for side in (run, baseline):
        environment = side["environment"]
        for key in METHOD_FIELDS:
            if key not in environment or (environment[key] is None and not (key == "qemu" and environment.get("execution") == "native")):
                return False, "environment_incomplete:" + key
        if environment["execution"] not in ("native", "qemu"):
            return False, "environment_invalid:execution"
        for key in ("vcpus", "ram_bytes", "repetitions"):
            if int(environment[key]) <= 0:
                return False, "environment_invalid:" + key
        if environment["execution"] == "qemu" and (
            not isinstance(environment["qemu"], dict) or not environment["qemu"].get("version") or not environment["qemu"].get("backend")
        ):
            return False, "environment_incomplete:qemu"
        if not environment["workloads"] or not environment["storage"]:
            return False, "environment_incomplete:workloads_or_storage"
        if all_cpu:
            if any(metric["primary"] and metric["metric_version"] != "2" for metric in side["metrics"]):
                return False, "metric_contract_mismatch:suite3_primary_metric_version"
            if environment.get("cpu_policy") != "all-available-v1" or environment.get("measurement") != "parallel-wall-ns-per-operation-v3" or environment.get("filesystem") != "ntfs":
                return False, "environment_invalid:all_cpu_method"
            for key in method_fields:
                if environment.get(key) is None:
                    return False, "environment_incomplete:" + key
    # Suite 3 deliberately measures hardware/CPU-count differences using
    # equal work per CPU and aggregate elapsed time per completed operation.
    # Historical suites retain their original strict hardware-method checks.
    for key in method_fields:
        if canonical(run["environment"][key]) != canonical(baseline["environment"][key]):
            return False, "environment_mismatch:" + key
    return True, None


def usable_metric(metric):
    return (
        metric["status"] == "ok" and metric["metric_version"] is not None and metric["unit"] is not None
        and metric["direction"] in ("lower_is_better", "higher_is_better")
        and metric["weight"] is not None and Decimal(metric["weight"]) > 0
        and metric["sample_count"] is not None and int(metric["sample_count"]) > 0
        and metric["median_value"] is not None and Decimal(metric["median_value"]) > 0
        and metric["checks"] is not None and checks_ok(metric["checks"])
    )


def evaluate(run, baseline):
    reports = {score["area_id"]: score for score in run["scores"]}
    result = {
        "status": "invalid",
        "baseline_run_id": run["baseline_run_id"],
        "comparison": {"status": "not_comparable", "reason": None, "cross_host": False, "cpu_changed": False},
        "areas": {},
    }

    def entry(area, score=None, status="invalid", reason=None, ratios=None):
        reported = reports.get(area)
        matches = None
        if score is not None and reported and reported["score"] is not None:
            matches = (
                reported["status"] == "valid"
                and reported["baseline_run_id"] == run["baseline_run_id"]
                and reported["scoring_version"] == run["scoring_version"]
                and abs(score - Decimal(reported["score"])) <= max(Decimal("0.01"), abs(score) * Decimal("0.000001"))
            )
            if not matches:
                status, reason = "invalid", "reported_score_mismatch"
        return {
            "score": number(score) if score is not None else None,
            "status": status, "reason": reason,
            "reported_score": reported["score"] if reported else None,
            "reported_matches": matches,
            "metric_ratios": ratios or [],
        }

    def unavailable(status, reason):
        result["status"] = status
        result["comparison"]["status"] = "pending" if status == "pending" else "not_comparable"
        result["comparison"]["reason"] = reason
        result["areas"] = {area: entry(area, status=status, reason=reason) for area in SCORE_AREAS}
        return result

    if run["baseline_run_id"] is None:
        return unavailable("unscored", "no_baseline")
    if baseline is None:
        return unavailable("pending", "unknown_baseline")
    if baseline["status"] != "completed":
        return unavailable("invalid", "baseline_not_completed")
    valid, reason = comparable(run, baseline)
    result["comparison"]["cross_host"] = run["host_id"] != baseline["host_id"]
    result["comparison"]["cpu_changed"] = canonical(run["environment"].get("cpu")) != canonical(baseline["environment"].get("cpu"))
    if not valid:
        return unavailable("invalid", reason)
    result["comparison"].update(status="comparable", reason=None)
    exact_scores = {}
    with localcontext() as context:
        context.prec = 60
        for area in AREAS:
            current = {item["metric_id"]: item for item in run["metrics"] if item["area_id"] == area and item["primary"]}
            reference = {item["metric_id"]: item for item in baseline["metrics"] if item["area_id"] == area and item["primary"]}
            if not reference:
                result["areas"][area] = entry(area, reason="baseline_missing_primary_metrics")
                continue
            if current.keys() != reference.keys():
                result["areas"][area] = entry(area, reason="primary_metric_set_mismatch")
                continue
            weighted_log = Decimal(0)
            weights = Decimal(0)
            ratios = []
            failure = None
            for identifier, metric in current.items():
                base = reference[identifier]
                if any(metric[key] != base[key] for key in ("metric_version", "unit", "direction", "weight")):
                    failure = "metric_contract_mismatch:" + identifier
                    break
                if not usable_metric(base) or not usable_metric(metric):
                    failure = "invalid_primary_metric:" + identifier
                    break
                base_value, current_value = Decimal(base["median_value"]), Decimal(metric["median_value"])
                ratio = base_value / current_value if metric["direction"] == "lower_is_better" else current_value / base_value
                weight = Decimal(metric["weight"])
                weighted_log += ratio.ln() * weight
                weights += weight
                ratios.append({"metric_id": identifier, "ratio": number(ratio), "weight": metric["weight"]})
            if failure:
                result["areas"][area] = entry(area, reason=failure)
                continue
            score = Decimal(100) * (weighted_log / weights).exp()
            exact_scores[area] = score
            result["areas"][area] = entry(area, score, "valid", ratios=ratios)
        if run["status"] != "completed":
            result["areas"]["overall"] = entry("overall", reason="run_not_completed")
        elif not all(result["areas"][area]["status"] == "valid" for area in AREAS):
            result["areas"]["overall"] = entry("overall", reason="five_valid_areas_required")
        else:
            overall = Decimal(100) * (sum((score / Decimal(100)).ln() for score in exact_scores.values()) / Decimal(5)).exp()
            result["areas"]["overall"] = entry("overall", overall, "valid")
        if result["areas"]["overall"]["status"] == "valid":
            result["status"] = "valid"
    return result


def calculate_from_measurements(run, baseline):
    """Compute independently of the scores reported against the client's reference."""
    current = dict(run, baseline_run_id=baseline["run_id"], scores=[])
    result = evaluate(current, baseline)
    for area in result["areas"].values():
        del area["reported_score"]
        del area["reported_matches"]
    return result
