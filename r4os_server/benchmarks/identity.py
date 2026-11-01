"""Create-only host identity contract; raw hardware identifiers stay on the client.

Method v1 starts with the checked SMBIOS system UUID as 36 lowercase ASCII
characters in 8-4-4-4-12 form. Hash a domain, one NUL byte, then that text:
  host_id = 'hw1-' + SHA256('R4OS/benchmark/host-id/v1' + NUL + uuid).hex()[:60]
  hash = SHA256('R4OS/benchmark/host-identity/v1' + NUL + uuid).hex()
Hex is lowercase. The independent hashes detect conflicting bindings without
transmitting the UUID. The server validates their formats, not hardware ownership
or their derivation from a common UUID; registration and write roles still apply.
"""

import re

from .models import InvalidData, normalize_host


IDENTITY_METHOD = "smbios-system-uuid-sha256-v1"
CLAIM_ROUTE = re.compile(r"/api/v1/hosts/([^/]+)/claim")
CLAIM_HOST_ID = re.compile(r"hw1-[0-9a-f]{60}")
CLAIM_HASH = re.compile(r"[0-9a-f]{64}")
CLAIM_MAX_BYTES = 4096


def normalize_claim(identifier, document):
    if not isinstance(identifier, str) or CLAIM_HOST_ID.fullmatch(identifier) is None:
        raise InvalidData("host_id", "Expected hw1- followed by 60 lowercase hexadecimal digits.")
    if not isinstance(document, dict) or set(document) != {"display_name", "identity"}:
        raise InvalidData("claim", "Expected only display_name and identity.")
    identity = document["identity"]
    if not isinstance(identity, dict) or set(identity) != {"method", "hash"}:
        raise InvalidData("identity", "Expected only method and hash.")
    if identity["method"] != IDENTITY_METHOD:
        raise InvalidData("identity.method", "Unsupported identity method; expected " + IDENTITY_METHOD + ".")
    if not isinstance(identity["hash"], str) or CLAIM_HASH.fullmatch(identity["hash"]) is None:
        raise InvalidData("identity.hash", "Expected 64 lowercase SHA-256 hexadecimal digits.")
    host = normalize_host(identifier, {"display_name": document["display_name"]})
    return host, {"method": IDENTITY_METHOD, "hash": identity["hash"]}
