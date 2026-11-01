from __future__ import annotations

from ..common.http import LANHTTPServer
import logging
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from .catalog import Catalog
from .http_common import UpdateRequestHandler
from ..common.rate_limit import PerIpRateLimiter
from ..registration.state import AccessDecision, StateStore


RANGE_PATTERN = re.compile(r"^bytes=([0-9]*)-([0-9]*)$")


class UpdateGatewayServer(LANHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        store: StateStore,
        catalog: Catalog,
        attempt_limit: int,
        block_seconds: int,
        public_port: int,
    ):
        self.store = store
        self.catalog = catalog
        self.attempt_limit = attempt_limit
        self.block_seconds = block_seconds
        self.public_port = public_port
        self.rate_limiter = PerIpRateLimiter(store.gateway_requests_per_second_per_ip)
        self.logger = logging.getLogger("r4os_update_server.gateway")
        super().__init__(address, UpdateGatewayHandler)


class UpdateGatewayHandler(UpdateRequestHandler):
    server: UpdateGatewayServer

    def do_GET(self) -> None:
        self._handle(False)

    def do_HEAD(self) -> None:
        self._handle(True)

    def do_POST(self) -> None:
        if not self._authorized(False):
            return
        self.send_text(405, "Method Not Allowed\n")

    def _authorized(self, head_only: bool) -> bool:
        if hasattr(self.server, "policy"):
            self.server.policy.client(self)
            return True
        source_ip = self.client_source_ip()
        if not self.server.rate_limiter.allow(source_ip):
            self.send_text(429, "Too Many Requests!\n", head_only)
            return False
        decision = self.server.store.record_gateway_attempt(
            source_ip,
            self.server.attempt_limit,
            self.server.block_seconds,
        )
        if decision == AccessDecision.ALLOWED:
            return True
        if decision == AccessDecision.STOP_NOW:
            self.send_text(401, "STOP NOW!\n", head_only)
            return False
        if decision == AccessDecision.LAST_CHANCE:
            self.send_text(401, "LAST CHANCE!\n", head_only)
            return False
        self.send_text(403, "Blocked!\n", head_only)
        return False

    def _handle(self, head_only: bool) -> None:
        if not self._authorized(head_only):
            return
        path = urlsplit(self.path).path
        if path in {"/", "/catalog", "/catalog.json"}:
            self.send_json(200, self.server.catalog.public_catalog(self.server.public_port), head_only)
            return
        if path.startswith("/packages/"):
            filename = unquote(path[len("/packages/") :])
            try:
                released = self.server.catalog.released_entry(filename)
            except ValueError:
                released = None
            if released is None:
                self.send_text(404, "Not Found\n", head_only)
                return
            _, package_path = released
            self._send_package(package_path, head_only)
            return
        self.send_text(404, "Not Found\n", head_only)

    def _send_package(self, package_path: Path, head_only: bool) -> None:
        size = package_path.stat().st_size
        start = 0
        end = size - 1
        status = 200
        requested_range = self.headers.get("Range")
        if requested_range:
            match = RANGE_PATTERN.fullmatch(requested_range.strip())
            if match is None:
                self._range_not_satisfiable(size, head_only)
                return
            left, right = match.groups()
            if not left and not right:
                self._range_not_satisfiable(size, head_only)
                return
            if left:
                start = int(left)
                end = int(right) if right else size - 1
            else:
                suffix = int(right)
                if suffix < 1:
                    self._range_not_satisfiable(size, head_only)
                    return
                start = max(0, size - suffix)
                end = size - 1
            if start >= size or end < start:
                self._range_not_satisfiable(size, head_only)
                return
            end = min(end, size - 1)
            status = 206
        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Disposition", f'attachment; filename="{package_path.name}"')
        self.send_header("Cache-Control", "private, no-transform")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if head_only:
            return
        try:
            with package_path.open("rb") as source:
                source.seek(start)
                remaining = length
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _range_not_satisfiable(self, size: int, head_only: bool) -> None:
        body = b"Range Not Satisfiable\n"
        self.send_response(416)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Range", f"bytes */{size}")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not head_only:
            self.wfile.write(body)
