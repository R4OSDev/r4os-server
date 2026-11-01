"""Explicit source networks, bounded connections/bodies and TLS transport."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import logging
import ssl
import threading
from urllib.parse import urlsplit

from .config import LAN_NETWORKS


class HTTPError(Exception):
    def __init__(self, status, error, detail=None):
        self.status = status
        self.body = {"error": error}
        if detail:
            self.body["detail"] = detail


class LANMixin:
    allowed_networks = LAN_NETWORKS

    def verify_request(self, request, client_address):
        try:
            address = ipaddress.ip_address(client_address[0])
            return any(address in network for network in self.allowed_networks)
        except ValueError:
            return False


    def get_request(self):
        request,address=super().get_request()
        timeout=getattr(getattr(self,'config',None),'request_timeout',30)
        request.settimeout(min(timeout,5) if getattr(self,'public_listener',False) else timeout)
        if getattr(self,'tls_context',None):
            request=self.tls_context.wrap_socket(request,server_side=True,do_handshake_on_connect=False)
        return request,address

    def process_request(self, request, address):
        slots = getattr(self, 'connection_slots', None)
        if slots is not None and not slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            if slots is not None:
                slots.release()
            raise

    def process_request_thread(self,request,address):
        try:
            if isinstance(request,ssl.SSLSocket):
                request.do_handshake()
                request.settimeout(getattr(getattr(self,'config',None),'request_timeout',30))
            super().process_request_thread(request,address)
        except OSError:
            self.shutdown_request(request)
        finally:
            slots = getattr(self, 'connection_slots', None)
            if slots is not None:
                slots.release()


class LANHTTPServer(LANMixin, ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def configure_server(server, config, *, tls=True, public=False):
    server.config = config
    server.public_listener = public
    server.allowed_networks = tuple(ipaddress.ip_network(n) for n in (config.public_api_networks if public else config.allowed_networks))
    if public:
        if not tls or not config.tls_cert:
            raise ValueError('Public listeners require TLS.')
        server.connection_slots = threading.BoundedSemaphore(64)
    if tls and config.tls_cert:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(config.tls_cert, config.tls_key)
        server.tls_context = context
    return server


class Handler(BaseHTTPRequestHandler):
    server_version = "R4OS-Server"
    sys_version = ""
    protocol_version = "HTTP/1.0"

    def send_response(self, code, message=None):
        self.response_status = code
        return super().send_response(code, message)

    def setup(self):
        super().setup()
        self.connection.settimeout(getattr(getattr(self.server, "config", None), "request_timeout", 30))

    def source_ip(self):
        peer = self.client_address[0]
        trusted = getattr(getattr(self.server, "config", None), "trusted_proxies", ())
        if peer in trusted and self.headers.get("X-Real-IP"):
            try:
                return str(ipaddress.ip_address(self.headers["X-Real-IP"]))
            except ValueError:
                raise HTTPError(400, "invalid_source_ip") from None
        return str(ipaddress.ip_address(peer))

    def reply(self, status, body, content_type="application/json; charset=utf-8", headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, status, value, headers=None):
        self.reply(status, (json.dumps(value, ensure_ascii=False, allow_nan=False)+"\n").encode(), headers=headers)

    def body_length(self, maximum, content_type=None):
        values = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") is not None or len(values) != 1 or not values[0].isascii() or not values[0].isdigit():
            raise HTTPError(411, "content_length_required")
        if len(values[0]) > 12 or int(values[0]) > maximum:
            raise HTTPError(413, "payload_too_large")
        if content_type and self.headers.get_content_type() != content_type:
            raise HTTPError(415, "unsupported_media_type", content_type + " required")
        return int(values[0])

    def body(self, maximum=1024*1024, content_type="application/json"):
        length = self.body_length(maximum, content_type)
        try:
            data = self.rfile.read(length)
        except (TimeoutError, OSError):
            raise HTTPError(408, "request_timeout") from None
        if len(data) != length:
            raise HTTPError(400, "incomplete_body")
        return data

    def log_message(self, fmt, *args):
        logging.info("%s %s %s %s", self.client_address[0], self.command, urlsplit(self.path).path, getattr(self,"response_status","-"))

    def do_HEAD(self):
        self.do_GET()

    def unsupported(self):
        self.json(405, {"error": "method_not_allowed"})

    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = unsupported
