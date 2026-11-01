"""SQLite persistence: immutable imports, mutable host names, transactional children."""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
from pathlib import Path
import sqlite3

from .models import AREAS, InvalidData, VERSION_FIELDS, canonical, fingerprint, host_id, now, timestamp, uuid
from .scoring import evaluate
from .subsystems import (SCHEMA as SUBSYSTEM_SCHEMA, validate as validate_subsystem,
                         compare as compare_subsystems, observations as subsystem_observations)

SCHEMA = """
CREATE TABLE IF NOT EXISTS hosts (
    host_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, document_json TEXT NOT NULL,
    created_at_utc TEXT NOT NULL, updated_at_utc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS host_identities (
    host_id TEXT PRIMARY KEY REFERENCES hosts(host_id),
    method TEXT NOT NULL CHECK(method='smbios-system-uuid-sha256-v1'),
    identity_hash TEXT NOT NULL CHECK(length(identity_hash)=64 AND identity_hash NOT GLOB '*[^0-9a-f]*'),
    created_at_utc TEXT NOT NULL,
    UNIQUE(method, identity_hash)
);
CREATE TRIGGER IF NOT EXISTS host_identity_no_update BEFORE UPDATE ON host_identities
BEGIN SELECT RAISE(ABORT, 'host_identity_immutable'); END;
CREATE TRIGGER IF NOT EXISTS host_identity_no_delete BEFORE DELETE ON host_identities
BEGIN SELECT RAISE(ABORT, 'host_identity_immutable'); END;
CREATE TABLE IF NOT EXISTS environments (
    snapshot_id TEXT PRIMARY KEY, environment_id TEXT NOT NULL, host_id TEXT NOT NULL REFERENCES hosts(host_id),
    document_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS benchmark_runs (
    run_id TEXT PRIMARY KEY, schema_version INTEGER NOT NULL, host_id TEXT NOT NULL REFERENCES hosts(host_id),
    environment_id TEXT NOT NULL, environment_snapshot_id TEXT NOT NULL REFERENCES environments(snapshot_id),
    started_at_utc TEXT, finished_at_utc TEXT, status TEXT NOT NULL, failure_reason TEXT,
    release_version TEXT, kernel_version TEXT, spectra_version TEXT, suite_version TEXT, scoring_version TEXT,
    baseline_run_id TEXT, comparison_key TEXT, instrumentation_mode TEXT, notes TEXT,
    content_hash TEXT NOT NULL, payload_json TEXT NOT NULL, scoring_input_json TEXT NOT NULL,
    host_snapshot_json TEXT NOT NULL, imported_at_utc TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS runs_date ON benchmark_runs(started_at_utc DESC, run_id);
CREATE INDEX IF NOT EXISTS runs_host_environment ON benchmark_runs(host_id, environment_id);
CREATE INDEX IF NOT EXISTS runs_versions ON benchmark_runs(release_version, kernel_version, spectra_version);
CREATE INDEX IF NOT EXISTS runs_baseline ON benchmark_runs(baseline_run_id);
CREATE TABLE IF NOT EXISTS artifacts (
    run_id TEXT NOT NULL REFERENCES benchmark_runs(run_id), ordinal INTEGER NOT NULL, document_json TEXT NOT NULL,
    PRIMARY KEY (run_id, ordinal)
);
CREATE TABLE IF NOT EXISTS metrics (
    run_id TEXT NOT NULL REFERENCES benchmark_runs(run_id), area_id TEXT NOT NULL, metric_id TEXT NOT NULL,
    metric_version TEXT, unit TEXT, direction TEXT, is_primary INTEGER NOT NULL, weight TEXT, status TEXT NOT NULL,
    failure_reason TEXT, sample_count TEXT, median_value TEXT, min_value TEXT, max_value TEXT, mad_value TEXT,
    checks_json TEXT NOT NULL, PRIMARY KEY (run_id, area_id, metric_id)
);
CREATE TABLE IF NOT EXISTS samples (
    run_id TEXT NOT NULL, area_id TEXT NOT NULL, metric_id TEXT NOT NULL, ordinal INTEGER NOT NULL,
    sample_index TEXT NOT NULL, phase TEXT NOT NULL, status TEXT NOT NULL, failure_reason TEXT,
    elapsed_ns TEXT, operation_count TEXT, bytes_processed TEXT, value TEXT, clock_source TEXT,
    clock_generation TEXT, clock_resolution_ns TEXT, observer_overhead_ns TEXT,
    document_json TEXT NOT NULL,
    FOREIGN KEY (run_id, area_id, metric_id) REFERENCES metrics(run_id, area_id, metric_id),
    PRIMARY KEY (run_id, area_id, metric_id, phase, sample_index)
);
CREATE INDEX IF NOT EXISTS samples_page ON samples(run_id, area_id, metric_id, ordinal);
CREATE TABLE IF NOT EXISTS scores (
    run_id TEXT NOT NULL REFERENCES benchmark_runs(run_id), area_id TEXT NOT NULL, baseline_run_id TEXT,
    scoring_version TEXT, score TEXT, status TEXT NOT NULL, reason TEXT, PRIMARY KEY (run_id, area_id)
);
"""


