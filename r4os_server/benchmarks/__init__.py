"""Benchmark feature, adapted from Apache-2.0 R4OS DevCenter."""
from pathlib import Path
NAME = "R4OS Server Benchmarks"
VERSION = (Path(__file__).resolve().parents[2]/"VERSION").read_text().strip()
