from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from r4os_server.updates.release_manifest import ReleaseManifestError, inspect_release_manifest
from release_fixture import create_release_manifest


class ReleaseManifestTest(unittest.TestCase):
    def test_generated_release_manifest_is_strictly_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "R4OS-RELEASE-0.63.14.JSON"
            raw = create_release_manifest(path)
            metadata = inspect_release_manifest(path)
            self.assertEqual("0.63.14", metadata.release)
            self.assertEqual(2, metadata.profiles["slim"].count)
            self.assertEqual(len(raw), metadata.size)
            self.assertEqual(64, len(metadata.sha256))

    def test_unknown_schema_and_profile_drift_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "R4OS-RELEASE-0.63.14.JSON"
            create_release_manifest(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["schema"] = 2
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaises(ReleaseManifestError):
                inspect_release_manifest(path)
            create_release_manifest(path)
            value = json.loads(path.read_text(encoding="utf-8"))
            value["profiles"]["test"]["required"][1]["version"] = "9.9.9"
            path.write_text(json.dumps(value), encoding="utf-8")
            with self.assertRaisesRegex(ReleaseManifestError, "Slim component"):
                inspect_release_manifest(path)


if __name__ == "__main__":
    unittest.main()
