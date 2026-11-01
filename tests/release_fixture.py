from __future__ import annotations

import json
from pathlib import Path


def create_release_manifest(
    path: Path,
    *,
    release: str = "0.63.14",
    component_name: str = "TESTAPP",
    component_version: str = "1.2.3",
    component_target: str = "/R4OS/SOFTWARE/TEST/TESTAPP.R4X",
    extra_components: tuple[dict[str, str], ...] = (),
) -> bytes:
    kernel = {"kind": "KERNEL", "name": "KERNEL", "target": "/boot/r4os.elf", "version": "0.1.0"}
    component = {"kind": "R4X", "name": component_name, "target": component_target, "version": component_version}
    full_only = {"kind": "R4X", "name": "FULLOPT", "target": "/R4OS/SOFTWARE/TEST/FULLOPT.R4X", "version": "1.0.0"}
    test_only = {"kind": "R4X", "name": "TESTONLY", "target": "/R4OS/SOFTWARE/TEST/TESTONLY.R4X", "version": "1.0.0"}
    base = [kernel, component, *extra_components]
    value = {
        "schema": 1,
        "release": release,
        "profiles": {
            "slim": {"count": len(base), "required": base},
            "full": {"count": len(base) + 1, "required": [*base, full_only]},
            "test": {"count": len(base) + 1, "required": [*base, test_only]},
        },
    }
    raw = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    path.write_bytes(raw)
    return raw
