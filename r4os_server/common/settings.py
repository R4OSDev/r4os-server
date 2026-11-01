"""Administrative views of shared registration; used by the feature API adapter."""
import json
from urllib.parse import urlsplit
from .http import HTTPError


def dispatch(handler, policy):
    policy.require_role(handler, ("admin",))
    path = urlsplit(handler.path).path
    store = policy.store
    if handler.command in ("GET", "HEAD") and path == "/api/settings/state":
        return 200, {"allowed": store.list_allowed(), "blocked": store.list_blocked(),
                     "audit": store.list_audit(), "rates": store.settings(),
                     "network": policy.config.public()}
    if handler.command not in ("POST", "PUT"):
        raise HTTPError(405,"method_not_allowed")
    try:
        data = json.loads(handler.body())
    except (ValueError, UnicodeError):
        raise HTTPError(422,"validation_error","Valid JSON is required") from None
    if not isinstance(data,dict):
        raise HTTPError(422,"validation_error")
    if path == "/api/settings/unblock" and handler.command == "POST":
        changed=store.unblock(data.get("ip", ""),"WebApp administrator")
    elif path == "/api/settings/revoke" and handler.command == "POST":
        changed=store.revoke(data.get("ip", ""))
    elif path == "/api/settings/block" and handler.command == "POST":
        duration=data.get("duration_seconds", policy.config.block_seconds)
        if type(duration) is not int or not 1 <= duration <= 86400*30:
            raise HTTPError(422,"validation_error","Invalid block duration")
        store.block_ip(data.get("ip", ""),"Administrator",duration)
        changed=True
    elif path == "/api/settings/rates" and handler.command == "PUT":
        if not data or set(data) - {"updates","benchmarks","software","registration"}:
            raise HTTPError(422,"validation_error","Unknown services")
        if any(type(value) is not int or not 1 <= value <= 10000 for value in data.values()):
            raise HTTPError(422,"validation_error","Rate must be between 1 and 10000")
        for service,value in data.items():
            store.set_rate(service,value)
        return 200,{"rates":store.settings()}
    else:
        raise HTTPError(404,"not_found")
    return 200,{"changed":changed}
