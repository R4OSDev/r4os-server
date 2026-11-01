from pathlib import Path
import tempfile
import unittest

from r4os_server.updates.catalog import Catalog
from r4os_server.updates.state import StateStore
from r4u_fixture import create_multi_r4u, create_r4u
from release_fixture import create_release_manifest


class CatalogTest(unittest.TestCase):
    def test_valid_package_is_atomically_published(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "incoming"
            releases = root / "releases"
            incoming.mkdir()
            releases.mkdir()
            store = StateStore(root / "state.sqlite3", 60)
            catalog = Catalog(store, incoming, releases)
            filename = "TEST06314-1.0.0-0.63.14.R4U"
            create_r4u(incoming / filename)
            self.assertTrue(catalog.list_incoming()[0].valid)
            published = catalog.publish(filename)
            self.assertTrue(published.valid)
            self.assertFalse((incoming / filename).exists())
            self.assertTrue((releases / filename).is_file())
            self.assertEqual("0.63.14", catalog.list_released()[0].metadata["release"])
            release_filename = "R4OS-RELEASE-0.63.14.JSON"
            create_release_manifest(incoming / release_filename)
            release_entry = catalog.publish_release_manifest(release_filename)
            self.assertTrue(release_entry.valid)
            document = catalog.public_catalog(1339)
            self.assertEqual(2, document["schema"])
            self.assertIsNotNone(document["generated_at"])
            self.assertEqual("0.63.14", document["releases"][0]["release"])
            self.assertEqual("R4OS Test Update", document["releases"][0]["packages"][0]["title"])

            create_r4u(
                incoming / "LATE-1.0.0-0.63.14.R4U",
                package="LATE",
                component_name="LATE",
                payload_target="C:\\R4OS\\SOFTWARE\\TEST\\LATE.R4X",
            )
            with self.assertRaisesRegex(ValueError, "already completed by its release manifest"):
                catalog.publish("LATE-1.0.0-0.63.14.R4U")

    def test_invalid_package_stays_in_incoming(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "incoming"
            releases = root / "releases"
            incoming.mkdir()
            releases.mkdir()
            (incoming / "BAD.R4U").write_bytes(b"invalid")
            catalog = Catalog(StateStore(root / "state.sqlite3", 60), incoming, releases)
            entry = catalog.list_incoming()[0]
            self.assertFalse(entry.valid)
            with self.assertRaises(ValueError):
                catalog.publish("BAD.R4U")
            self.assertTrue((incoming / "BAD.R4U").exists())

    def test_release_manifest_without_package_is_not_publishable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "incoming"
            releases = root / "releases"
            incoming.mkdir()
            releases.mkdir()
            catalog = Catalog(StateStore(root / "state.sqlite3", 60), incoming, releases)
            filename = "R4OS-RELEASE-0.63.14.JSON"
            create_release_manifest(incoming / filename)
            with self.assertRaisesRegex(ValueError, "no published installation package"):
                catalog.publish_release_manifest(filename)

    def test_dependencies_precede_foundation_priority_and_unresolved_requirements_fail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "incoming"
            releases = root / "releases"
            incoming.mkdir()
            releases.mkdir()
            catalog = Catalog(StateStore(root / "state.sqlite3", 60), incoming, releases)
            provider = {"kind": "R4X", "name": "PROVIDER", "target": "/R4OS/SOFTWARE/TEST/PROVIDER.R4X", "version": "2.0.0"}
            foundation = {"kind": "R4L", "name": "R4CAT", "target": "/R4OS/LIBS/R4CAT.R4L", "version": "2.0.0"}
            create_r4u(
                incoming / "TEST06314-1.0.0-0.63.14.R4U",
                requirements=(("R4X", "PROVIDER", provider["target"], provider["version"], "installed"),),
            )
            create_r4u(
                incoming / "PROVIDER-1.0.0-0.63.14.R4U",
                package="PROVIDER",
                component_name="PROVIDER",
                component_version="2.0.0",
                payload_target="C:\\R4OS\\SOFTWARE\\TEST\\PROVIDER.R4X",
            )
            create_r4u(
                incoming / "R4CAT-1.0.0-0.63.14.R4U",
                package="R4CAT",
                component_kind="R4L",
                component_name="R4CAT",
                component_version="2.0.0",
                payload_target="C:\\R4OS\\LIBS\\R4CAT.R4L",
                payload_kind="system-library",
            )
            for filename in (
                "TEST06314-1.0.0-0.63.14.R4U",
                "PROVIDER-1.0.0-0.63.14.R4U",
                "R4CAT-1.0.0-0.63.14.R4U",
            ):
                catalog.publish(filename)
            release_filename = "R4OS-RELEASE-0.63.14.JSON"
            create_release_manifest(incoming / release_filename, extra_components=(provider, foundation))
            catalog.publish_release_manifest(release_filename)
            packages = catalog.public_catalog(1339)["releases"][0]["packages"]
            self.assertEqual(["R4CAT", "PROVIDER", "TEST06314"], [item["id"] for item in packages])

            bad_root = root / "bad"
            bad_incoming = bad_root / "incoming"
            bad_releases = bad_root / "releases"
            bad_incoming.mkdir(parents=True)
            bad_releases.mkdir()
            bad_catalog = Catalog(StateStore(bad_root / "state.sqlite3", 60), bad_incoming, bad_releases)
            create_r4u(
                bad_incoming / "TEST06314-1.0.0-0.63.14.R4U",
                requirements=(("R4X", "MISSING", "/R4OS/SOFTWARE/TEST/MISSING.R4X", "1.0.0", "installed"),),
            )
            bad_catalog.publish("TEST06314-1.0.0-0.63.14.R4U")
            create_release_manifest(bad_incoming / release_filename)
            with self.assertRaisesRegex(ValueError, "Unresolvable specific requirement"):
                bad_catalog.publish_release_manifest(release_filename)

    def test_multi_component_package_remains_one_catalog_unit_and_kernel_may_stay_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            incoming = root / "incoming"
            releases = root / "releases"
            incoming.mkdir()
            releases.mkdir()
            catalog = Catalog(StateStore(root / "state.sqlite3", 60), incoming, releases)
            package_name = "MULTI-1.0.0-0.63.14.R4U"
            create_multi_r4u(incoming / package_name)
            catalog.publish(package_name)
            library = {
                "kind": "R4L",
                "name": "R4CAT",
                "target": "/R4OS/LIBS/R4CAT.R4L",
                "version": "2.0.0",
            }
            create_release_manifest(
                incoming / "R4OS-RELEASE-0.63.14.JSON",
                component_name="APP",
                component_version="1.1.0",
                component_target="/R4OS/SOFTWARE/TEST/APP.R4X",
                extra_components=(library,),
            )
            catalog.publish_release_manifest("R4OS-RELEASE-0.63.14.JSON")
            first = catalog.public_catalog(1339)["releases"][0]
            self.assertEqual(1, len(first["packages"]))
            self.assertEqual(2, len(first["packages"][0]["components"]))
            first_kernel = first["manifest"]["profiles"]["slim"]["required"][0]["version"]

            second_name = "NEXT-1.0.0-0.63.15.R4U"
            create_r4u(
                incoming / second_name,
                package="NEXT",
                release="0.63.15",
                component_name="NEXT",
                component_version="1.0.1",
                payload_target="C:\\R4OS\\SOFTWARE\\TEST\\NEXT.R4X",
            )
            catalog.publish(second_name)
            create_release_manifest(
                incoming / "R4OS-RELEASE-0.63.15.JSON",
                release="0.63.15",
                component_name="NEXT",
                component_version="1.0.1",
                component_target="/R4OS/SOFTWARE/TEST/NEXT.R4X",
            )
            catalog.publish_release_manifest("R4OS-RELEASE-0.63.15.JSON")
            latest = catalog.public_catalog(1339)["releases"]
            self.assertEqual(1, len(latest))
            second = latest[0]
            self.assertEqual("0.63.15", second["release"])
            second_kernel = second["manifest"]["profiles"]["slim"]["required"][0]["version"]
            self.assertEqual(first_kernel, second_kernel)
            self.assertEqual(2, len(catalog.list_released_release_manifests()))


if __name__ == "__main__":
    unittest.main()