class Conflict(Exception):
    pass


class HostClaimConflict(Conflict):
    pass


class NotFound(Exception):
    pass


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1, 2, 3, 4):
                raise ValueError("Unsupported DevCenter database version.")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + SUBSYSTEM_SCHEMA)
            columns={row['name'] for row in connection.execute('PRAGMA table_info(subsystem_runs)')}
            if 'summary_json' not in columns:
                connection.execute('ALTER TABLE subsystem_runs ADD COLUMN summary_json TEXT')
            for row in connection.execute('SELECT run_id,payload_json FROM subsystem_runs WHERE summary_json IS NULL'):
                connection.execute('UPDATE subsystem_runs SET summary_json=? WHERE run_id=?',
                                   (self._subsystem_summary(json.loads(row['payload_json'])),row['run_id']))
            connection.execute('PRAGMA user_version=4')
            connection.commit()

    @staticmethod
    def _subsystem_summary(run):
        summary={key:value for key,value in run.items() if key!='samples'}
        summary.update(validation=validate_subsystem(run),observations=subsystem_observations(run),sample_total=len(run['samples']))
        return canonical(summary)

    @contextmanager
    def connection(self, *, write=False):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            yield connection
            if write:
                connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _host(row):
        document = json.loads(row["document_json"])
        document.update(created_at_utc=row["created_at_utc"], updated_at_utc=row["updated_at_utc"])
        if "run_count" in row.keys():
            document["run_count"] = row["run_count"]
        return document

    def put_host(self, host):
        with self.connection(write=True) as connection:
            row = connection.execute("SELECT * FROM hosts WHERE host_id=?", (host["host_id"],)).fetchone()
            created = row is None
            moment = now()
            if created:
                connection.execute("INSERT INTO hosts VALUES (?, ?, ?, ?, ?)", (
                    host["host_id"], host["display_name"], canonical(host), moment, moment))
            elif row["document_json"] != canonical(host):
                connection.execute("UPDATE hosts SET display_name=?, document_json=?, updated_at_utc=? WHERE host_id=?", (
                    host["display_name"], canonical(host), moment, host["host_id"]))
            row = connection.execute("SELECT * FROM hosts WHERE host_id=?", (host["host_id"],)).fetchone()
        return created, self._host(row)

    def claim_host(self, host, identity):
        identifier = host["host_id"]
        with self.connection(write=True) as connection:
            row = connection.execute("SELECT * FROM hosts WHERE host_id=?", (identifier,)).fetchone()
            if row is not None:
                binding = connection.execute("SELECT method, identity_hash FROM host_identities WHERE host_id=?", (identifier,)).fetchone()
                if binding is None:
                    raise HostClaimConflict("host_id_in_use")
                if (binding["method"], binding["identity_hash"]) != (identity["method"], identity["hash"]):
                    raise HostClaimConflict("host_identity_conflict")
                return False, self._host(row)
            occupied = connection.execute("SELECT 1 FROM host_identities WHERE method=? AND identity_hash=?",
                                          (identity["method"], identity["hash"])).fetchone()
            if occupied is not None:
                raise HostClaimConflict("host_identity_in_use")
            moment = now()
            connection.execute("INSERT INTO hosts VALUES (?, ?, ?, ?, ?)",
                               (identifier, host["display_name"], canonical(host), moment, moment))
            connection.execute("INSERT INTO host_identities VALUES (?, ?, ?, ?)",
                               (identifier, identity["method"], identity["hash"], moment))
            row = connection.execute("SELECT * FROM hosts WHERE host_id=?", (identifier,)).fetchone()
        return True, self._host(row)

    def hosts(self, identifier=None):
        parameters = []
        where = ""
        if identifier is not None:
            where = "WHERE hosts.host_id=?"
            parameters.append(host_id(identifier))
        with self.connection() as connection:
            rows = connection.execute(
                "SELECT hosts.*, ((SELECT count(*) FROM benchmark_runs r WHERE r.host_id=hosts.host_id) + "
                "(SELECT count(*) FROM subsystem_runs s WHERE s.host_id=hosts.host_id)) run_count "
                "FROM hosts " + where + " ORDER BY display_name, host_id", parameters).fetchall()
        if identifier is not None:
            if not rows:
                raise NotFound("host_not_found")
            return self._host(rows[0])
        return {"items": [self._host(row) for row in rows], "total": len(rows)}

    @staticmethod
    def _validation(connection, run):
        if run["baseline_run_id"] == run["run_id"]:
            baseline = run
        else:
            row = connection.execute("SELECT scoring_input_json FROM benchmark_runs WHERE run_id=?", (run["baseline_run_id"],)).fetchone()
            baseline = json.loads(row[0]) if row else None
        return evaluate(run, baseline)

    def put_run(self, run):
        payload = canonical(run)
        digest = fingerprint(run)
        scoring_input = {key: value for key, value in run.items() if key not in ("samples", "artifacts")}
        with self.connection(write=True) as connection:
            existing = connection.execute("SELECT content_hash FROM benchmark_runs WHERE run_id=?", (run["run_id"],)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise Conflict("run_id_content_conflict")
                return False, self._validation(connection, scoring_input)
            host = connection.execute("SELECT * FROM hosts WHERE host_id=?", (run["host_id"],)).fetchone()
            if host is None:
                raise InvalidData("host_id", "Create the host first with PUT /api/v1/hosts/{host_id}.")
            snapshot = fingerprint({"host_id": run["host_id"], "environment_id": run["environment_id"], "environment": run["environment"]})
            connection.execute("INSERT OR IGNORE INTO environments VALUES (?, ?, ?, ?)", (
                snapshot, run["environment_id"], run["host_id"], canonical(run["environment"])))
            fields = ("run_id", "schema_version", "host_id", "environment_id", "started_at_utc", "finished_at_utc", "status", "failure_reason",
                      *VERSION_FIELDS, "baseline_run_id", "comparison_key", "instrumentation_mode", "notes")
            values = [run[key] for key in fields]
            fields += ("environment_snapshot_id", "content_hash", "payload_json", "scoring_input_json", "host_snapshot_json", "imported_at_utc")
            values += [snapshot, digest, payload, canonical(scoring_input), canonical(self._host(host)), now()]
            connection.execute("INSERT INTO benchmark_runs (" + ",".join(fields) + ") VALUES (" + ",".join("?" for _ in fields) + ")", values)
            connection.executemany("INSERT INTO artifacts VALUES (?, ?, ?)", (
                (run["run_id"], index, canonical(artifact)) for index, artifact in enumerate(run["artifacts"])))
            metric_fields = ("run_id", "area_id", "metric_id", "metric_version", "unit", "direction", "primary", "weight", "status",
                             "failure_reason", "sample_count", "median_value", "min_value", "max_value", "mad_value")
            connection.executemany("INSERT INTO metrics VALUES (" + ",".join("?" for _ in range(16)) + ")", (
                tuple(int(item[key]) if key == "primary" else item[key] for key in metric_fields) + (canonical(item["checks"]),)
                for item in run["metrics"]))
            sample_fields = ("sample_index", "phase", "status", "failure_reason", "elapsed_ns", "operation_count", "bytes_processed",
                             "value", "clock_source", "clock_generation", "clock_resolution_ns", "observer_overhead_ns")
            connection.executemany("INSERT INTO samples VALUES (" + ",".join("?" for _ in range(17)) + ")", (
                (run["run_id"], item["area_id"], item["metric_id"], index) + tuple(item[key] for key in sample_fields) + (canonical(item),)
                for index, item in enumerate(run["samples"])))
            score_fields = ("run_id", "area_id", "baseline_run_id", "scoring_version", "score", "status", "reason")
            connection.executemany("INSERT INTO scores VALUES (?, ?, ?, ?, ?, ?, ?)", (
                tuple(item[key] for key in score_fields) for item in run["scores"]))
            validation = self._validation(connection, scoring_input)
        return True, validation

    def run(self, identifier, *, include_samples=True):
        identifier = uuid(identifier, "run_id")
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM benchmark_runs WHERE run_id=?", (identifier,)).fetchone()
            if row is None:
                raise NotFound("run_not_found")
            data = json.loads(row["payload_json"] if include_samples else row["scoring_input_json"])
            if not include_samples:
                data["artifacts"] = [json.loads(item[0]) for item in connection.execute(
                    "SELECT document_json FROM artifacts WHERE run_id=? ORDER BY ordinal", (identifier,))]
            data["host_snapshot"] = json.loads(row["host_snapshot_json"])
            data["imported_at_utc"] = row["imported_at_utc"]
            data["score_validation"] = self._validation(connection, data)
            data["sample_total"] = connection.execute("SELECT count(*) FROM samples WHERE run_id=?", (identifier,)).fetchone()[0]
        return data

    def put_subsystem_run(self, run):
        digest, payload = fingerprint(run), canonical(run)
        with self.connection(write=True) as connection:
            existing = connection.execute("SELECT content_hash FROM subsystem_runs WHERE run_id=?", (run["run_id"],)).fetchone()
            if existing:
                if existing[0] != digest:
                    raise Conflict("run_id_content_conflict")
                return False, validate_subsystem(run)
            host = connection.execute("SELECT * FROM hosts WHERE host_id=?", (run["host_id"],)).fetchone()
            if host is None:
                raise InvalidData("host_id", "Create the host first with PUT /api/v1/hosts/{host_id}.")
            fields = ("run_id", "schema_version", "benchmark_family", "subsystem_id", "host_id",
                      "started_at_utc", "finished_at_utc", "status", "profile", "region", "instrumentation_mode")
            values = [run[key] for key in fields]
            fields += ("scene_id", "rom_sha256", "emulator_version", "release_version", "backend", "content_hash",
                       "payload_json", "host_snapshot_json", "imported_at_utc", "summary_json")
            values += [run["workload"].get("scene_id"), run["workload"].get("rom_sha256"),
                       run["versions"].get("emulator_version"), run["versions"].get("release_version"),
                       run["environment"].get("backend"), digest, payload, canonical(self._host(host)), now(),self._subsystem_summary(run)]
            connection.execute("INSERT INTO subsystem_runs (" + ",".join(fields) + ") VALUES (" +
                               ",".join("?" for _ in fields) + ")", values)
        return True, validate_subsystem(run)

    def subsystem_run(self, identifier):
        identifier = uuid(identifier, "run_id")
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM subsystem_runs WHERE run_id=?", (identifier,)).fetchone()
        if row is None:
            raise NotFound("subsystem_run_not_found")
        data = json.loads(row["payload_json"])
        data.update(host_snapshot=json.loads(row["host_snapshot_json"]), imported_at_utc=row["imported_at_utc"],
                    sample_total=len(data["samples"]), validation=validate_subsystem(data))
        return data

    def subsystem_runs(self, query):
        allowed = {"host_id", "status", "profile", "region", "instrumentation_mode", "scene_id", "rom_sha256",
                   "emulator_version", "release_version", "backend", "date_from", "date_to", "limit", "offset"}
        if set(query) - allowed:
            raise InvalidData("query", "Unknown subsystem filter.")
        limit, offset = self.pagination(query)
        clauses, parameters = [], []
        for key, value in query.items():
            if key in ("limit", "offset") or not value:
                continue
            if key in ("date_from", "date_to"):
                from .models import timestamp
                # Same UTC day semantics as the existing dashboard filters.
                day = timestamp(value + "T00:00:00Z", key)
                if key == "date_from":
                    clauses.append("started_at_utc>=?")
                    parameters.append(day)
                else:
                    from datetime import datetime, timezone, timedelta
                    end = datetime.fromisoformat(value).replace(tzinfo=timezone.utc) + timedelta(days=1)
                    clauses.append("started_at_utc<?")
                    parameters.append(timestamp(end.isoformat(), key))
            else:
                clauses.append(key + "=?")
                parameters.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as connection:
            total = connection.execute("SELECT count(*) FROM subsystem_runs" + where, parameters).fetchone()[0]
            rows = connection.execute("SELECT summary_json FROM subsystem_runs" + where +
                                      " ORDER BY started_at_utc DESC,run_id LIMIT ? OFFSET ?", parameters + [limit, offset]).fetchall()
        items = []
        for row in rows:
            data = json.loads(row["summary_json"])
            items.append(data)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def subsystem_comparison(self, identifier, reference):
        current, previous = self.subsystem_run(identifier), self.subsystem_run(reference)
        return compare_subsystems(current, previous)

    def subsystem_samples(self, identifier, query):
        if set(query) - {"limit", "offset"}:
            raise InvalidData("query", "Unknown raw data filter.")
        limit, offset = self.pagination(query)
        data = self.subsystem_run(identifier)
        return {"run_id": data["run_id"], "items": data["samples"][offset:offset+limit],
                "total": len(data["samples"]), "limit": limit, "offset": offset}

    @staticmethod
    def pagination(query, maximum=200):
        try:
            limit, offset = int(query.get("limit", "50")), int(query.get("offset", "0"))
        except (TypeError, ValueError):
            raise InvalidData("pagination", "limit and offset must be integers.") from None
        if not 1 <= limit <= maximum or not 0 <= offset <= 2**31 - 1:
            raise InvalidData("pagination", f"limit 1..{maximum}, offset 0..2147483647 required.")
        return limit, offset

    @staticmethod
    def _filters(query):
        fields = ("host_id", "environment_id", *VERSION_FIELDS, "status", "comparison_key", "baseline_run_id", "instrumentation_mode")
        allowed = (*fields, "date_from", "date_to", "limit", "offset", "order")
        if set(query) - set(allowed):
            raise InvalidData("query", "Unknown filter: " + ", ".join(sorted(set(query) - set(allowed))))
        conditions, values = [], []
        for key in fields:
            if query.get(key):
                value = query[key]
                if key == "host_id":
                    value = host_id(value)
                if key == "baseline_run_id":
                    value = uuid(value, key)
                conditions.append("r." + key + "=?")
                values.append(value)
        for key, operator in (("date_from", ">="), ("date_to", "<=")):
            if query.get(key):
                value = query[key]
                if len(value) == 10:
                    try:
                        day = datetime.strptime(value, "%Y-%m-%d")
                    except ValueError:
                        raise InvalidData(key, "Invalid date.") from None
                    if key == "date_to":
                        day += timedelta(days=1)
                        operator = "<"
                    value = day.strftime("%Y-%m-%dT00:00:00Z")
                conditions.append("r.started_at_utc " + operator + " ?")
                values.append(timestamp(value, key, nullable=False))
        order = query.get("order", "desc")
        if order not in ("asc", "desc"):
            raise InvalidData("order", "asc or desc is required.")
        return (" WHERE " + " AND ".join(conditions) if conditions else ""), values, order.upper()

    def runs(self, query):
        limit, offset = self.pagination(query)
        where, parameters, order = self._filters(query)
        with self.connection() as connection:
            total = connection.execute("SELECT count(*) FROM benchmark_runs r" + where, parameters).fetchone()[0]
            rows = connection.execute(
                "SELECT r.scoring_input_json, r.imported_at_utc, h.display_name FROM benchmark_runs r JOIN hosts h ON h.host_id=r.host_id"
                + where + f" ORDER BY r.started_at_utc {order}, r.run_id {order} LIMIT ? OFFSET ?", (*parameters, limit, offset)).fetchall()
            items = []
            for row in rows:
                run = json.loads(row["scoring_input_json"])
                item = {key: value for key, value in run.items() if key not in ("environment", "metrics", "scores")}
                item.update(host_name=row["display_name"], metric_count=len(run["metrics"]), imported_at_utc=row["imported_at_utc"],
                            score_validation=self._validation(connection, run))
                items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def samples(self, identifier, query):
        identifier = uuid(identifier, "run_id")
        allowed = ("area_id", "metric_id", "phase", "limit", "offset")
        if set(query) - set(allowed):
            raise InvalidData("query", "Unknown raw sample filter.")
        limit, offset = self.pagination(query, maximum=500)
        conditions, values = ["run_id=?"], [identifier]
        for key in ("area_id", "metric_id", "phase"):
            if query.get(key):
                conditions.append(key + "=?")
                values.append(query[key])
        where = " WHERE " + " AND ".join(conditions)
        with self.connection() as connection:
            if not connection.execute("SELECT 1 FROM benchmark_runs WHERE run_id=?", (identifier,)).fetchone():
                raise NotFound("run_not_found")
            total = connection.execute("SELECT count(*) FROM samples" + where, values).fetchone()[0]
            rows = connection.execute("SELECT document_json FROM samples" + where + " ORDER BY ordinal LIMIT ? OFFSET ?", (*values, limit, offset)).fetchall()
        return {"items": [json.loads(row[0]) for row in rows], "total": total, "limit": limit, "offset": offset}

    def filters(self):
        with self.connection() as connection:
            versions = {key: [row[0] for row in connection.execute(
                "SELECT DISTINCT " + key + " FROM benchmark_runs WHERE " + key + " IS NOT NULL ORDER BY " + key + " DESC")]
                for key in VERSION_FIELDS}
            environments = [dict(row) for row in connection.execute(
                "SELECT environment_id, count(*) run_count, count(DISTINCT host_id) host_count FROM benchmark_runs GROUP BY environment_id ORDER BY environment_id")]
            baselines = [dict(row) for row in connection.execute(
                "SELECT r.baseline_run_id, count(*) run_count, b.host_id, h.display_name host_name, b.release_version "
                "FROM benchmark_runs r LEFT JOIN benchmark_runs b ON b.run_id=r.baseline_run_id "
                "LEFT JOIN hosts h ON h.host_id=b.host_id WHERE r.baseline_run_id IS NOT NULL GROUP BY r.baseline_run_id ORDER BY max(r.started_at_utc) DESC")]
        return {"versions": versions, "environments": environments, "baselines": baselines, "areas": AREAS}
