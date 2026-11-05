"""Public TLS first-installation admission, with no administrative role."""
import ssl

from ..common.http import HTTPError

PATH = "/api/v1/registration/bootstrap"
CONTENT_TYPE = "application/vnd.r4os.registration-v1"


def issue(handler):
    if not getattr(handler.server, "public_listener", False) or not isinstance(handler.connection, ssl.SSLSocket):
        raise HTTPError(404, "not_found")
    if handler.command != "POST":
        raise HTTPError(405, "method_not_allowed")
    if handler.body_length(0) != 0:
        raise HTTPError(400, "empty_body_required")
    policy = handler.server.policy
    ip = handler.source_ip()
    if policy.store.is_blocked(ip):
        raise HTTPError(403, "source_blocked")
    if not handler.server.bootstrap_limiter.allow(ip):
        raise HTTPError(429, "rate_limit")
    token = policy.store.issue_client(ip)
    if token is None:
        raise HTTPError(403 if policy.store.is_blocked(ip) else 429, "bootstrap_denied")
    document = ("SCHEMA=R4OS_PUBLIC_REGISTRATION\nVERSION=1\n"
                "SCOPE=REGISTRATION_READ\nREGISTRATION_SECRET=" + token + "\n").encode("ascii")
    return handler.reply(201, document, CONTENT_TYPE)
