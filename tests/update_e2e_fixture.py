from __future__ import annotations

import argparse
import datetime
import json
import os
from pathlib import Path
import socket
import ssl
import struct
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


TESTS_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_ROOT.parent
SOURCE_ROOT = PROJECT_ROOT / "src"
sys.path.insert(0, str(SOURCE_ROOT))
sys.path.insert(0, str(TESTS_ROOT))

from r4os_server.registration.authentication import AuthenticationServer
from r4os_server.updates.catalog import Catalog
from r4os_server.registration.state import StateStore
from r4os_server.updates.state import StateStore as UpdateState
from r4os_server.updates.update_gateway import UpdateGatewayHandler, UpdateGatewayServer
from r4u_fixture import FNV_SEED, create_r4m0, create_r4u, fnv_update


MAGIC_KEY = "0x063210E2E5A17C0DEBADC0FFEE123450"
TARGET_RELEASE = "0.63.21"
TARGET_KERNEL = "0.1.1"
EXPECTED_TITLE = 'E2E "Neon" \\ Paket'
EXPECTED_DESCRIPTION = 'Bytegenau: quote="ok", slash=\\, umlaut=Gr\u00f6\u00dfe.'
INTERRUPTED_PACKAGE = f"LIVE06321-1.0.0-{TARGET_RELEASE}.R4U"


class FixtureCatalog(Catalog):
    def __init__(self, *args, public_host: str, **kwargs):
        if args and isinstance(args[0], StateStore):
            access = args[0]
            args = (UpdateState(access.database.with_name('updates-'+access.database.name)), *args[1:])
        super().__init__(*args, **kwargs)
        self.public_host = public_host

    def public_catalog(self, public_port: int) -> dict[str, object]:
        document = super().public_catalog(public_port)
        for release in document["releases"]:
            for package in release["packages"]:
                parsed = urlsplit(str(package["download_url"]))
                package["download_url"] = f"https://{self.public_host}:{public_port}{parsed.path}"
        return document


class FixtureGatewayHandler(UpdateGatewayHandler):
    server: "FixtureGatewayServer"

    def _handle(self, head_only: bool) -> None:
        self.server.record_request(self.command, self.path, self.headers.get("Range"))
        super()._handle(head_only)

    def _send_package(self, package_path: Path, head_only: bool) -> None:
        if (
            not head_only
            and package_path.name == self.server.interrupted_package
            and not self.server.interruption_sent
            and self.headers.get("Range") is None
        ):
            self.server.interruption_sent = True
            size = package_path.stat().st_size
            cutoff = max(64, min(size - 1, size // 2))
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.end_headers()
            with package_path.open("rb") as source:
                self.wfile.write(source.read(cutoff))
                self.wfile.flush()
            self.server.record_interruption(package_path.name, cutoff, size)
            try:
                self.connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self.connection.close()
            return
        super()._send_package(package_path, head_only)


class FixtureGatewayServer(UpdateGatewayServer):
    def __init__(self, *args, interrupted_package: str, report_writer, **kwargs):
        super().__init__(*args, **kwargs)
        self.RequestHandlerClass = FixtureGatewayHandler
        self.interrupted_package = interrupted_package
        self.interruption_sent = False
        self.events: list[dict[str, object]] = []
        self.report_writer = report_writer
        self.event_lock = threading.Lock()

    def record_request(self, method: str, path: str, range_header: str | None) -> None:
        with self.event_lock:
            self.events.append({"event": "request", "method": method, "path": path, "range": range_header})
            self.report_writer()

    def record_interruption(self, filename: str, sent: int, size: int) -> None:
        with self.event_lock:
            self.events.append({"event": "interrupted", "filename": filename, "sent": sent, "size": size})
            self.report_writer()


def wrapper_field(path: Path, name: str) -> str:
    prefix = name + "="
    with path.open("r", encoding="utf-8-sig") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if line.startswith(prefix):
                return line[len(prefix) :]
    raise ValueError(f"{name} is missing in {path}")


def development_private_key(path: Path):
    modulus = int(wrapper_field(path, "MODULUS_HEX"), 16)
    public_exponent = int(wrapper_field(path, "PUBLIC_EXPONENT_HEX"), 16)
    private_exponent = int(wrapper_field(path, "PRIVATE_EXPONENT_HEX"), 16)
    prime_p, prime_q = rsa.rsa_recover_prime_factors(modulus, public_exponent, private_exponent)
    return rsa.RSAPrivateNumbers(
        prime_p,
        prime_q,
        private_exponent,
        rsa.rsa_crt_dmp1(private_exponent, prime_p),
        rsa.rsa_crt_dmq1(private_exponent, prime_q),
        rsa.rsa_crt_iqmp(prime_p, prime_q),
        rsa.RSAPublicNumbers(public_exponent, modulus),
    ).private_key()


def fixture_certificate(certificate_wrapper: Path, key_wrapper: Path, hostname: str):
    issuer = x509.load_der_x509_certificate(bytes.fromhex(wrapper_field(certificate_wrapper, "CERT_DER_HEX")))
    private_key = development_private_key(key_wrapper)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer.subject)
        .public_key(private_key.public_key())
        .serial_number(0x63210001)
        .not_valid_before(datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc))
        .not_valid_after(datetime.datetime(2035, 12, 31, tzinfo=datetime.timezone.utc))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
        .sign(private_key, hashes.SHA256())
    )
    return certificate, private_key


