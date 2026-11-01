#!/usr/bin/env python3
"""Run inherited and focused integration tests with isolated temporary state."""
from pathlib import Path
import argparse
import sys
import tempfile
import unittest

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root))
sys.path.insert(0, str(root / "tests"))
parser = argparse.ArgumentParser()
parser.add_argument("--pattern", default="test_*.py")
args = parser.parse_args()
scratch = root.parent / "Temp/Test-R4OS-Server"
scratch.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(scratch)
suite = unittest.defaultTestLoader.discover(str(root / "tests"), pattern=args.pattern)
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(0 if result.wasSuccessful() else 1)
