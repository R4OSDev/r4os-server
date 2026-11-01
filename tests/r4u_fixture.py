from __future__ import annotations

from pathlib import Path
import struct

from r4os_server.updates.r4u import FNV_SEED, fnv_update


def create_r4m0(name: str = "TESTAPP", version: str = "1.2.3", kind: str = "R4X") -> bytes:
    kind_value = {"R4X": 1, "R4L": 2, "R4D": 3, "R4P": 4}[kind]
    first_name = f"R4D_{name}" if kind == "R4D" else f"R4P_{name}" if kind == "R4P" else name
    explicit = f"{kind.lower()}.name={name}\0" if kind in {"R4X", "R4D", "R4P"} else ""
    strings = (first_name + "\0" + explicit + f"module.version={version}\0").encode("ascii")
    header = bytearray(64)
    struct.pack_into("<4sHHHH", header, 0, b"R4M0", 1, 1, kind_value, 64)
    struct.pack_into("<II", header, 56, 64, len(strings))
    return bytes(header) + strings


def create_kernel_elf(version: str = "0.1.0") -> bytes:
    names = b"\0.shstrtab\0.r4os.kernel.meta\0"
    names_offset = 64
    metadata_offset = 96
    section_offset = 144
    result = bytearray(section_offset + 3 * 64)
    result[0:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<H", result, 18, 62)
    struct.pack_into("<Q", result, 40, section_offset)
    struct.pack_into("<HHH", result, 58, 64, 3, 1)
    result[names_offset : names_offset + len(names)] = names
    metadata = bytearray(44)
    metadata[0:8] = b"R4OSKRN1"
    parts = tuple(int(value) for value in version.split("."))
    struct.pack_into("<IIIII", metadata, 8, 1, 44, *parts)
    version_bytes = version.encode("ascii")
    metadata[28 : 28 + len(version_bytes)] = version_bytes
    result[metadata_offset : metadata_offset + len(metadata)] = metadata
    string_section = section_offset + 64
    struct.pack_into("<I", result, string_section, 1)
    struct.pack_into("<QQ", result, string_section + 24, names_offset, len(names))
    metadata_section = section_offset + 128
    struct.pack_into("<I", result, metadata_section, 11)
    struct.pack_into("<QQ", result, metadata_section + 24, metadata_offset, len(metadata))
    return bytes(result)


def create_multi_r4u(path: Path, *, duplicate_component: bool = False) -> bytes:
    payload_values = (
        ("APP.R4X", "C:\\R4OS\\SOFTWARE\\TEST\\APP.R4X", "software", "R4X", "APP", "1.1.0", create_r4m0("APP", "1.1.0", "R4X")),
        (
            "DUP.R4X" if duplicate_component else "R4CAT.R4L",
            "C:\\R4OS\\SOFTWARE\\TEST\\DUP.R4X" if duplicate_component else "C:\\R4OS\\LIBS\\R4CAT.R4L",
            "software" if duplicate_component else "system-library",
            "R4X" if duplicate_component else "R4L",
            "APP" if duplicate_component else "R4CAT",
            "1.1.0" if duplicate_component else "2.0.0",
            create_r4m0("APP", "1.1.0", "R4X") if duplicate_component else create_r4m0("R4CAT", "2.0.0", "R4L"),
        ),
    )
    payload_blob = b"".join(item[6] for item in payload_values)
    lines = [
        "R4U_MANIFEST=2",
        "PACKAGE=MULTI",
        "PACKAGE_VERSION=1.0.0",
        "RELEASE=0.63.14",
        "TITLE=Mehrkomponentenupdate",
        "DESCRIPTION=Eine atomare Gruppe aus Anwendung und Systembibliothek.",
        "ACTIVATION=restart",
        "PRIORITY=foundation",
        "PAYLOADS=2",
        "COMPONENTS=2",
        "REQUIRES=0",
        "ABI;R4M0=1;R4L=1;R4D=1;R4P=1;R4XSTART=1;R4U_COMPONENTS=1",
    ]
    offset = 0
    for index, (name, target, payload_kind, component_kind, component_name, version, payload) in enumerate(payload_values):
        checksum = fnv_update(FNV_SEED, payload)
        lines.append(
            f"PAYLOAD;index={index};name={name};target={target};kind={payload_kind};size={len(payload)};checksum={checksum};offset={offset};abi=R4M0:1"
        )
        lines.append(f"ROLLBACK;target={target};backup={target}.prev;strategy=replace")
        canonical = target[2:].replace(chr(92), "/")
        install = "live" if component_kind == "R4X" else "restart"
        lines.append(
            f"COMPONENT;payload={index};kind={component_kind};name={component_name};target={canonical};version={version};install={install}"
        )
        offset += len(payload)
    manifest = ("\n".join(lines) + "\n").encode("utf-8")
    manifest_checksum = fnv_update(FNV_SEED, manifest)
    payload_checksum = fnv_update(FNV_SEED, payload_blob)
    package_checksum = fnv_update(fnv_update(FNV_SEED, manifest), payload_blob)
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
        manifest_checksum,
        payload_checksum,
        package_checksum,
        2,
        1,
    )
    package_bytes = bytes(header) + manifest + payload_blob
    path.write_bytes(package_bytes)
    return package_bytes