def write_tls_material(directory: Path, certificate, private_key) -> tuple[Path, Path]:
    certificate_path = directory / "fixture-cert.pem"
    key_path = directory / "fixture-key.pem"
    certificate_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certificate_path, key_path


def create_group_r4u(path: Path) -> bytes:
    payloads = (
        ("GROUPA.R4X", "C:\\R4OS\\SOFTWARE\\DESKTOP\\GROUPA.R4X", "GROUPA", "1.0.0"),
        ("GROUPB.R4X", "C:\\R4OS\\SOFTWARE\\DESKTOP\\GROUPB.R4X", "GROUPB", "1.0.0"),
    )
    artifacts = [create_r4m0(name, version, "R4X") for _, _, name, version in payloads]
    payload_blob = b"".join(artifacts)
    lines = [
        "R4U_MANIFEST=2",
        "PACKAGE=GROUP06321",
        "PACKAGE_VERSION=1.0.0",
        f"RELEASE={TARGET_RELEASE}",
        "TITLE=Atomic package group",
        "DESCRIPTION=Two R4X components remain one catalog and install unit.",
        "ACTIVATION=live",
        "PRIORITY=normal",
        "PAYLOADS=2",
        "COMPONENTS=2",
        "REQUIRES=0",
        "ABI;R4M0=1;R4L=1;R4D=1;R4P=1;R4XSTART=1;R4U_COMPONENTS=1",
    ]
    offset = 0
    for index, ((filename, target, name, version), payload) in enumerate(zip(payloads, artifacts)):
        lines.append(
            f"PAYLOAD;index={index};name={filename};target={target};kind=software;size={len(payload)};checksum={fnv_update(FNV_SEED, payload)};offset={offset};abi=R4M0:1"
        )
        lines.append(f"ROLLBACK;target={target};backup={target}.prev;strategy=replace")
        lines.append(f"COMPONENT;payload={index};kind=R4X;name={name};target={target[2:].replace(chr(92), '/')};version={version};install=live")
        offset += len(payload)
    manifest = ("\n".join(lines) + "\n").encode("utf-8")
    header = bytearray(64)
    struct.pack_into(
        "<4sHHQQIIIII",
        header,
        0,
        b"R4U2",
        2,
        64,
        len(manifest),
        len(payload_blob),
        fnv_update(FNV_SEED, manifest),
        fnv_update(FNV_SEED, payload_blob),
        fnv_update(fnv_update(FNV_SEED, manifest), payload_blob),
        2,
        0,
    )
    package = bytes(header) + manifest + payload_blob
    path.write_bytes(package)
    return package


