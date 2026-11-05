from __future__ import annotations

import hmac
import logging
import socket
import socketserver

from .state import StateStore, normalize_ip


from ..common.http import LANMixin
from ..common.rate_limit import PerIpRateLimiter


class AuthenticationServer(LANMixin, socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        store: StateStore,
        magic_number: str,
        block_seconds: int,
    ):
        self.store = store
        self.magic_number = magic_number.encode("ascii")
        self.block_seconds = block_seconds
        self.rate_limiter = PerIpRateLimiter(lambda: store.get_rate("registration"))
        self.logger = logging.getLogger("r4os_server.registration")
        super().__init__(address, AuthenticationHandler)


class AuthenticationHandler(socketserver.BaseRequestHandler):
    server: AuthenticationServer

    def handle(self) -> None:
        source_ip = normalize_ip(self.client_address[0])
        if self.server.store.is_blocked(source_ip):
            return
        if not self.server.rate_limiter.allow(source_ip):
            return
        self.request.settimeout(5.0)
        received = bytearray()
        try:
            while len(received) < 259:
                chunk = self.request.recv(259 - len(received))
                if not chunk:
                    break
                received.extend(chunk)
                candidate = bytes(received)
                if b"\n" in candidate:
                    break
                if len(candidate) == len(self.server.magic_number) and hmac.compare_digest(candidate, self.server.magic_number):
                    break
                # Both the original unframed secret and v1 installation
                # tokens have known lengths. Prefix fragments must not be
                # mistaken for a short legacy credential.
                if candidate.startswith(b"R4C1_"):
                    if len(candidate) >= 69:
                        if len(candidate) < len(self.server.magic_number) and self.server.magic_number.startswith(candidate):
                            continue
                        break
                elif not b"R4C1_".startswith(candidate) and len(candidate) >= len(self.server.magic_number):
                    break
        except (TimeoutError, socket.timeout, OSError):
            pass
        candidate = bytes(received)
        if candidate.endswith(b"\r\n"):
            candidate = candidate[:-2]
        elif candidate.endswith(b"\n"):
            candidate = candidate[:-1]
        legacy = len(candidate) == len(self.server.magic_number) and hmac.compare_digest(candidate, self.server.magic_number)
        accepted = (self.server.store.register_magic(source_ip) if legacy else
                    bool(getattr(self.server, "public_listener", False)) and self.server.store.register_client(source_ip, candidate))
        if accepted:
            try:
                self.request.sendall(b"OK")
                # Keep the successful half of the connection open until
                # the client consumes OK and closes. R4OS currently
                # reports a payload immediately followed by peer FIN as
                # peer_closed, so closing here could hide a valid reply.
                self.request.settimeout(1.0)
                try:
                    self.request.recv(1)
                except (TimeoutError, socket.timeout, OSError):
                    pass
            except OSError:
                pass
            return
        self.server.store.block_ip(source_ip, "invalid-magic-number", self.server.block_seconds)
