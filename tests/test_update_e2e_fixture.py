from __future__ import annotations

import http.client
import json
from pathlib import Path
import socket
import tempfile
import threading
import unittest

from r4os_server.registration.authentication import AuthenticationServer
from r4os_server.registration.state import StateStore
from r4u_fixture import create_kernel_elf
from update_e2e_fixture import (
    EXPECTED_DESCRIPTION,
    EXPECTED_TITLE,
    INTERRUPTED_PACKAGE,
    MAGIC_KEY,
    TARGET_RELEASE,
    FixtureCatalog,
    FixtureGatewayServer,
    create_packages,
)


class RunningServer:
    def __init__(self, server):
        self.server = server
        self.thread = threading.Thread(target=server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server

    def __exit__(self, _kind, _value, _traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def request(port: int, path: str, headers: dict[str, str] | None = None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    try:
        body = response.read()
    except http.client.IncompleteRead as error:
        body = error.partial
    result = response.status, dict(response.getheaders()), body
    connection.close()
    return result


class UpdateEndToEndFixtureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.incoming = self.root / "incoming"
        self.releases = self.root / "releases"
        self.incoming.mkdir()
        self.releases.mkdir()
        self.kernel = self.root / "r4os.elf"
        self.kernel.write_bytes(create_kernel_elf("0.1.1"))
        self.store = StateStore(self.root / "state.sqlite3", 60)
        self.catalog = FixtureCatalog(self.store, self.incoming, self.releases, public_host="10.0.2.2")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def publish_fixture(self) -> list[str]:
        packages, release_name = create_packages(self.incoming, self.kernel)
        for filename in packages:
            self.catalog.publish(filename)
        self.catalog.publish_release_manifest(release_name)
        return packages

    def test_fixture_catalog_preserves_display_text_group_and_dependency_order(self) -> None:
        packages = self.publish_fixture()
        self.assertEqual(6, len(packages))
        document = self.catalog.public_catalog(18339)
        release = document["releases"][0]
        self.assertEqual(TARGET_RELEASE, release["release"])
        by_id = {package["id"]: package for package in release["packages"]}
        self.assertEqual(EXPECTED_TITLE, by_id["LIVE06321"]["title"])
        self.assertEqual(EXPECTED_DESCRIPTION, by_id["LIVE06321"]["description"])
        self.assertEqual(2, len(by_id["GROUP06321"]["components"]))
        self.assertTrue(by_id["LIVE06321"]["download_url"].startswith("https://10.0.2.2:18339/"))
        self.assertLess(by_id["LIB06321"]["install_order"], by_id["SERVICE06321"]["install_order"])
        self.assertLess(by_id["KERNEL06321"]["install_order"], by_id["LIVE06321"]["install_order"])

    def test_fixture_auth_isolated_warnings_block_and_range_resume(self) -> None:
        self.publish_fixture()
        report_events: list[dict[str, object]] = []
        gateway_holder: list[FixtureGatewayServer] = []

        def report_writer() -> None:
            if gateway_holder:
                report_events[:] = list(gateway_holder[0].events)

        auth = AuthenticationServer(("127.0.0.1", 0), self.store, MAGIC_KEY, 86400)
        gateway = FixtureGatewayServer(
            ("127.0.0.1", 0),
            self.store,
            self.catalog,
            3,
            86400,
            18339,
            interrupted_package=INTERRUPTED_PACKAGE,
            report_writer=report_writer,
        )
        gateway_holder.append(gateway)
        with RunningServer(auth), RunningServer(gateway):
            with socket.create_connection(("127.0.0.1", auth.server_address[1]), timeout=5) as connection:
                connection.sendall((MAGIC_KEY + "\n").encode("ascii"))
                self.assertEqual(b"OK", connection.recv(2))
            status, _, catalog_body = request(gateway.server_address[1], "/catalog")
            self.assertEqual(200, status)
            self.assertEqual(6, len(json.loads(catalog_body)["releases"][0]["packages"]))
            status, headers, first = request(gateway.server_address[1], "/packages/" + INTERRUPTED_PACKAGE)
            self.assertEqual(200, status)
            self.assertLess(len(first), int(headers["Content-Length"]))
            status, headers, resumed = request(
                gateway.server_address[1],
                "/packages/" + INTERRUPTED_PACKAGE,
                {"Range": f"bytes={len(first)}-"},
            )
            self.assertEqual(206, status)
            self.assertTrue(headers["Content-Range"].startswith(f"bytes {len(first)}-"))
            self.assertGreater(len(resumed), 0)
        self.assertEqual(1, len(self.store.list_allowed()))
        self.assertEqual([], self.store.list_blocked())
        self.assertTrue(any(event.get("event") == "interrupted" for event in report_events))
        self.assertTrue(any(event.get("range") == f"bytes={len(first)}-" for event in report_events))

        isolated_root = self.root / "negative"
        isolated_root.mkdir()
        isolated_store = StateStore(isolated_root / "state.sqlite3", 60)
        wrong_auth = AuthenticationServer(("127.0.0.1", 0), isolated_store, MAGIC_KEY, 86400)
        with RunningServer(wrong_auth):
            with socket.create_connection(("127.0.0.1", wrong_auth.server_address[1]), timeout=5) as connection:
                connection.sendall(b"WRONG\n")
                self.assertEqual(b"", connection.recv(16))
        self.assertEqual(1, len(isolated_store.list_blocked()))
        self.assertEqual(1, len(self.store.list_allowed()))

        warning_root = self.root / "warnings"
        warning_root.mkdir()
        warning_store = StateStore(warning_root / "state.sqlite3", 60)
        warning_catalog = FixtureCatalog(
            warning_store,
            warning_root / "incoming",
            warning_root / "releases",
            public_host="10.0.2.2",
        )
        warnings = FixtureGatewayServer(
            ("127.0.0.1", 0),
            warning_store,
            warning_catalog,
            3,
            86400,
            18339,
            interrupted_package="none",
            report_writer=lambda: None,
        )
        with RunningServer(warnings):
            self.assertEqual((401, b"STOP NOW!\n"), (lambda value: (value[0], value[2]))(request(warnings.server_address[1], "/catalog")))
            self.assertEqual((401, b"LAST CHANCE!\n"), (lambda value: (value[0], value[2]))(request(warnings.server_address[1], "/catalog")))
            self.assertEqual(403, request(warnings.server_address[1], "/catalog")[0])
        self.assertEqual(1, len(warning_store.list_blocked()))
        self.assertEqual(1, len(self.store.list_allowed()))


if __name__ == "__main__":
    unittest.main()
