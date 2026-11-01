from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
from typing import Any

from .r4u import R4UError, _canonical_target, _target_key, _valid_token, _valid_version


MAX_RELEASE_MANIFEST_BYTES = 1024 * 1024
PROFILE_NAMES = ("slim", "full", "test")
COMPONENT_KINDS = {"KERNEL", "R4X", "R4L", "R4D", "R4P"}


class ReleaseManifestError(ValueError):
    pass


@dataclass(frozen=True)
class ReleaseComponent:
    kind: str
    name: str
    target: str
    version: str

    def key(self) -> tuple[str, str, str]:
        return self.kind, self.name.upper(), _target_key(self.target)


@dataclass(frozen=True)
class ReleaseProfile:
    count: int
    required: tuple[ReleaseComponent, ...]


@dataclass(frozen=True)
class ReleaseManifestMetadata:
    manifest_schema: int
    release: str
    profiles: dict[str, ReleaseProfile]
    size: int
    sha256: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def immutable_release_filename(metadata: ReleaseManifestMetadata | dict[str, object] | str) -> str:
    if isinstance(metadata, str):
        release = metadata
    elif isinstance(metadata, ReleaseManifestMetadata):
        release = metadata.release
    else:
        release = str(metadata["release"])
    return f"R4OS-RELEASE-{release}.JSON"


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseManifestError(f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def _component(value: object) -> ReleaseComponent:
    if not isinstance(value, dict) or set(value) != {"kind", "name", "target", "version"}:
        raise ReleaseManifestError("Invalid component object contract")
    if not all(isinstance(value[key], str) for key in value):
        raise ReleaseManifestError("Component fields must be text")
    kind = str(value["kind"]).upper()
    name = str(value["name"])
    target = str(value["target"])
    version = str(value["version"])
    if kind not in COMPONENT_KINDS or not _valid_token(name) or not _valid_version(version):
        raise ReleaseManifestError("Invalid component identity in release manifest")
    try:
        if _canonical_target(target) != target:
            raise ReleaseManifestError("Component target in the release manifest is not canonical")
    except R4UError as error:
        raise ReleaseManifestError(str(error)) from error
    upper = target.upper()
    valid_target = {
        "KERNEL": name.upper() == "KERNEL" and upper == "/BOOT/R4OS.ELF",
        "R4X": upper.endswith(".R4X"),
        "R4L": upper.startswith("/R4OS/LIBS/") and upper.endswith(".R4L"),
        "R4D": upper.startswith("/R4OS/DRIVERS/") and upper.endswith(".R4D"),
        "R4P": upper.startswith("/R4OS/PROTOCOLS/") and upper.endswith(".R4P"),
    }[kind]
    if not valid_target:
        raise ReleaseManifestError("Component type does not match the release manifest target")
    return ReleaseComponent(kind, name, target, version)


def _profile(name: str, value: object) -> ReleaseProfile:
    if not isinstance(value, dict) or set(value) != {"count", "required"}:
        raise ReleaseManifestError(f"Invalid profile contract: {name}")
    count = value["count"]
    required = value["required"]
    if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= 512:
        raise ReleaseManifestError(f"Invalid component count: {name}")
    if not isinstance(required, list) or len(required) != count:
        raise ReleaseManifestError(f"Profile count does not match: {name}")
    components = tuple(_component(item) for item in required)
    identities: set[tuple[str, str]] = set()
    targets: set[str] = set()
    for component in components:
        identity = component.kind, component.name.upper()
        target = _target_key(component.target)
        if identity in identities or target in targets:
            raise ReleaseManifestError(f"Duplicate component im Profil: {name}")
        identities.add(identity)
        targets.add(target)
    kernels = [item for item in components if item.kind == "KERNEL"]
    if len(kernels) != 1:
        raise ReleaseManifestError(f"Profile requires exactly one kernel: {name}")
    return ReleaseProfile(count, components)


def inspect_release_manifest(path: str | Path) -> ReleaseManifestMetadata:
    manifest_path = Path(path)
    size = manifest_path.stat().st_size
    if not 1 <= size <= MAX_RELEASE_MANIFEST_BYTES:
        raise ReleaseManifestError("Release manifest is empty or too large")
    raw = manifest_path.read_bytes()
    try:
        text = raw.decode("utf-8-sig", errors="strict")
        value = json.loads(text, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseManifestError("Release manifest is not valid UTF-8 JSON") from error
    if not isinstance(value, dict) or set(value) != {"schema", "release", "profiles"}:
        raise ReleaseManifestError("Unknown release manifest contract")
    if value["schema"] != 1 or isinstance(value["schema"], bool):
        raise ReleaseManifestError("Unsupported release manifest schema")
    release = value["release"]
    if not isinstance(release, str) or not _valid_version(release):
        raise ReleaseManifestError("Invalid release version")
    raw_profiles = value["profiles"]
    if not isinstance(raw_profiles, dict) or set(raw_profiles) != set(PROFILE_NAMES):
        raise ReleaseManifestError("Release manifest requires exactly slim, full and test")
    profiles = {name: _profile(name, raw_profiles[name]) for name in PROFILE_NAMES}

    full = {item.key(): item for item in profiles["full"].required}
    test = {item.key(): item for item in profiles["test"].required}
    for component in profiles["slim"].required:
        if full.get(component.key()) != component or test.get(component.key()) != component:
            raise ReleaseManifestError("A Slim component is missing or changed in Full or Test")
    all_components: dict[tuple[str, str, str], ReleaseComponent] = {}
    for profile in profiles.values():
        for component in profile.required:
            previous = all_components.get(component.key())
            if previous is not None and previous != component:
                raise ReleaseManifestError("Component differs between profiles")
            all_components[component.key()] = component
    return ReleaseManifestMetadata(
        manifest_schema=1,
        release=release,
        profiles=profiles,
        size=size,
        sha256=hashlib.sha256(raw).hexdigest(),
    )
