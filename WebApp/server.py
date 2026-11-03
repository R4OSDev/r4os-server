#!/usr/bin/env python3
"""LAN web server for R4OS Server without external dependencies."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import sys
import ssl
import socket
import time
from http.client import HTTPConnection, HTTPSConnection, HTTPException
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from r4os_server.common.config import Config
from r4os_server.common.http import Handler, HTTPError, LANHTTPServer
from r4os_server.workspace.store import Store as WorkspaceStore
STATIC_ROOT = Path(__file__).resolve().parent / "static"
VERSION = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip()
LAN_NETWORKS = tuple(
    ipaddress.ip_network(value)
    for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8")
)
STATIC_ROUTES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/updates.js": ("updates.js", "text/javascript; charset=utf-8"),
    "/settings.js": ("settings.js", "text/javascript; charset=utf-8"),
    "/benchmarks.js": ("benchmarks.js", "text/javascript; charset=utf-8"),
    "/benchmark-actions.js": ("benchmark-actions.js", "text/javascript; charset=utf-8"),
    "/repositories.js": ("repositories.js", "text/javascript; charset=utf-8"),
    "/connections.js": ("connections.js", "text/javascript; charset=utf-8"),
    "/apps.js": ("apps.js", "text/javascript; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}
ICON_PATHS = frozenset(("/favicon.svg", "/favicon.ico", "/apple-touch-icon.png"))


class WebAppServer(LANHTTPServer):
    allowed_networks: tuple[ipaddress.IPv4Network, ...]

    def verify_request(self, request, client_address):
        address = ipaddress.ip_address(client_address[0])
        return any(address in network for network in self.allowed_networks)


class LimitedReader:
    def __init__(self, source, length):
        self.source, self.remaining = source, length

    def read(self, size):
        if not self.remaining:
            return b""
        try:
            data = self.source.read(min(size, self.remaining))
        except TimeoutError:
            raise HTTPError(408, "request_timeout") from None
        if not data:
            raise HTTPError(400, "incomplete_body")
        self.remaining -= len(data)
        return data


class WebAppHandler(Handler):
    server: WebAppServer
    server_version = "R4OS-Server"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def do_GET(self):
        self.respond(send_body=True)

    def do_HEAD(self):
        self.respond(send_body=False)

    def respond(self, *, send_body):
        path = urlsplit(self.path).path
        if path == "/api/status":
            content = json.dumps(
                {"name": "R4OS Server", "status": "ok", "version": VERSION,
                 "services": self.health()}
            ).encode("utf-8")
            self.send_content(200, "application/json; charset=utf-8", content, send_body)
            return

        if path.startswith("/api/"):
            self.api_route()
            return

        asset = STATIC_ROUTES.get(path)
        if asset is None:
            self.send_content(
                404,
                "application/json; charset=utf-8",
                b'{"error":"Not found"}',
                send_body,
            )
            return

        filename, content_type = asset
        try:
            content = (STATIC_ROOT / filename).read_bytes()
        except OSError:
            logging.exception("WebApp file could not be read: %s", filename)
            self.send_content(
                500,
                "application/json; charset=utf-8",
                b'{"error":"WebApp file unavailable"}',
                send_body,
            )
            return
        cache_control = "private, max-age=86400" if path in ICON_PATHS else "no-store"
        self.send_content(200, content_type, content, send_body, cache_control=cache_control)

    def send_content(self, status, content_type, content, send_body, *, cache_control="no-store"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'self'",
        )
        self.end_headers()
        if send_body:
            self.wfile.write(content)

    def log_message(self, message, *arguments):
        logging.info("%s %s %s", self.client_address[0], self.command, urlsplit(self.path).path)

    def same_origin(self):
        origin = self.headers.get("Origin")
        if origin:
            try:
                parsed = urlsplit(origin)
            except ValueError:
                raise HTTPError(403, "origin_not_allowed") from None
            if parsed.scheme not in ("http", "https") or parsed.netloc.lower() != self.headers.get("Host", "").lower():
                raise HTTPError(403, "origin_not_allowed")
        elif self.headers.get("X-R4OS-Request") != "1":
            raise HTTPError(403, "origin_required")

    def api_route(self):
        try:
            path = urlsplit(self.path).path
            config = getattr(self.server, "config", None)
            if not config:
                raise HTTPError(503, "server_not_configured")
            try:
                host = urlsplit('//'+self.headers.get('Host','')).hostname
            except ValueError:
                host = None
            if host not in self.server.allowed_hosts:
                raise HTTPError(403, "host_not_allowed")
            if self.command not in ("GET", "HEAD"):
                self.same_origin()
            if path.startswith("/api/repositories/"):
                from r4os_server.workspace.api import dispatch
                return dispatch(self)
            if path.startswith("/api/apps/"):
                from r4os_server.workspace.apps import dispatch
                return dispatch(self)
            if path.startswith('/api/sdk/'):
                from r4os_server.workspace.sdk_api import dispatch
                return dispatch(self)
            if path.startswith(("/api/updates/", "/api/settings/", "/api/benchmarks/")):
                return self.proxy(config)
            raise HTTPError(404, "not_found")
        except HTTPError as error:
            self.json(error.status, error.body)
        except (ValueError, UnicodeError):
            self.json(422, {"error": "validation_error", "detail": "A valid request is required."})
        except (OSError, HTTPException):
            if not self.wfile.closed and not getattr(self,"proxy_started",False):
                try:
                    self.json(503, {"error": "backend_unavailable", "detail": "The service is currently unavailable."})
                except OSError:
                    pass

    do_POST = do_PUT = do_PATCH = do_DELETE = api_route

    def backend_connection(self, config, port, timeout=None):
        if config.tls_cert:
            return HTTPSConnection(config.host, port, timeout=timeout or config.request_timeout,
                                   context=self.server.backend_tls)
        return HTTPConnection(config.host, port, timeout=timeout or config.request_timeout)

    def proxy(self, config):
        benchmark = self.path.startswith("/api/benchmarks/")
        target = self.path.replace("/api/benchmarks/", "/api/v1/", 1) if benchmark else self.path
        port = config.benchmarks_port if benchmark else config.updates_port
        connection = self.backend_connection(config, port)
        try:
            headers = {"Authorization": "Bearer "+config.admin_token}
            body = None
            if self.command not in ("GET", "HEAD"):
                maximum = config.benchmark_limit if benchmark else (config.upload_limit if urlsplit(self.path).path == "/api/updates/uploads/package" else 1024*1024)
                length = self.body_length(maximum)
                headers.update({"Content-Type": self.headers.get("Content-Type", "application/json"), "Content-Length": str(length)})
                body = LimitedReader(self.rfile, length)
            connection.request(self.command, target, body=body, headers=headers)
            response = connection.getresponse()
            self.send_response(response.status)
            self.send_header("Content-Type", response.getheader("Content-Type", "application/json"))
            if response.getheader("Content-Length"):
                self.send_header("Content-Length", response.getheader("Content-Length"))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.proxy_started = True
            self.close_connection = True
            if self.command != "HEAD":
                while chunk := response.read(65536):
                    self.wfile.write(chunk)
        finally:
            connection.close()

    def health(self):
        config = getattr(self.server, "config", None)
        if config is None:
            return {}
        if time.monotonic()-self.server.health_time < 3:
            return self.server.health_value
        values = {}
        for name, port in (("updates", config.updates_port), ("benchmarks", config.benchmarks_port)):
            installed = (PROJECT_ROOT/"r4os_server"/name/"service.py").exists()
            if not installed:
                values[name] = "pending"
                continue
            connection = self.backend_connection(config, port, timeout=.7)
            try:
                connection.request("GET", "/api/health")
                response = connection.getresponse()
                values[name] = "ok" if response.status == 200 and json.loads(response.read(8192)).get("status") == "ok" else "error"
            except (OSError, ValueError, HTTPException):
                values[name] = "error"
            finally:
                connection.close()
        self.server.health_value = values
        self.server.health_time = time.monotonic()
        return values


def create_server(config, host=None, port=None):
    bind_host=host or config.host
    address=ipaddress.IPv4Address(bind_host)
    networks=tuple(ipaddress.IPv4Network(n) for n in config.allowed_networks)
    if not any(address in network for network in LAN_NETWORKS):
        raise ValueError('WebApp requires a specific LAN or loopback address.')
    if not networks or any(not any(network.subnet_of(lan) for lan in LAN_NETWORKS) for network in networks):
        raise ValueError('WebApp source networks must be limited to LAN/loopback.')
    server = WebAppServer((bind_host, config.web_port if port is None else port), WebAppHandler)
    server.config = config
    server.workspace = WorkspaceStore(config.workspace_dir or config.data_dir / "workspace", config.project_root or None)
    server.allowed_networks = networks
    server.allowed_hosts={bind_host,config.host,'localhost','127.0.0.1',socket.gethostname().lower(),socket.getfqdn().lower()}
    server.backend_tls = ssl.create_default_context(cafile=config.tls_ca or config.tls_cert) if config.tls_cert else None
    server.health_time = 0
    server.health_value = {}
    return server


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    host = os.environ.get("R4OS_WEBAPP_HOST", "127.0.0.1")
    try:
        address = ipaddress.IPv4Address(host)
        if not any(address in network for network in LAN_NETWORKS):
            raise ValueError("The bind address must be a LAN or loopback address.")
        port = int(os.environ.get("R4OS_WEBAPP_PORT", "4020"))
        if not 1 <= port <= 65535:
            raise ValueError("Invalid port.")
        configured = os.environ.get("R4OS_WEBAPP_ALLOWED_NETWORKS")
        networks = LAN_NETWORKS
        if configured is not None:
            networks = tuple(
                ipaddress.IPv4Network(value.strip()) for value in configured.split(",")
            )
            if not networks or any(
                not any(network.subnet_of(lan) for lan in LAN_NETWORKS)
                for network in networks
            ):
                raise ValueError("Allowed networks must be limited to LAN and loopback.")
        config_path = Path(os.environ.get("R4OS_SERVER_CONFIG", "/etc/r4os-server/server.json"))
        config = Config.load(config_path) if config_path.exists() else None
        server = create_server(config, host, port) if config else WebAppServer((host, port), WebAppHandler)
        with server:
            server.allowed_networks = networks
            logging.info("R4OS Server %s starting at http://%s:%s", VERSION, host, port)
            server.serve_forever()
    except (ValueError, OSError) as error:
        logging.error("WebApp could not be started: %s", error)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