def create_r4u(
    path: Path,
    *,
    package: str = "TEST06314",
    package_version: str = "1.0.0",
    release: str = "0.63.14",
    title: str = "R4OS Test Update",
    description: str = "Prueft den komponentenfaehigen Katalog.",
    component_kind: str | None = "R4X",
    component_name: str = "TESTAPP",
    component_version: str = "1.2.3",
    payload_target: str = "C:\\R4OS\\SOFTWARE\\TEST\\TESTAPP.R4X",
    payload_kind: str = "software",
    activation: str | None = None,
    priority: str | None = None,
    requirements: tuple[tuple[str, str, str, str, str], ...] = (),
    payload: bytes | None = None,
) -> bytes:
    if payload is None:
        if component_kind is None:
            payload = b"CONFIG=1\n"
        else:
            payload = create_r4m0(component_name, component_version, component_kind)
    canonical_target = payload_target
    if len(canonical_target) >= 3 and canonical_target[1] == ":":
        canonical_target = canonical_target[2:]
    canonical_target = canonical_target.replace("\\", "/")
    if component_kind is None:
        derived_activation = "live"
        derived_priority = "normal"
    else:
        derived_activation = "restart" if component_kind in {"KERNEL", "R4L", "R4D", "R4P"} or canonical_target.upper().startswith("/R4OS/SERVICES/") else "live"
        derived_priority = "foundation" if component_kind in {"KERNEL", "R4L"} else "normal"
    activation = activation or derived_activation
    priority = priority or derived_priority
    payload_checksum = fnv_update(FNV_SEED, payload)
    lines = [
        "R4U_MANIFEST=2",
        f"PACKAGE={package}",
        f"PACKAGE_VERSION={package_version}",
        f"RELEASE={release}",
        f"TITLE={title}",
        f"DESCRIPTION={description}",
        f"ACTIVATION={activation}",
        f"PRIORITY={priority}",
        "PAYLOADS=1",
        f"COMPONENTS={1 if component_kind is not None else 0}",
        f"REQUIRES={len(requirements)}",
        "ABI;R4M0=1;R4L=1;R4D=1;R4P=1;R4XSTART=1;R4U_COMPONENTS=1",
        f"PAYLOAD;index=0;name={Path(payload_target.replace(chr(92), '/')).name};target={payload_target};kind={payload_kind};size={len(payload)};checksum={payload_checksum};offset=0;abi=R4M0:1",
        f"ROLLBACK;target={payload_target};backup={payload_target}.prev;strategy=replace",
    ]
    if component_kind is not None:
        lines.append(
            f"COMPONENT;payload=0;kind={component_kind};name={component_name};target={canonical_target};version={component_version};install={derived_activation}"
        )
    for kind, name, target, version, state in requirements:
        lines.append(f"REQUIRE;kind={kind};name={name};target={target};version={version};state={state}")
    manifest = ("\n".join(lines) + "\n").encode("utf-8")
    manifest_checksum = fnv_update(FNV_SEED, manifest)
    package_checksum = fnv_update(fnv_update(FNV_SEED, manifest), payload)
    header = bytearray(64)
    struct.pack_into(
        "<4sHHQQIIIII",
        header,
        0,
        b"R4U2",
        2,
        64,
        len(manifest),
        len(payload),
        manifest_checksum,
        payload_checksum,
        package_checksum,
        1,
        1 if activation == "restart" else 0,
    )
    package_bytes = bytes(header) + manifest + payload
    path.write_bytes(package_bytes)
    return package_bytes
