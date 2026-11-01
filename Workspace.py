#!/usr/bin/env python3
"""Entry point for the credential-free local build worker."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from r4os_server.workspace.worker import main

if __name__ == "__main__":
    main()
