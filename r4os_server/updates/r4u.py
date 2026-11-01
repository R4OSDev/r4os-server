from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
from pathlib import Path
import re
import struct
from typing import BinaryIO


HEADER_SIZE = 64
MANIFEST_MAX = 32 * 1024
MAX_PAYLOADS = 32
MAX_COMPONENTS = 32
MAX_REQUIREMENTS = 64
FNV_SEED = 2166136261
FNV_PRIME = 16777619
VERSION_PATTERN = re.compile(r"^[0-9]+[.][0-9]+[.][0-9]+$")
COMPONENT_KINDS = {"KERNEL", "R4X", "R4L", "R4D", "R4P"}
VALID_PAYLOAD_KINDS = {
    "boot-kernel",
    "system-library",
    "driver",
    "protocol",
    "service",
    "software",
    "font",
    "config",
    "sdk",
    "data",
}


class R4UError(ValueError):
    pass


@dataclass(frozen=True)
class PayloadMetadata:
    index: int
    name: str
    target: str
    kind: str
    size: int
    checksum: int
    offset: int


@dataclass(frozen=True)
class ComponentMetadata:
    payload: int
    kind: str
    name: str
    target: str
    version: str
    install: str


@dataclass(frozen=True)
class RequirementMetadata:
    kind: str
    name: str
    target: str
    version: str
    state: str


