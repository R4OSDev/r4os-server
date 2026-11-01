"""Separate API service tokens; the private WebApp requires no session."""

import hashlib
import hmac
import secrets


def role(config, header):
    parts = header.split() if isinstance(header, str) else []
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    candidate = parts[1].encode()
    for name, expected in (("admin", config.admin_token), ("publisher", config.publisher_token),
                           ("benchmark", config.benchmark_token)):
        if expected and hmac.compare_digest(candidate, expected.encode()):
            return name
    return None


def password_hash(password, salt=None):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), 310000)
    return salt + "$" + digest.hex()


