"""Read one connection credential only for its exact HTTPS GitHub repository."""
import os
from pathlib import Path
import re
import stat
import sys
from urllib.parse import urlsplit


def answer(prompt, url, credential_file):
    match = re.fullmatch(r"(Username|Password) for '(https://[^']+)': ?", prompt)
    if not match:
        raise ValueError("Unexpected Git credential prompt.")
    expected, requested = urlsplit(url), urlsplit(match[2])
    if (expected.scheme != "https" or expected.netloc != "github.com" or not expected.path
            or requested.scheme != expected.scheme or requested.hostname != "github.com"
            or requested.port not in (None, 443) or requested.path != expected.path
            or requested.username not in (None, "x-access-token") or requested.password
            or requested.query or requested.fragment):
        raise ValueError("Unexpected Git credential destination.")
    if match[1] == "Username":
        return "x-access-token"
    path = Path(credential_file)
    if path.parent.is_symlink():
        raise ValueError("Invalid credential directory.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "r", encoding="utf-8") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("Invalid credential file.")
        value = source.read(4097)
    if not re.fullmatch(r"[!-~]{8,4096}", value):
        raise ValueError("Invalid stored credential.")
    return value


if __name__ == "__main__":
    try:
        if len(sys.argv) != 2:
            raise ValueError("Missing prompt.")
        print(answer(sys.argv[1], os.environ["R4OS_GIT_URL"], os.environ["R4OS_GIT_CREDENTIAL_FILE"]))
    except (KeyError, OSError, ValueError, UnicodeError):
        raise SystemExit(1) from None
