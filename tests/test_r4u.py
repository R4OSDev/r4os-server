from pathlib import Path
import tempfile
import unittest

from r4os_server.updates.r4u import R4UError, inspect_package
from r4u_fixture import create_kernel_elf, create_multi_r4u, create_r4m0, create_r4u


class R4UTest(unittest.TestCase):
    def test_valid_package_is_fully_inspected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_path = Path(directory) / "TEST06314-1.0.0-0.63.14.R4U"
            package = create_r4u(package_path)
            metadata = inspect_package(package_path)
            self.assertEqual("TEST06314", metadata.package)
            self.assertEqual("0.63.14", metadata.release)
            self.assertEqual("R4OS Test Update", metadata.title)
            self.assertEqual(1, metadata.payload_count)
            self.assertEqual("TESTAPP", metadata.components[0].name)
            self.assertEqual(len(package), metadata.size)
            self.assertEqual(64, len(metadata.sha256))

    def test_corrupt_payload_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package_path = Path(directory) / "BROKEN.R4U"
            package = bytearray(create_r4u(package_path))
            package[-1] ^= 0xFF
            package_path.write_bytes(package)
            with self.assertRaises(R4UError):
                inspect_package(package_path)

    def test_artifact_identity_and_derived_classes_are_authoritative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "BAD-1.0.0-0.63.14.R4U"
            create_r4u(path, component_version="1.2.3", payload=create_r4m0(version="1.2.2"))
            with self.assertRaisesRegex(R4UError, "artefact identity"):
                inspect_package(path)
            create_r4u(
                path,
                package="R4CAT",
                component_kind="R4L",
                component_name="R4CAT",
                component_version="2.0.0",
                payload_target="C:\\R4OS\\LIBS\\R4CAT.R4L",
                payload_kind="system-library",
                priority="normal",
            )
            with self.assertRaisesRegex(R4UError, "priority class"):
                inspect_package(path)

    def test_config_package_and_utf8_plain_text_are_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "CONFIG-1.0.0-0.63.14.R4U"
            create_r4u(
                path,
                package="CONFIG",
                component_kind=None,
                payload_target="C:\\R4OS\\CONFIG\\UPDATE.R4S",
                payload_kind="config",
                title="Geraete & Groesse",
                description="UTF-8: Geräte, Größe und naïve Prüfung.",
                requirements=(("KERNEL", "KERNEL", "/boot/r4os.elf", "0.1.0", "active"),),
            )
            metadata = inspect_package(path)
            self.assertEqual(0, metadata.component_count)
            self.assertEqual("UTF-8: Geräte, Größe und naïve Prüfung.", metadata.description)
            self.assertEqual("live", metadata.activation)

    def test_kernel_package_is_verified_from_elf_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "KERNEL-1.0.0-0.63.14.R4U"
            create_r4u(
                path,
                package="KERNEL",
                component_kind="KERNEL",
                component_name="KERNEL",
                component_version="0.2.0",
                payload_target="/boot/r4os.elf",
                payload_kind="boot-kernel",
                payload=create_kernel_elf("0.2.0"),
            )
            metadata = inspect_package(path)
            component = metadata.components[0]
            self.assertEqual(("KERNEL", "KERNEL", "0.2.0"), (component.kind, component.name, component.version))
            self.assertEqual("restart", metadata.activation)
            self.assertEqual("foundation", metadata.priority)

    def test_multi_component_package_is_atomic_and_duplicates_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "MULTI-1.0.0-0.63.14.R4U"
            create_multi_r4u(path)
            metadata = inspect_package(path)
            self.assertEqual(2, metadata.payload_count)
            self.assertEqual(["APP", "R4CAT"], [item.name for item in metadata.components])
            self.assertEqual("foundation", metadata.priority)
            duplicate = root / "DUPLICATE.R4U"
            create_multi_r4u(duplicate, duplicate_component=True)
            with self.assertRaisesRegex(R4UError, "Duplicate component"):
                inspect_package(duplicate)


if __name__ == "__main__":
    unittest.main()
