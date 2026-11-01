"""JSON API with shared HTTP behavior for direct access and the web proxy."""

import hmac
import logging
import re
import sqlite3
from urllib.parse import parse_qs, urlsplit

from . import NAME, VERSION
from .models import InvalidData, normalize_host, normalize_run
from .store import Conflict, NotFound, Store
from .subsystems import normalize as normalize_subsystem

LOGGER = logging.getLogger(__name__)


class Application:
    def __init__(self, config, *, token=None, store=None):
        self.config = config
        self.token = (token if token is not None else config.benchmark_token).encode("utf-8")
        self.store = store if store is not None else Store(config.data_dir / "benchmarks/benchmarks.sqlite3")

    def authorized(self, header):
        parts = header.split() if isinstance(header, str) else []
        return len(parts) == 2 and parts[0].lower() == "bearer" and hmac.compare_digest(parts[1].encode("utf-8"), self.token)

    @staticmethod
    def query(url):
        values = parse_qs(urlsplit(url).query, keep_blank_values=True, max_num_fields=32)
        if any(len(items) != 1 for items in values.values()):
            raise InvalidData("query", "Filters must not be specified more than once.")
        return {key: items[0] for key, items in values.items()}

    def dispatch(self, method, url, document=None):
        path = urlsplit(url).path.rstrip("/")
        try:
            query = self.query(url)
            if method in ("GET", "HEAD"):
                if path == "/api/health":
                    if query:
                        raise InvalidData("query", "Health accepts no filters.")
                    return 200, {"service": NAME, "version": VERSION, "status": "ok", "schema_version": 1,
                                 "ports": {"web": self.config.web_port, "api": self.config.benchmarks_port, "registration": self.config.registration_port}}
                if path == "/api/v1/hosts":
                    if query:
                        raise InvalidData("query", "The host list accepts no filters.")
                    return 200, self.store.hosts()
                if path == "/api/v1/benchmark-filters":
                    if query:
                        raise InvalidData("query", "The filter inventory accepts no query parameters.")
                    return 200, self.store.filters()
                if path == "/api/v1/benchmark-runs":
                    return 200, self.store.runs(query)
                if path == "/api/v1/subsystem-benchmark-runs":
                    return 200, self.store.subsystem_runs(query)
                match = re.fullmatch(r"/api/v1/subsystem-benchmark-runs/([^/]+)/comparison", path)
                if match:
                    if set(query) != {"reference_run_id"}:
                        raise InvalidData("query", "reference_run_id is required.")
                    return 200, self.store.subsystem_comparison(match[1], query["reference_run_id"])
                match = re.fullmatch(r"/api/v1/subsystem-benchmark-runs/([^/]+)/samples", path)
                if match:
                    return 200, self.store.subsystem_samples(match[1], query)
                match = re.fullmatch(r"/api/v1/subsystem-benchmark-runs/([^/]+)", path)
                if match:
                    if query:
                        raise InvalidData("query", "Run details accept no filters.")
                    return 200, self.store.subsystem_run(match[1])
                match = re.fullmatch(r"/api/v1/hosts/([^/]+)", path)
                if match:
                    if query:
                        raise InvalidData("query", "Host details accept no filters.")
                    return 200, self.store.hosts(match[1])
                match = re.fullmatch(r"/api/v1/benchmark-runs/([^/]+)/samples", path)
                if match:
                    return 200, self.store.samples(match[1], query)
                match = re.fullmatch(r"/api/v1/benchmark-runs/([^/]+)", path)
                if match:
                    if set(query) - {"include_samples"} or query.get("include_samples", "true") not in ("true", "false"):
                        raise InvalidData("query", "include_samples must be true or false.")
                    return 200, self.store.run(match[1], include_samples=query.get("include_samples", "true") == "true")
            elif method == "PUT":
                if query:
                    raise InvalidData("query", "PUT accepts no query parameters.")
                match = re.fullmatch(r"/api/v1/hosts/([^/]+)", path)
                if match:
                    created, host = self.store.put_host(normalize_host(match[1], document))
                    return 201 if created else 200, {"host_id": host["host_id"], "stored": True, "created": created, "host": host}
                match = re.fullmatch(r"/api/v1/benchmark-runs/([^/]+)", path)
                if match:
                    run = normalize_run(match[1], document)
                    created, validation = self.store.put_run(run)
                    return 201 if created else 200, {"run_id": run["run_id"], "stored": True, "duplicate": not created, "score_validation": validation}
                match = re.fullmatch(r"/api/v1/subsystem-benchmark-runs/([^/]+)", path)
                if match:
                    run = normalize_subsystem(match[1], document)
                    created, validation = self.store.put_subsystem_run(run)
                    return 201 if created else 200, {"run_id": run["run_id"], "stored": True,
                                                   "duplicate": not created, "validation": validation}
            elif method not in ("GET", "HEAD", "PUT"):
                return 405, {"error": "method_not_allowed"}
            return 404, {"error": "not_found"}
        except InvalidData as error:
            return 422, {"error": "validation_error", "path": error.path, "detail": error.message}
        except NotFound as error:
            return 404, {"error": str(error)}
        except Conflict as error:
            return 409, {"error": str(error), "stored": False, "duplicate": False}
        except sqlite3.OperationalError:
            LOGGER.exception("DevCenter database operation failed")
            return 503, {"error": "database_unavailable"}
        except (ValueError, OverflowError) as error:
            return 422, {"error": "validation_error", "detail": str(error)}
