"""Shared client admission; network registration does not confer write roles."""
from .auth import role
from .http import HTTPError
from .rate_limit import PerIpRateLimiter
from ..registration.state import AccessDecision


class AccessPolicy:
    def __init__(self, config, store, service):
        self.config, self.store, self.service = config, store, service
        self.limiter = PerIpRateLimiter(lambda: store.get_rate(service))

    def require_role(self, handler, accepted):
        selected = role(self.config, handler.headers.get("Authorization"))
        if selected is None:
            raise HTTPError(401,"unauthorized")
        if selected not in accepted:
            raise HTTPError(403,"forbidden")
        return selected

    def client(self, handler, *, write=False):
        selected = role(self.config, handler.headers.get("Authorization"))
        if selected == "admin":
            return selected
        if write:
            selected = self.require_role(handler, ("benchmark",))
        ip = handler.source_ip()
        if not self.limiter.allow(ip):
            raise HTTPError(429,"rate_limit")
        decision = self.store.record_gateway_attempt(ip,self.config.attempt_limit,self.config.block_seconds,service=self.service)
        if decision != AccessDecision.ALLOWED:
            raise HTTPError(403 if decision == AccessDecision.BLOCKED else 401, "registration_required",
                            "LAST CHANCE!" if decision == AccessDecision.LAST_CHANCE else ("Blocked!" if decision == AccessDecision.BLOCKED else "STOP NOW!"))
        return selected