def create_packages(incoming: Path, kernel_artifact: Path) -> tuple[list[str], str]:
    package_names = [
        INTERRUPTED_PACKAGE,
        f"SINGLE06321-1.0.0-{TARGET_RELEASE}.R4U",
        f"GROUP06321-1.0.0-{TARGET_RELEASE}.R4U",
        f"KERNEL06321-1.0.0-{TARGET_RELEASE}.R4U",
        f"LIB06321-1.0.0-{TARGET_RELEASE}.R4U",
        f"SERVICE06321-1.0.0-{TARGET_RELEASE}.R4U",
    ]
    create_r4u(
        incoming / package_names[0],
        package="LIVE06321",
        release=TARGET_RELEASE,
        title=EXPECTED_TITLE,
        description=EXPECTED_DESCRIPTION,
        component_name="E2EGUI",
        component_version="1.0.0",
        payload_target="C:\\R4OS\\SOFTWARE\\DESKTOP\\E2EGUI.R4X",
    )
    create_r4u(
        incoming / package_names[1],
        package="SINGLE06321",
        release=TARGET_RELEASE,
        title="Update All single module",
        description="A standalone normal-priority package.",
        component_name="SING21",
        component_version="1.0.0",
        payload_target="C:\\R4OS\\SOFTWARE\\DESKTOP\\SING21.R4X",
    )
    create_group_r4u(incoming / package_names[2])
    create_r4u(
        incoming / package_names[3],
        package="KERNEL06321",
        release=TARGET_RELEASE,
        title="Kernel foundation",
        description="A bootable fixture kernel with a newer embedded version.",
        component_kind="KERNEL",
        component_name="KERNEL",
        component_version=TARGET_KERNEL,
        payload_target="/boot/r4os.elf",
        payload_kind="boot-kernel",
        payload=kernel_artifact.read_bytes(),
    )
    create_r4u(
        incoming / package_names[4],
        package="LIB06321",
        release=TARGET_RELEASE,
        title="Library foundation",
        description="Foundation provider for the dependent service.",
        component_kind="R4L",
        component_name="E2ELIB",
        component_version="1.0.0",
        payload_target="C:\\R4OS\\LIBS\\E2ELIB.R4L",
        payload_kind="system-library",
    )
    create_r4u(
        incoming / package_names[5],
        package="SERVICE06321",
        release=TARGET_RELEASE,
        title="Dependent service target",
        description="Restart-only R4X consumer of E2ELIB.",
        component_name="E2ESVC",
        component_version="1.0.0",
        payload_target="C:\\R4OS\\SERVICES\\E2ESVC.R4X",
        payload_kind="service",
        requirements=(("R4L", "E2ELIB", "/R4OS/LIBS/E2ELIB.R4L", "1.0.0", "installed"),),
    )
    components = [
        {"kind": "KERNEL", "name": "KERNEL", "target": "/boot/r4os.elf", "version": TARGET_KERNEL},
        {"kind": "R4X", "name": "E2EGUI", "target": "/R4OS/SOFTWARE/DESKTOP/E2EGUI.R4X", "version": "1.0.0"},
        {"kind": "R4X", "name": "SING21", "target": "/R4OS/SOFTWARE/DESKTOP/SING21.R4X", "version": "1.0.0"},
        {"kind": "R4X", "name": "GROUPA", "target": "/R4OS/SOFTWARE/DESKTOP/GROUPA.R4X", "version": "1.0.0"},
        {"kind": "R4X", "name": "GROUPB", "target": "/R4OS/SOFTWARE/DESKTOP/GROUPB.R4X", "version": "1.0.0"},
        {"kind": "R4L", "name": "E2ELIB", "target": "/R4OS/LIBS/E2ELIB.R4L", "version": "1.0.0"},
        {"kind": "R4X", "name": "E2ESVC", "target": "/R4OS/SERVICES/E2ESVC.R4X", "version": "1.0.0"},
    ]
    profile = {"count": len(components), "required": components}
    release_document = {
        "schema": 1,
        "release": TARGET_RELEASE,
        "profiles": {"slim": profile, "full": profile, "test": profile},
    }
    release_name = f"R4OS-RELEASE-{TARGET_RELEASE}.JSON"
    (incoming / release_name).write_text(json.dumps(release_document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return package_names, release_name


def atomic_json(path: Path, value: object) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def run(arguments) -> None:
    arguments.work_root.mkdir(parents=True, exist_ok=True)
    incoming = arguments.work_root / "incoming"
    releases = arguments.work_root / "releases"
    incoming.mkdir(parents=True, exist_ok=True)
    releases.mkdir(parents=True, exist_ok=True)
    store = StateStore(arguments.work_root / "state.sqlite3", 60)
    catalog = FixtureCatalog(store, incoming, releases, public_host=arguments.public_host)
    package_names, release_name = create_packages(incoming, arguments.kernel_artifact)
    for filename in package_names:
        catalog.publish(filename)
    catalog.publish_release_manifest(release_name)

    report: dict[str, object] = {
        "ready": False,
        "auth_port": arguments.auth_port,
        "gateway_port": arguments.gateway_port,
        "magic_key": MAGIC_KEY,
        "target_release": TARGET_RELEASE,
        "target_kernel": TARGET_KERNEL,
        "expected_title": EXPECTED_TITLE,
        "expected_description": EXPECTED_DESCRIPTION,
        "packages": package_names,
        "catalog": catalog.public_catalog(arguments.gateway_port),
        "events": [],
    }
    gateway: FixtureGatewayServer | None = None

    def write_report() -> None:
        if gateway is not None:
            report["events"] = list(gateway.events)
        report["allowlist"] = store.list_allowed()
        report["blocklist"] = store.list_blocked()
        atomic_json(arguments.report_file, report)

    authentication = AuthenticationServer((arguments.bind, arguments.auth_port), store, MAGIC_KEY, 86400)
    gateway = FixtureGatewayServer(
        (arguments.bind, arguments.gateway_port),
        store,
        catalog,
        3,
        86400,
        arguments.gateway_port,
        interrupted_package=INTERRUPTED_PACKAGE,
        report_writer=write_report,
    )

    certificate, private_key = fixture_certificate(arguments.certificate_wrapper, arguments.key_wrapper, arguments.public_host)
    with tempfile.TemporaryDirectory(prefix="r4os-update-e2e-tls-") as tls_directory:
        certificate_path, key_path = write_tls_material(Path(tls_directory), certificate, private_key)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        context.set_ciphers("ECDHE-RSA-AES128-GCM-SHA256")
        context.set_alpn_protocols(["http/1.1"])
        context.load_cert_chain(certificate_path, key_path)
        gateway.socket = context.wrap_socket(gateway.socket, server_side=True)

        servers = (authentication, gateway)
        threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in servers]
        for thread in threads:
            thread.start()
        report["ready"] = True
        write_report()
        atomic_json(arguments.ready_file, {"ready": True, "auth_port": arguments.auth_port, "gateway_port": arguments.gateway_port})
        try:
            deadline = time.monotonic() + arguments.timeout
            while not arguments.stop_file.exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("update E2E fixture timed out")
                time.sleep(0.1)
        finally:
            for server in servers:
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=5)
            write_report()


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated real R4OS update-server E2E fixture")
    parser.add_argument("--certificate-wrapper", type=Path, required=True)
    parser.add_argument("--key-wrapper", type=Path, required=True)
    parser.add_argument("--kernel-artifact", type=Path, required=True)
    parser.add_argument("--work-root", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path, required=True)
    parser.add_argument("--report-file", type=Path, required=True)
    parser.add_argument("--stop-file", type=Path, required=True)
    parser.add_argument("--bind", default="0.0.0.0")
    parser.add_argument("--public-host", default="10.0.2.2")
    parser.add_argument("--auth-port", type=int, default=18338)
    parser.add_argument("--gateway-port", type=int, default=18339)
    parser.add_argument("--timeout", type=float, default=600.0)
    arguments = parser.parse_args()
    for port in (arguments.auth_port, arguments.gateway_port):
        if not 1 <= port <= 65535:
            parser.error("ports must be between 1 and 65535")
    if arguments.auth_port == arguments.gateway_port:
        parser.error("auth and gateway ports must differ")
    run(arguments)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
