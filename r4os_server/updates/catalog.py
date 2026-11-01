from __future__ import annotations

from dataclasses import dataclass
from contextlib import contextmanager
from functools import wraps
import fcntl
import os
from pathlib import Path
import time
import threading
from typing import Any, Callable
from urllib.parse import quote

from .r4u import R4UError, compare_versions, immutable_package_filename, inspect_package
from .release_manifest import (
    ReleaseManifestError,
    immutable_release_filename,
    inspect_release_manifest,
)
from .state import PackageCache, StateStore, utc_text


def exclusive(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with self.mutation():
            return method(self, *args, **kwargs)
    return call


@dataclass(frozen=True)
class PackageEntry:
    state: str
    filename: str
    size: int
    mtime_ns: int
    metadata: dict[str, Any] | None
    error: str | None
    published_at: float | None

    @property
    def valid(self) -> bool:
        return self.metadata is not None and self.error is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "filename": self.filename,
            "size": self.size,
            "valid": self.valid,
            "error": self.error,
            "published_at": utc_text(self.published_at),
            "metadata": self.metadata,
        }


class Catalog:
    def __init__(self, store: StateStore, incoming_root: str | Path, release_root: str | Path):
        self.store = store
        self.incoming_root = Path(incoming_root)
        self.release_root = Path(release_root)
        self.incoming_root.mkdir(parents=True, exist_ok=True)
        self.release_root.mkdir(parents=True, exist_ok=True)
        self.base_url = None
        self._lock = threading.RLock()
        self._local = threading.local()

    @contextmanager
    def mutation(self):
        with self._lock:
            depth = getattr(self._local, "depth", 0)
            lock = None
            if not depth:
                lock = (self.release_root.parent / ".publication.lock").open("a+b")
                fcntl.flock(lock, fcntl.LOCK_EX)
            self._local.depth = depth + 1
            try:
                yield
            finally:
                self._local.depth = depth
                if lock is not None:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                    lock.close()

    @staticmethod
    def _safe_package_filename(filename: str) -> str:
        if not filename or Path(filename).name != filename or not filename.upper().endswith(".R4U"):
            raise ValueError("Invalid R4U filename")
        if any(ord(character) < 0x20 or character in '<>:"/\\|?*' for character in filename):
            raise ValueError("Invalid R4U filename")
        return filename

    @staticmethod
    def _safe_release_filename(filename: str) -> str:
        if not filename or Path(filename).name != filename or not filename.upper().endswith(".JSON"):
            raise ValueError("Invalid release manifest filename")
        if any(ord(character) < 0x20 or character in '<>:"/\\|?*' for character in filename):
            raise ValueError("Invalid release manifest filename")
        return filename

    def _scan_root(
        self,
        label: str,
        cache_state: str,
        root: Path,
        include: Callable[[str], bool],
        inspect: Callable[[Path], object],
    ) -> list[PackageEntry]:
        filenames = {item.name for item in root.iterdir() if item.is_file() and not item.is_symlink() and include(item.name)}
        entries: list[PackageEntry] = []
        for filename in sorted(filenames, key=str.casefold):
            path = root / filename
            stat = path.stat()
            cached = self.store.package_cache_get(cache_state, filename)
            if cached is not None and cached.size == stat.st_size and cached.mtime_ns == stat.st_mtime_ns:
                entries.append(PackageEntry(label, filename, cached.size, cached.mtime_ns, cached.metadata, cached.error, cached.published_at))
                continue
            try:
                metadata = inspect(path).to_dict()  # type: ignore[attr-defined]
                error = None
            except (OSError, R4UError, ReleaseManifestError) as failure:
                metadata = None
                error = str(failure)
            published_at = cached.published_at if cached is not None else (stat.st_mtime if label == "released" else None)
            record = PackageCache(cache_state, filename, stat.st_size, stat.st_mtime_ns, metadata, error, published_at)
            self.store.package_cache_put(record)
            entries.append(PackageEntry(label, filename, record.size, record.mtime_ns, record.metadata, record.error, record.published_at))
        self.store.package_cache_delete_missing(cache_state, filenames)
        return entries

    def _scan_packages(self, label: str, root: Path) -> list[PackageEntry]:
        return self._scan_root(label, f"r4u2-{label}", root, lambda name: name.upper().endswith(".R4U"), inspect_package)

    def _scan_release_manifests(self, label: str, root: Path) -> list[PackageEntry]:
        return self._scan_root(
            label,
            f"release1-{label}",
            root,
            lambda name: name.upper().startswith("R4OS-RELEASE-") and name.upper().endswith(".JSON"),
            inspect_release_manifest,
        )

    def list_incoming(self) -> list[PackageEntry]:
        return self._scan_packages("incoming", self.incoming_root)

    def list_released(self) -> list[PackageEntry]:
        return self._scan_packages("released", self.release_root)

    def list_incoming_release_manifests(self) -> list[PackageEntry]:
        return self._scan_release_manifests("incoming", self.incoming_root)

    def list_released_release_manifests(self) -> list[PackageEntry]:
        return self._scan_release_manifests("released", self.release_root)

    @staticmethod
    def _entry_from_metadata(filename: str, size: int, metadata: dict[str, Any]) -> PackageEntry:
        return PackageEntry("candidate", filename, size, 0, metadata, None, None)

    @staticmethod
    def _component_key(component: dict[str, Any]) -> tuple[str, str, str]:
        return str(component["kind"]).upper(), str(component["name"]).upper(), str(component["target"]).replace("\\", "/").upper()

    @classmethod
    def _validate_release(
        cls,
        manifest: PackageEntry,
        packages: list[PackageEntry],
    ) -> list[PackageEntry]:
        if not manifest.valid or manifest.metadata is None:
            raise ValueError(manifest.error or "Invalid release manifest")
        release = str(manifest.metadata["release"])
        if manifest.filename != immutable_release_filename(release):
            raise ValueError("Release manifest filename is not immutably bound")
        selected = [entry for entry in packages if entry.valid and entry.metadata is not None and entry.metadata.get("release") == release]
        if not selected:
            raise ValueError("The release manifest has no published installation package")

        package_ids: set[str] = set()
        offered: dict[tuple[str, str, str], tuple[int, dict[str, Any]]] = {}
        profiles = manifest.metadata["profiles"]
        release_components: dict[tuple[str, str, str], dict[str, Any]] = {}
        for profile in profiles.values():
            for component in profile["required"]:
                release_components[cls._component_key(component)] = component
        for index, entry in enumerate(selected):
            metadata = entry.metadata
            assert metadata is not None
            if entry.filename != immutable_package_filename(metadata):
                raise ValueError(f"Package filename is not immutably bound: {entry.filename}")
            package_id = str(metadata["package"]).upper()
            if package_id in package_ids:
                raise ValueError(f"Duplicate package ID in release: {package_id}")
            package_ids.add(package_id)
            for component in metadata["components"]:
                key = cls._component_key(component)
                if key in offered:
                    raise ValueError(f"Component provided by multiple packages: {component['name']}")
                target_component = release_components.get(key)
                if target_component is None or target_component["version"] != component["version"]:
                    raise ValueError(f"Package component contradicts the release manifest: {component['name']}")
                offered[key] = index, component

        edges: list[set[int]] = [set() for _ in selected]
        indegree = [0] * len(selected)
        for consumer_index, entry in enumerate(selected):
            metadata = entry.metadata
            assert metadata is not None
            for requirement in metadata["requirements"]:
                key = cls._component_key(requirement)
                release_component = release_components.get(key)
                if release_component is None or compare_versions(str(release_component["version"]), str(requirement["version"])) < 0:
                    raise ValueError(f"Unresolvable specific requirement: {requirement['name']}")
                provider = offered.get(key)
                if provider is None or provider[0] == consumer_index:
                    continue
                if compare_versions(str(provider[1]["version"]), str(requirement["version"])) < 0:
                    raise ValueError(f"The supplied provider is too old: {requirement['name']}")
                provider_index = provider[0]
                if consumer_index not in edges[provider_index]:
                    edges[provider_index].add(consumer_index)
                    indegree[consumer_index] += 1

        order: list[int] = []
        while len(order) < len(selected):
            ready = [index for index, degree in enumerate(indegree) if degree == 0 and index not in order]
            if not ready:
                raise ValueError("Cyclic package dependency")
            ready.sort(
                key=lambda index: (
                    0 if selected[index].metadata and selected[index].metadata["priority"] == "foundation" else 1,
                    selected[index].filename.casefold(),
                )
            )
            current = ready[0]
            order.append(current)
            for consumer in edges[current]:
                indegree[consumer] -= 1
        return [selected[index] for index in order]

    def _replace(self, source: Path, destination: Path, cache_state: str, metadata: dict[str, Any]) -> PackageEntry:
        with source.open("r+b") as artifact_file:
            os.fsync(artifact_file.fileno())
        # Same-filesystem hard-link creation fails atomically if a name exists.
        os.link(source, destination, follow_symlinks=False)
        source.unlink()
        if os.name != "nt":
            for directory in (self.incoming_root, self.release_root):
                descriptor = os.open(directory, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        stat = destination.stat()
        published_at = time.time()
        record = PackageCache(cache_state, destination.name, stat.st_size, stat.st_mtime_ns, metadata, None, published_at)
        self.store.package_cache_put(record)
        return PackageEntry("released", destination.name, record.size, record.mtime_ns, record.metadata, None, published_at)

    @exclusive
    def publish(self, filename: str) -> PackageEntry:
        safe_name = self._safe_package_filename(filename)
        source = self.incoming_root / safe_name
        destination = self.release_root / safe_name
        if not source.is_file():
            raise FileNotFoundError(safe_name)
        if destination.exists():
            raise FileExistsError(safe_name)
        metadata = inspect_package(source).to_dict()
        if safe_name != immutable_package_filename(metadata):
            raise ValueError("R4U filename must match PACKAGE-PACKAGE_VERSION-RELEASE.R4U")
        existing = self.list_released()
        same_release = [item for item in existing if item.metadata and item.metadata.get("release") == metadata["release"]]
        if any(item.metadata and str(item.metadata["package"]).upper() == str(metadata["package"]).upper() for item in same_release):
            raise ValueError("Package ID is already published for this release")
        manifests = [item for item in self.list_released_release_manifests() if item.metadata and item.metadata.get("release") == metadata["release"]]
        if manifests:
            raise ValueError("Release is already completed by its release manifest")
        result = self._replace(source, destination, "r4u2-released", metadata)
        self.store.package_cache_delete("r4u2-incoming", safe_name)
        return result

    @exclusive
    def publish_release_manifest(self, filename: str) -> PackageEntry:
        safe_name = self._safe_release_filename(filename)
        source = self.incoming_root / safe_name
        destination = self.release_root / safe_name
        if not source.is_file():
            raise FileNotFoundError(safe_name)
        if destination.exists():
            raise FileExistsError(safe_name)
        metadata = inspect_release_manifest(source).to_dict()
        if safe_name != immutable_release_filename(metadata):
            raise ValueError("Release manifest filename must match R4OS-RELEASE-VERSION.JSON")
        if any(item.metadata and item.metadata.get("release") == metadata["release"] for item in self.list_released_release_manifests()):
            raise ValueError("Release is already published")
        candidate = self._entry_from_metadata(safe_name, source.stat().st_size, metadata)
        self._validate_release(candidate, self.list_released())
        result = self._replace(source, destination, "release1-released", metadata)
        self.store.package_cache_delete("release1-incoming", safe_name)
        return result

    def catalog_errors(self) -> list[dict[str, str]]:
        packages = self.list_released()
        errors: list[dict[str, str]] = []
        manifests = self.list_released_release_manifests()
        counts: dict[str, int] = {}
        for manifest in manifests:
            if manifest.metadata:
                release = str(manifest.metadata["release"])
                counts[release] = counts.get(release, 0) + 1
        for manifest in manifests:
            try:
                if manifest.metadata and counts[str(manifest.metadata["release"])] != 1:
                    raise ValueError("Release has multiple release manifests")
                self._validate_release(manifest, packages)
            except ValueError as error:
                errors.append({"filename": manifest.filename, "error": str(error)})
        return errors

    def public_catalog(self, public_port: int = 4023) -> dict[str, object]:
        packages = self.list_released()
        releases: list[dict[str, object]] = []
        manifests = self.list_released_release_manifests()
        counts: dict[str, int] = {}
        for manifest in manifests:
            if manifest.metadata:
                release = str(manifest.metadata["release"])
                counts[release] = counts.get(release, 0) + 1
        for manifest in manifests:
            try:
                if manifest.metadata and counts[str(manifest.metadata["release"])] != 1:
                    raise ValueError("Release has multiple release manifests")
                ordered = self._validate_release(manifest, packages)
            except ValueError:
                continue
            assert manifest.metadata is not None
            package_values: list[dict[str, object]] = []
            for order, entry in enumerate(ordered):
                assert entry.metadata is not None
                metadata = entry.metadata
                package_values.append(
                    {
                        "id": metadata["package"],
                        "package_version": metadata["package_version"],
                        "title": metadata["title"],
                        "description": metadata["description"],
                        "filename": entry.filename,
                        "size": entry.size,
                        "sha256": metadata["sha256"],
                        "reboot_required": metadata["reboot_required"],
                        "activation": metadata["activation"],
                        "priority": metadata["priority"],
                        "install_order": order,
                        "components": metadata["components"],
                        "requirements": metadata["requirements"],
                        "download_url": f"{self.base_url or ('https://127.0.0.1:' + str(public_port))}/packages/{quote(entry.filename)}",
                    }
                )
            releases.append(
                {
                    "release": manifest.metadata["release"],
                    "manifest": {
                        "schema": manifest.metadata["manifest_schema"],
                        "release": manifest.metadata["release"],
                        "profiles": manifest.metadata["profiles"],
                        "filename": manifest.filename,
                        "sha256": manifest.metadata["sha256"],
                    },
                    "packages": package_values,
                }
            )
        releases.sort(key=lambda value: tuple(int(part) for part in str(value["release"]).split(".")))
        # Clients validate every transmitted release but deliberately select
        # only the highest one that is not older than their local state. Older
        # entries can therefore never act as a fallback; transmitting them
        # merely grows the bounded response forever. Keep all immutable files
        # and ControlCenter history, but expose only the newest valid target.
        if releases:
            releases = [releases[-1]]
        return {"schema": 2, "generated_at": utc_text(time.time()), "releases": releases}

    def released_entry(self, filename: str) -> tuple[PackageEntry, Path] | None:
        safe_name = self._safe_package_filename(filename)
        for entry in self.list_released():
            if entry.filename == safe_name and entry.valid:
                return entry, self.release_root / safe_name
        return None
