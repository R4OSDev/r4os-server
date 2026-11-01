#!/usr/bin/env python3
"""Trusted entrypoint; works with Python -I without a package installation."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    from r4os_server.main import main
    raise SystemExit(main())