@dataclass(frozen=True)
class PackageMetadata:
    manifest_schema: int
    package: str
    package_version: str
    release: str
    title: str
    description: str
    activation: str
    priority: str
    reboot_required: bool
    payload_count: int
    component_count: int
    requirement_count: int
    payload_bytes: int
    size: int
    sha256: str
    package_checksum: int
    payloads: tuple[PayloadMetadata, ...]
    components: tuple[ComponentMetadata, ...]
    requirements: tuple[RequirementMetadata, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def fnv_update(value: int, data: bytes) -> int:
    result = value
    for byte in data:
        result ^= byte
        result = (result * FNV_PRIME) & 0xFFFFFFFF
    return result


def compare_versions(left: str, right: str) -> int:
    if not _valid_version(left) or not _valid_version(right):
        raise R4UError("Invalid version")
    left_parts = tuple(int(value) for value in left.split("."))
    right_parts = tuple(int(value) for value in right.split("."))
    return (left_parts > right_parts) - (left_parts < right_parts)


def immutable_package_filename(metadata: PackageMetadata | dict[str, object]) -> str:
    if isinstance(metadata, PackageMetadata):
        package = metadata.package
        package_version = metadata.package_version
        release = metadata.release
    else:
        package = str(metadata["package"])
        package_version = str(metadata["package_version"])
        release = str(metadata["release"])
    return f"{package}-{package_version}-{release}.R4U"


def _read_exact(source: BinaryIO, length: int) -> bytes:
    data = source.read(length)
    if len(data) != length:
        raise R4UError("Unexpected end of file")
    return data


def _read_at(source: BinaryIO, base: int, size: int, offset: int, length: int) -> bytes:
    if offset < 0 or length < 0 or offset > size or length > size - offset:
        raise R4UError("Artefact range is outside the payload")
    source.seek(base + offset)
    return _read_exact(source, length)


def _valid_token(value: str, maximum: int = 48) -> bool:
    if not value or len(value.encode("utf-8")) > maximum:
        return False
    return all(0x20 < ord(character) < 0x7F and character not in ";|=" for character in value)


def _valid_version(value: str) -> bool:
    if not 0 < len(value) <= 24 or VERSION_PATTERN.fullmatch(value) is None:
        return False
    for part in value.split("."):
        if len(part) > 1 and part[0] == "0":
            return False
        try:
            if int(part) > 0xFFFFFFFF:
                return False
        except ValueError:
            return False
    return True


def _valid_display_text(value: str, maximum: int) -> bool:
    if not value or len(value.encode("utf-8")) > maximum:
        return False
    return all(
        character not in "<>\x00"
        and ord(character) >= 0x20
        and not 0x7F <= ord(character) <= 0x9F
        for character in value
    )


def _valid_target(value: str) -> bool:
    if not value or len(value) > 1023 or any(ord(character) >= 0x80 for character in value):
        return False
    drive_absolute = len(value) >= 3 and value[0].isalpha() and value[1] == ":" and value[2] in "\\/"
    if not drive_absolute and value[0] not in "\\/":
        return False
    components = re.split(r"[\\/]+", value[3:] if drive_absolute else value[1:])
    forbidden = set('\"*:;<>?|')
    for component in components:
        if not component:
            continue
        if component in {".", ".."} or component[-1] in {" ", "."}:
            return False
        if any(ord(character) < 0x20 or ord(character) == 0x7F or character in forbidden for character in component):
            return False
    return True


def _canonical_target(value: str) -> str:
    if not _valid_target(value):
        raise R4UError("Invalid target path")
    if len(value) >= 3 and value[1] == ":":
        if value[0].upper() != "C":
            raise R4UError("Component targets must be on the system drive")
        value = value[2:]
    normalized = re.sub(r"[\\/]+", "/", value)
    if not normalized.startswith("/") or normalized.endswith("/"):
        raise R4UError("Invalid canonical target path")
    return normalized


def _target_key(value: str) -> str:
    return value.replace("\\", "/").upper()


def _kind_from_target(target: str) -> str:
    normalized = target.replace("/", "\\").upper()
    if normalized == "\\BOOT\\R4OS.ELF":
        return "boot-kernel"
    if normalized == "C:\\CONFIG.R4S":
        return "config"
    mappings = (
        ("C:\\R4OS\\LIBS\\", ".R4L", "system-library"),
        ("C:\\R4OS\\DRIVERS\\", ".R4D", "driver"),
        ("C:\\R4OS\\PROTOCOLS\\", ".R4P", "protocol"),
        ("C:\\R4OS\\SERVICES\\", ".R4X", "service"),
        ("C:\\R4OS\\FONTS\\", ".R4F", "font"),
    )
    for prefix, suffix, kind in mappings:
        if normalized.startswith(prefix) and normalized.endswith(suffix):
            return kind
    if normalized.startswith("C:\\R4OS\\SOFTWARE\\") and normalized.endswith(".R4X"):
        return "software"
    if normalized.startswith("C:\\R4OS\\CONFIG\\"):
        return "config"
    if normalized.startswith("C:\\R4OS\\SDK\\"):
        return "sdk"
    if len(normalized) >= 4 and normalized[0].isalpha() and normalized[0] != "C" and normalized[1:3] == ":\\":
        return "data"
    return "unknown"


def _component_kind_for_payload(kind: str, target: str) -> str | None:
    if kind == "boot-kernel":
        return "KERNEL"
    if kind == "system-library":
        return "R4L"
    if kind == "driver":
        return "R4D"
    if kind == "protocol":
        return "R4P"
    if kind in {"service", "software"} and target.upper().endswith(".R4X"):
        return "R4X"
    return None


def _install_mode(kind: str, target: str) -> str:
    if kind in {"KERNEL", "R4L", "R4D", "R4P"}:
        return "restart"
    return "restart" if _target_key(target).startswith("/R4OS/SERVICES/") else "live"


def _priority(kind: str) -> str:
    return "foundation" if kind in {"KERNEL", "R4L"} else "normal"


def _fields(line: str, prefix: str, expected: set[str]) -> dict[str, str]:
    if not line.startswith(prefix):
        raise R4UError(f"Manifest line expects {prefix}")
    result: dict[str, str] = {}
    for item in line[len(prefix) :].split(";"):
        if "=" not in item:
            raise R4UError("Manifest field has no equals sign")
        key, value = item.split("=", 1)
        if not key or key in result:
            raise R4UError("Duplicate or empty manifest field")
        result[key] = value
    if set(result) != expected:
        raise R4UError(f"Falsche Felder in {prefix[:-1]}")
    return result


def _parse_number(value: str, maximum: int, label: str) -> int:
    if not value or not value.isascii() or not value.isdigit():
        raise R4UError(f"Invalid numeric field: {label}")
    number = int(value, 10)
    if number > maximum:
        raise R4UError(f"Numeric field is outside the contract: {label}")
    return number


def _parse_manifest(
    manifest: bytes,
    payload_len: int,
    payload_count: int,
    flags: int,
) -> tuple[
    dict[str, str],
    list[PayloadMetadata],
    list[ComponentMetadata],
    list[RequirementMetadata],
]:
    try:
        text = manifest.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise R4UError("Manifest is not valid UTF-8") from error
    if "\x00" in text:
        raise R4UError("Manifest contains NUL")

    scalar: dict[str, str] = {}
    payloads: list[PayloadMetadata] = []
    components: list[ComponentMetadata] = []
    requirements: list[RequirementMetadata] = []
    rollbacks: list[dict[str, str]] = []
    saw_magic = False
    saw_abi = False
    scalar_names = {
        "PACKAGE",
        "PACKAGE_VERSION",
        "RELEASE",
        "TITLE",
        "DESCRIPTION",
        "ACTIVATION",
        "PRIORITY",
        "PAYLOADS",
        "COMPONENTS",
        "REQUIRES",
    }
    for line in text.splitlines():
        if not line:
            continue
        if line == "R4U_MANIFEST=2":
            if saw_magic:
                raise R4UError("Duplicate manifest identifier")
            saw_magic = True
            continue
        if line.startswith("ABI;"):
            if saw_abi:
                raise R4UError("Duplicate ABI line")
            abi = _fields(line, "ABI;", {"R4M0", "R4L", "R4D", "R4P", "R4XSTART", "R4U_COMPONENTS"})
            if any(value != "1" for value in abi.values()):
                raise R4UError("Unsupported ABI contract")
            saw_abi = True
            continue
        if line.startswith("PAYLOAD;"):
            values = _fields(line, "PAYLOAD;", {"index", "name", "target", "kind", "size", "checksum", "offset", "abi"})
            index = _parse_number(values["index"], MAX_PAYLOADS - 1, "payload.index")
            size = _parse_number(values["size"], payload_len, "payload.size")
            checksum = _parse_number(values["checksum"], 0xFFFFFFFF, "payload.checksum")
            offset = _parse_number(values["offset"], payload_len, "payload.offset")
            if values["kind"] not in VALID_PAYLOAD_KINDS or values["abi"] != "R4M0:1":
                raise R4UError("Invalid payload type or ABI")
            if not _valid_token(values["name"]) or not _valid_target(values["target"]):
                raise R4UError("Invalid payload name or target path")
            if _kind_from_target(values["target"]) != values["kind"]:
                raise R4UError("Payload type does not match the target path")
            if _target_key(values["target"]) in {
                "C:/R4OS/CONFIG/VERSION.R4S",
                "C:/R4OS/CONFIG/MODULES.JSON",
            }:
                raise R4UError("Managed release and inventory files are not valid payloads")
            if offset > payload_len or size > payload_len - offset:
                raise R4UError("Payload is outside the package range")
            payloads.append(PayloadMetadata(index, values["name"], values["target"], values["kind"], size, checksum, offset))
            continue
        if line.startswith("ROLLBACK;"):
            values = _fields(line, "ROLLBACK;", {"target", "backup", "strategy"})
            if values["strategy"] != "replace" or not _valid_target(values["target"]) or not _valid_target(values["backup"]):
                raise R4UError("Invalid rollback line")
            rollbacks.append(values)
            continue
        if line.startswith("COMPONENT;"):
            values = _fields(line, "COMPONENT;", {"payload", "kind", "name", "target", "version", "install"})
            component = ComponentMetadata(
                payload=_parse_number(values["payload"], MAX_PAYLOADS - 1, "component.payload"),
                kind=values["kind"].upper(),
                name=values["name"],
                target=values["target"],
                version=values["version"],
                install=values["install"],
            )
            if component.kind not in COMPONENT_KINDS or not _valid_token(component.name) or not _valid_version(component.version):
                raise R4UError("Invalid component identity")
            if _canonical_target(component.target) != component.target:
                raise R4UError("Component target is not canonical")
            if component.install != _install_mode(component.kind, component.target):
                raise R4UError("Installation type does not match the component")
            components.append(component)
            continue
        if line.startswith("REQUIRE;"):
            values = _fields(line, "REQUIRE;", {"kind", "name", "target", "version", "state"})
            requirement = RequirementMetadata(
                kind=values["kind"].upper(),
                name=values["name"],
                target=values["target"],
                version=values["version"],
                state=values["state"],
            )
            if requirement.kind not in COMPONENT_KINDS or not _valid_token(requirement.name) or not _valid_version(requirement.version):
                raise R4UError("Invalid requirement")
            if _canonical_target(requirement.target) != requirement.target or requirement.state not in {"installed", "active"}:
                raise R4UError("Invalid requirement target or state")
            if requirement.state == "active" and requirement.kind != "KERNEL":
                raise R4UError("Only the kernel may be used as an active requirement")
            requirements.append(requirement)
            continue
        if "=" in line:
            key, value = line.split("=", 1)
            if key not in scalar_names or key in scalar:
                raise R4UError("Unknown or duplicate manifest field")
            scalar[key] = value
            continue
        raise R4UError("Unknown manifest line")

    if not saw_magic or not saw_abi or set(scalar) != scalar_names:
        raise R4UError("Required manifest fields are missing")
    if not _valid_token(scalar["PACKAGE"]):
        raise R4UError("Invalid package name")
    if not _valid_version(scalar["PACKAGE_VERSION"]) or not _valid_version(scalar["RELEASE"]):
        raise R4UError("Invalid version")
    if not _valid_display_text(scalar["TITLE"], 96) or not _valid_display_text(scalar["DESCRIPTION"], 2048):
        raise R4UError("Invalid title or description")
    if scalar["ACTIVATION"] not in {"live", "restart"} or scalar["PRIORITY"] not in {"normal", "foundation"}:
        raise R4UError("Invalid activation or priority class")
    manifest_payload_count = _parse_number(scalar["PAYLOADS"], MAX_PAYLOADS, "payloads")
    manifest_component_count = _parse_number(scalar["COMPONENTS"], MAX_COMPONENTS, "components")
    manifest_requirement_count = _parse_number(scalar["REQUIRES"], MAX_REQUIREMENTS, "requires")
    if manifest_payload_count != payload_count or len(payloads) != payload_count or len(rollbacks) != payload_count:
        raise R4UError("Payload count does not match")
    if len(components) != manifest_component_count or len(requirements) != manifest_requirement_count:
        raise R4UError("Component or requirement count does not match")
    if not components and not requirements:
        raise R4UError("Unversioned payloads require a specific requirement")

    if sorted(item.index for item in payloads) != list(range(payload_count)):
        raise R4UError("Payload indices are not unique and contiguous")
    cursor = 0
    for payload in sorted(payloads, key=lambda item: item.offset):
        if payload.offset != cursor:
            raise R4UError("Payload range contains gaps or overlaps")
        cursor += payload.size
    if cursor != payload_len:
        raise R4UError("Payload range is not fully described")
    rollback_targets = [_target_key(item["target"]) for item in rollbacks]
    if len(set(rollback_targets)) != len(rollback_targets) or set(rollback_targets) != {_target_key(item.target) for item in payloads}:
        raise R4UError("Rollback targets do not match the payloads")

    payload_by_index = {item.index: item for item in payloads}
    component_payloads: set[int] = set()
    component_targets: set[str] = set()
    component_identities: set[tuple[str, str]] = set()
    for component in components:
        payload = payload_by_index.get(component.payload)
        if payload is None or component.payload in component_payloads:
            raise R4UError("Component payload is invalid or duplicated")
        canonical = _canonical_target(payload.target)
        if _target_key(canonical) != _target_key(component.target):
            raise R4UError("Component target does not match the payload")
        if _component_kind_for_payload(payload.kind, payload.target) != component.kind:
            raise R4UError("Component type does not match the payload")
        target_key = _target_key(component.target)
        identity_key = (component.kind, component.name.upper())
        if target_key in component_targets or identity_key in component_identities:
            raise R4UError("Duplicate component")
        component_payloads.add(component.payload)
        component_targets.add(target_key)
        component_identities.add(identity_key)
    requirement_keys: set[tuple[str, str, str, str]] = set()
    for requirement in requirements:
        key = (requirement.kind, requirement.name.upper(), _target_key(requirement.target), requirement.state)
        if key in requirement_keys:
            raise R4UError("Duplicate requirement")
        requirement_keys.add(key)

    activation = "restart" if any(item.install == "restart" for item in components) else "live"
    priority = "foundation" if any(_priority(item.kind) == "foundation" for item in components) else "normal"
    if scalar["ACTIVATION"] != activation or scalar["PRIORITY"] != priority:
        raise R4UError("Derived activation or priority class does not match")
    if bool(flags & 1) != (activation == "restart"):
        raise R4UError("Reboot flag does not match activation")
    return scalar, payloads, components, requirements


def _zero_terminated(data: bytes, start: int, limit: int = 256) -> bytes:
    end = data.find(b"\x00", start)
    if end < 0 or end - start > limit:
        raise R4UError("Invalid null-terminated artefact text")
    return data[start:end]


def _inspect_r4m0(source: BinaryIO, base: int, size: int) -> tuple[str, str, str]:
    header = _read_at(source, base, size, 0, 64)
    if header[0:4] != b"R4M0" or struct.unpack_from("<H", header, 4)[0] != 1 or struct.unpack_from("<H", header, 10)[0] != 64:
        raise R4UError("Invalid R4M0 component header")
    kind_value = struct.unpack_from("<H", header, 8)[0]
    kind = {1: "R4X", 2: "R4L", 3: "R4D", 4: "R4P"}.get(kind_value)
    if kind is None:
        raise R4UError("Invalid R4M0 component type")
    string_offset, string_length = struct.unpack_from("<II", header, 56)
    if string_length < 1 or string_length > 1024 * 1024:
        raise R4UError("Invalid R4M0 metadata range")
    strings = _read_at(source, base, size, string_offset, string_length)
    if not strings.endswith(b"\x00"):
        raise R4UError("R4M0 metadata is incomplete")
    entries = strings[:-1].split(b"\x00")
    if any(len(item) > 256 for item in entries):
        raise R4UError("R4M0 metadata entry is too long")
    try:
        texts = [item.decode("ascii", errors="strict") for item in entries]
    except UnicodeDecodeError as error:
        raise R4UError("R4M0 metadata is not ASCII") from error
    first_name = next((item for item in texts if item), "")
    if kind == "R4D" and first_name.startswith("R4D_"):
        first_name = first_name[4:]
    if kind == "R4P" and first_name.startswith("R4P_"):
        first_name = first_name[4:]
    name_prefix = {"R4X": "r4x.name=", "R4D": "r4d.name=", "R4P": "r4p.name="}.get(kind)
    names = [item[len(name_prefix) :] for item in texts if name_prefix and item.startswith(name_prefix)]
    versions = [item[len("module.version=") :] for item in texts if item.startswith("module.version=")]
    if len(names) > 1 or len(versions) != 1:
        raise R4UError("R4M0 component identity is ambiguous")
    name = names[0] if names else first_name
    version = versions[0]
    if not _valid_token(name) or not _valid_version(version):
        raise R4UError("Invalid R4M0 component identity")
    return kind, name, version


def _inspect_kernel(source: BinaryIO, base: int, size: int) -> tuple[str, str, str]:
    header = _read_at(source, base, size, 0, 64)
    if header[0:4] != b"\x7fELF" or header[4] != 2 or header[5] != 1 or struct.unpack_from("<H", header, 18)[0] != 62:
        raise R4UError("Invalid kernel ELF header")
    section_offset = struct.unpack_from("<Q", header, 40)[0]
    section_size, section_count, names_index = struct.unpack_from("<HHH", header, 58)
    if section_size < 64 or not 0 < section_count <= 4096 or names_index >= section_count:
        raise R4UError("Invalid kernel ELF section table")
    names_header = _read_at(source, base, size, section_offset + names_index * section_size, 64)
    names_offset, names_size = struct.unpack_from("<QQ", names_header, 24)
    if names_size < 1 or names_size > 1024 * 1024:
        raise R4UError("Invalid kernel ELF section names")
    names = _read_at(source, base, size, names_offset, names_size)
    metadata: bytes | None = None
    for index in range(section_count):
        section = _read_at(source, base, size, section_offset + index * section_size, 64)
        name_offset = struct.unpack_from("<I", section, 0)[0]
        if name_offset >= names_size:
            raise R4UError("Invalid kernel ELF section name")
        name = _zero_terminated(names, name_offset, 64)
        if name != b".r4os.kernel.meta":
            continue
        if metadata is not None or struct.unpack_from("<Q", section, 32)[0] != 44:
            raise R4UError("Kernel metadata is duplicated or has invalid dimensions")
        metadata_offset = struct.unpack_from("<Q", section, 24)[0]
        metadata = _read_at(source, base, size, metadata_offset, 44)
    if metadata is None or metadata[0:8] != b"R4OSKRN1" or struct.unpack_from("<II", metadata, 8) != (1, 44):
        raise R4UError("Kernel metadata is missing or invalid")
    version_bytes = _zero_terminated(metadata, 28, 15)
    try:
        version = version_bytes.decode("ascii", errors="strict")
    except UnicodeDecodeError as error:
        raise R4UError("Kernel version is not ASCII") from error
    if not _valid_version(version):
        raise R4UError("Invalid kernel version")
    if tuple(int(value) for value in version.split(".")) != struct.unpack_from("<III", metadata, 16):
        raise R4UError("Kernel version contradicts the numeric metadata")
    return "KERNEL", "KERNEL", version


def _inspect_component(source: BinaryIO, base: int, size: int) -> tuple[str, str, str]:
    magic = _read_at(source, base, size, 0, 4)
    if magic == b"R4M0":
        return _inspect_r4m0(source, base, size)
    if magic == b"\x7fELF":
        return _inspect_kernel(source, base, size)
    raise R4UError("Component payload is neither R4M0 nor kernel ELF")


def inspect_package(path: str | Path) -> PackageMetadata:
    package_path = Path(path)
    file_size = package_path.stat().st_size
    if file_size < HEADER_SIZE:
        raise R4UError("Package is smaller than the header")

    sha256 = hashlib.sha256()
    with package_path.open("rb") as source:
        header = _read_exact(source, HEADER_SIZE)
        sha256.update(header)
        magic, format_version, header_size, manifest_len, payload_len, manifest_checksum, payload_checksum, package_checksum, payload_count, flags = struct.unpack_from(
            "<4sHHQQIIIII", header, 0
        )
        if magic != b"R4U2" or format_version != 2 or header_size != HEADER_SIZE:
            raise R4UError("Invalid or outdated R4U header")
        if flags & ~1 or any(header[44:]):
            raise R4UError("Invalid header flags or reserved bytes")
        if manifest_len < 1 or manifest_len > MANIFEST_MAX or payload_count < 1 or payload_count > MAX_PAYLOADS:
            raise R4UError("Manifest or payload count is outside the contract")
        if HEADER_SIZE + manifest_len + payload_len != file_size:
            raise R4UError("Package length does not match the header")

        manifest = _read_exact(source, manifest_len)
        sha256.update(manifest)
        if fnv_update(FNV_SEED, manifest) != manifest_checksum:
            raise R4UError("Manifest FNV does not match")
        scalar, payloads, components, requirements = _parse_manifest(manifest, payload_len, payload_count, flags)

        payload_hash = FNV_SEED
        complete_hash = fnv_update(FNV_SEED, manifest)
        remaining = payload_len
        while remaining:
            chunk = _read_exact(source, min(1024 * 1024, remaining))
            remaining -= len(chunk)
            sha256.update(chunk)
            payload_hash = fnv_update(payload_hash, chunk)
            complete_hash = fnv_update(complete_hash, chunk)
        if payload_hash != payload_checksum:
            raise R4UError("Payload FNV does not match")
        if complete_hash != package_checksum:
            raise R4UError("Package FNV does not match")

        payload_base = HEADER_SIZE + manifest_len
        payload_by_index = {item.index: item for item in payloads}
        for payload in payloads:
            source.seek(payload_base + payload.offset)
            remaining = payload.size
            checksum = FNV_SEED
            while remaining:
                chunk = _read_exact(source, min(1024 * 1024, remaining))
                remaining -= len(chunk)
                checksum = fnv_update(checksum, chunk)
            if checksum != payload.checksum:
                raise R4UError(f"Payload FNV does not match: {payload.name}")
        for component in components:
            payload = payload_by_index[component.payload]
            identity = _inspect_component(source, payload_base + payload.offset, payload.size)
            if identity[0] != component.kind or identity[1].upper() != component.name.upper() or identity[2] != component.version:
                raise R4UError(f"The artefact identity contradicts the component: {component.name}")

    return PackageMetadata(
        manifest_schema=2,
        package=scalar["PACKAGE"],
        package_version=scalar["PACKAGE_VERSION"],
        release=scalar["RELEASE"],
        title=scalar["TITLE"],
        description=scalar["DESCRIPTION"],
        activation=scalar["ACTIVATION"],
        priority=scalar["PRIORITY"],
        reboot_required=bool(flags & 1),
        payload_count=payload_count,
        component_count=len(components),
        requirement_count=len(requirements),
        payload_bytes=payload_len,
        size=file_size,
        sha256=sha256.hexdigest(),
        package_checksum=package_checksum,
        payloads=tuple(payloads),
        components=tuple(components),
        requirements=tuple(requirements),
    )
