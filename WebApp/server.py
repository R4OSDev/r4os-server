#!/usr/bin/env python3
"""LAN-Webserver für das R4OS-Server-Grundgerüst, ohne externe Pakete."""

from __future__ import annotations

import ipaddress
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parent.parent
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
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.ico": ("favicon.ico", "image/x-icon"),
    "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
}


class WebAppServer(ThreadingHTTPServer):
    allowed_networks: tuple[ipaddress.IPv4Network, ...]

    def verify_request(self, request, client_address):
        address = ipaddress.ip_address(client_address[0])
        return any(address in network for network in self.allowed_networks)


class WebAppHandler(BaseHTTPRequestHandler):
    server: WebAppServer
    server_version = "R4OS-Server"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def do_GET(self):
        self.respond(send_body=True)

    def do_HEAD(self):
        self.respond(send_body=False)

    def respond(self, *, send_body):
        path = urlsplit(self.path).path
        if path == "/api/status":
            content = json.dumps(
                {"name": "R4OS Server", "status": "ok", "version": VERSION}
            ).encode("utf-8")
            self.send_content(200, "application/json; charset=utf-8", content, send_body)
            return

        asset = STATIC_ROUTES.get(path)
        if asset is None:
            self.send_content(
                404,
                "application/json; charset=utf-8",
                b'{"error":"Nicht gefunden"}',
                send_body,
            )
            return

        filename, content_type = asset
        try:
            content = (STATIC_ROOT / filename).read_bytes()
        except OSError:
            logging.exception("WebApp-Datei konnte nicht gelesen werden: %s", filename)
            self.send_content(
                500,
                "application/json; charset=utf-8",
                b'{"error":"WebApp-Datei nicht verfuegbar"}',
                send_body,
            )
            return
        self.send_content(200, content_type, content, send_body)

    def send_content(self, status, content_type, content, send_body):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
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
        logging.info("%s %s", self.client_address[0], message % arguments)


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    host = os.environ.get("R4OS_WEBAPP_HOST", "127.0.0.1")
    try:
        address = ipaddress.IPv4Address(host)
        if not any(address in network for network in LAN_NETWORKS):
            raise ValueError("Die Bind-Adresse muss eine LAN- oder Loopback-Adresse sein.")
        port = int(os.environ.get("R4OS_WEBAPP_PORT", "4020"))
        if not 1 <= port <= 65535:
            raise ValueError("Ungültiger Port.")
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
                raise ValueError("Erlaubte Netze müssen auf LAN und Loopback begrenzt sein.")
        with WebAppServer((host, port), WebAppHandler) as server:
            server.allowed_networks = networks
            logging.info("R4OS Server %s startet auf http://%s:%s", VERSION, host, port)
            server.serve_forever()
    except (ValueError, OSError) as error:
        logging.error("WebApp konnte nicht gestartet werden: %s", error)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
