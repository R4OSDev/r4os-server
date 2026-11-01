"""Pinned, versioned SDK/Contract/toolchain sets; activation never mutates a set."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import tarfile
import time
import urllib.request
import uuid
import zipfile

from . import catalog
from .profiles import package_version
from .store import identifier, now, under

SOURCES = {"DevKit": "r4os-devkit", "Contract": "r4os-contract", "SDK": "r4os-sdk",
           "Libraries": "r4os-libraries", "Distribution": "r4os-distribution", "Project": "r4os-project"}


def digest(path):
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".new-" + uuid.uuid4().hex)
    scratch.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    scratch.replace(path)


def download(context, url, destination, expected=None):
    if not url.startswith("https://"):
        raise ValueError("Tool downloads require HTTPS.")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and expected and digest(destination) == expected:
        return destination
    scratch = destination.with_name(destination.name + ".part")
    context.phase("Downloading: " + destination.name)
    request = urllib.request.Request(url, headers={"User-Agent": "R4OS-Server"})
    try:
        count, last = 0, 0
        with urllib.request.urlopen(request, timeout=60) as response, scratch.open("wb") as target:
            while chunk := response.read(1024 * 1024):
                context.check()
                target.write(chunk)
                count += len(chunk)
                if count > 4 * 1024**3:
                    raise RuntimeError("The installation archive exceeds the size limit.")
                if time.monotonic() - last > 5:
                    context.phase(destination.name + ": " + str(count // 1048576) + " MiB downloaded")
                    last = time.monotonic()
        if expected and digest(scratch) != expected:
            raise RuntimeError("The installation archive SHA-256 does not match the version pin.")
        scratch.replace(destination)
    finally:
        scratch.unlink(missing_ok=True)
    return destination


def clone_snapshot(context, original, destination, commit):
    # Local snapshots still honour a managed source's current access/selection.
    try:
        relative = Path(original).resolve().relative_to((context.store.root / "Repositories").resolve()).as_posix()
    except ValueError:
        relative = None
    if relative:
        with context.store.connect() as db:
            row = db.execute("SELECT name FROM repositories WHERE directory=?", (relative,)).fetchone()
        if row:
            context.store.require_repository(row["name"])
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if (destination / ".git").is_dir():
        head, _ = context.git(destination, "rev-parse", "HEAD")
        dirty, _ = context.git(destination, "status", "--porcelain=v1", "--untracked-files=no")
        if head.strip() != commit or dirty.strip():
            raise ValueError("An existing tool snapshot has been modified: " + str(destination))
        return
    context.run(["git", "clone", "--no-hardlinks", "--no-checkout", "--", original, destination], quiet=True, timeout=300)
    context.git(destination, "checkout", "--detach", commit)
    context.git(destination, "remote", "set-url", "--push", "origin", "disabled://local-only")


def active(store):
    key = store.setting("active_toolset")
    if not key:
        raise ValueError("Build tools are not configured yet. Set them up under Repositories → Tools.")
    path = under(store.root / "BuildTools" / "Toolsets", identifier(key))
    try:
        manifest = json.loads((path / "toolset.json").read_text())
    except (OSError, ValueError):
        raise ValueError("The saved toolset is not available locally. Open Repositories → Tools and set it up again.") from None
    if manifest.get("status") != "ready":
        raise ValueError("The selected toolset is not ready.")
    for source in manifest.get("sources", {}).values():
        repo = store.require_repository(source["repository"])
        if repo["connection_id"] != "main":
            raise ValueError("Build tool sources must belong to R4OS Organisation.")
    return path, manifest


def refresh_profiles(context):
    prepare_metadata_parser(context)
    context.phase("Validating project profiles with the active SDK")
    for repo in context.store.repositories():
        if repo["listed"] and repo["local"].get("commit"):
            catalog.inspect(context, repo["name"], remote=False)


def prepare_metadata_parser(context):
    """Expose canonical manifest metadata before workspace inputs are staged."""
    tools, manifest = active(context.store)
    root = context.store.root / "BuildTools/Metadata" / manifest["id"]
    output = root / "HostTools"
    binary = output / "bin/module-catalog"
    if binary.is_file() and (root / "parser.json").is_file():
        return binary
    source = root / "SDK"
    if source.exists():
        shutil.rmtree(source)
    shutil.copytree(tools / "Repositories/SDK", source, ignore=shutil.ignore_patterns(".git", "zig-out", ".zig-cache"))
    path = source / "Tools/ModuleCatalog/src/main.zig"
    original = path.read_text()
    adapted = original.replace("enum { capabilities,", "enum { metadata, capabilities,", 1)
    adapted = adapted.replace("switch (options.action) {", """switch (options.action) {
        .metadata => {
            const value = try readManifest(arena_allocator, io, cwd, options.manifest_path orelse return error.MissingManifestPath);
            const rendered = try renderEntry(allocator, value, true);
            try writeOutput(io, cwd, options.output, rendered);
        },""", 1)
    adapted = adapted.replace('if (std.mem.eql(u8, args[1], "capabilities"))', 'if (std.mem.eql(u8, args[1], "metadata")) .metadata else if (std.mem.eql(u8, args[1], "capabilities"))', 1)
    if adapted == original or ".metadata =>" not in adapted:
        raise ValueError("The SDK metadata adapter does not match the selected source revision.")
    path.write_text(adapted)
    context.phase("Preparing the canonical SDK metadata parser for multi-repository sources")
    context.run([tools / "DevKit/Toolchains/Zig/zig", "build", "-j2", "-Dcatalog-only=true", "--fork=" + str(tools / "Repositories/Contract"),
                 "--prefix", output, "--cache-dir", context.store.root / "BuildTools/Cache/Zig/Metadata",
                 "--global-cache-dir", context.store.root / "BuildTools/Cache/Zig/Global"], cwd=source, timeout=2400)
    write_json(root / "parser.json", {"toolset": manifest["id"], "source_sha256": hashlib.sha256(original.encode()).hexdigest(), "adapter_sha256": digest(path), "binary_sha256": digest(binary)})
    return binary


def overview(store):
    sets = []
    root = store.root / "BuildTools" / "Toolsets"
    if root.is_dir():
        for path in sorted(root.glob("*/toolset.json")):
            try:
                value = json.loads(path.read_text())
                sets.append(value)
            except (OSError, ValueError):
                continue
    current = next((item for item in sets if item["id"] == store.setting("active_toolset")), None)
    sources = {item["display_name"]: item for item in store.repositories("main") if item["listed"]}
    required = {"pwsh": "PowerShell 7"}
    devkit_repo = sources.get("r4os-devkit")
    devkit = (catalog.repository_path(store, devkit_repo) if devkit_repo else store.root / "Repositories/Main/r4os-devkit") / "Setup"
    for key, filename, field in (("zig", "ZigToolchain.json", "version"), ("rust", "RustToolchain.json", "rust_version")):
        try:
            required[key] = json.loads((devkit / filename).read_text(encoding="utf-8-sig"))[field]
        except (OSError, ValueError, KeyError):
            pass
    installing = any(job["status"] in ("queued", "running") for job in store.jobs("tools"))
    components = []
    for key, label in (("zig", "Zig / C"), ("rust", "Rust / Cargo"), ("pwsh", "PowerShell")):
        components.append({"id": key, "name": label, "required": required.get(key, "From DevKit settings"),
                           "installed": (current or {}).get("versions", {}).get(key),
                           "state": "Ready" if current else "Setting up" if installing else "Not configured"})
    for role, repo in SOURCES.items():
        installed = (current or {}).get("sources", {}).get(role, {})
        latest = sources.get(repo, {}).get("local", {})
        available = latest.get("remote_commit") or latest.get("commit")
        components.append({"id": role, "name": role, "required": available or "Check after setup",
                           "installed": installed.get("version") or installed.get("commit"),
                           "state": "Update available" if installed and available and installed["commit"] != available else "Ready" if installed else "Setting up" if installing else "Not configured"})
    check = store.setting("tools_check", {})
    in_use = any(job["kind"] in ("build", "release") and job["status"] in ("queued", "running") and job["payload"].get("toolset") == store.setting("active_toolset") for job in store.jobs(limit=200))
    for component in components:
        role = component["id"]
        relative = "DevKit" if role == "DevKit" else "DevKit/Toolchains/Zig" if role == "zig" else "DevKit/Toolchains/Rust" if role == "rust" else "Repositories/" + role
        component["path"] = "/usr/bin/pwsh" if role == "pwsh" else str(Path(store.project_root) / "BuildTools/Toolsets" / (store.setting("active_toolset") or "…") / relative)
        component["host"] = role == "pwsh"
        component["in_use"] = in_use and bool(current)
    hosttools = (current or {}).get("capabilities", {})
    components.append({"id": "SDK", "name": "SDK-HostTools", "required": "SDK capability schema 3", "installed": "Schema " + str(hosttools["schema"]) if hosttools else None,
                       "path": str(Path(store.project_root) / "BuildTools/Toolsets" / (store.setting("active_toolset") or "…") / "HostTools"), "state": "Ready" if hosttools else "Not configured", "in_use": in_use})
    boot = store.setting("boot_tools", {})
    image = store.setting("image_tools", {})
    for key, label, installed in (("boot", "Limine (Releases)", boot), ("image", "Image-/Paketwerkzeuge (Releases)", image)):
        components.append({"id": key, "name": label, "required": "Matching the active DevKit / Distribution", "installed": installed.get("version") or installed.get("toolset"),
                           "path": str(Path(store.project_root) / installed["path"]) if installed.get("path") else "BuildTools/Boot" if key == "boot" else "BuildTools/ReleaseTools",
                           "state": "Ready" if installed and (key == "boot" or installed.get("toolset") == store.setting("active_toolset")) else "Wird bei Release-Build vorbereitet", "in_use": in_use})
    for command in ("git", "make", "cc", "clang-19", "llvm-ar-19", "llvm-nm-19"):
        command_path = shutil.which(command)
        components.append({"id": command, "name": command + " (Host)", "required": "Native Bibliotheken" if command.endswith("-19") else "Serverinstallation", "installed": command_path,
                           "path": command_path, "state": "Ready" if command_path else "Fehlt", "host": True})
    return {"active": store.setting("active_toolset"), "toolsets": sets, "components": components,
            "host": platform.system() + " " + platform.machine(), "errors": check.get("errors", []),
            "checked": check, "jobs": store.jobs("tools")}


def validate(context, root, manifest):
    errors = []
    for relative, expected in manifest.get("files", {}).items():
        path = under(root, relative)
        if not path.is_file() or digest(path) != expected:
            errors.append("Missing or modified tool: " + relative)
    zig = root / "DevKit/Toolchains/Zig/zig"
    if zig.is_file():
        version, code = context.run([zig, "version"], quiet=True, check=False)
        if code or version.strip() != manifest["versions"]["zig"]:
            errors.append("Zig-Version weicht vom Pin ab.")
    profile_path = root / "HostTools/RustToolsProfile.json"
    if profile_path.is_file():
        profile = json.loads(profile_path.read_text())
        for key, role in (("sdk", "SDK"), ("contract", "Contract")):
            for item in profile.get(key, {}).get("inputs", []):
                path = under(root / "Repositories" / role, item["path"])
                if not path.is_file() or digest(path) != item["sha256"]:
                    errors.append("SDK/Contract input modified: " + role + "/" + item["path"])
        if not errors:
            context.run([root / "HostTools/bin/x86_64-unknown-linux-gnu/module-catalog", "capabilities", "--json", "--profile", profile_path], quiet=True)
            context.run(["pwsh", "-NoLogo", "-NoProfile", "-File", root / "DevKit/Setup/Rust.ps1", "--profile", "sdk", "check"], quiet=True)
    context.store.set("tools_check", {"time": now(), "errors": errors, "toolset": manifest["id"]})
    if errors:
        raise ValueError("; ".join(errors))
    return {"toolset": manifest["id"], "ready": True}


def perform(context, action, payload):
    store = context.store
    if platform.system() != "Linux" or platform.machine() != "x86_64":
        raise ValueError("The build environment currently requires Linux x86_64.")
    component = payload.get("component")
    if component and component not in {*SOURCES, "zig", "rust", "pwsh", "boot", "image"}:
        raise ValueError("Unknown tool component.")
    if component:
        context.phase("Tool component with its dependencies: " + component)
    if component in ("boot", "image") and action == "check":
        saved = store.setting("boot_tools" if component == "boot" else "image_tools", {})
        if not saved or not saved.get("path"):
            raise ValueError("These release tools are not configured yet.")
        path = under(store.root, saved["path"])
        names = ("limine-bios.sys", "BOOTX64.EFI", "limine") if component == "boot" else tuple("bin/" + name for name in saved["files"])
        if not names or any(not (path / name).is_file() for name in names):
            raise ValueError("Release tools are incomplete. Set them up again.")
        return {"component": component, "ready": True, "path": saved["path"]}
    if component in ("boot", "image") and action in ("install", "update"):
        root, manifest = active(store)
        if component == "boot":
            from .release_build import boot_tools
            path, record = boot_tools(context, root)
            return dict(record, path=str(path.relative_to(store.root)))
        from . import builds
        name = store.source_repository("r4os-distribution")["name"]
        local = catalog.download(context, name)
        workspace = builds.workspace(context, root, "tools-" + context.job["id"])
        owner = workspace / "Repositories/Distribution"
        owner.unlink()
        clone_snapshot(context, catalog.repository_path(store, store.repository(name)), owner, local["commit"])
        output = store.root / "BuildTools/ReleaseTools" / manifest["id"] / "Distribution/HostTools"
        builds.configure_owner(workspace, owner, root, output)
        paths, _ = builds.execute_target(context, workspace, owner, {"id": "distribution-tools"}, root, output)
        record = {"toolset": manifest["id"], "commit": local["commit"], "path": str(output.relative_to(store.root)), "files": [path.name for path in paths]}
        store.set("image_tools", record)
        write_json(output / "tools.json", record)
        return record
    if action == "activate":
        key = identifier(payload.get("id", ""))
        root = under(store.root / "BuildTools/Toolsets", key)
        manifest = json.loads((root / "toolset.json").read_text())
        if manifest.get("status") != "ready":
            raise ValueError("This toolset was not fully configured.")
        validate(context, root, manifest)
        store.set("active_toolset", key)
        refresh_profiles(context)
        return {"active": key}
    if action == "check":
        context.phase("Checking tools and available source revisions")
        for repo in store.repositories():
            if repo["connection_id"] == "main" and repo["listed"] and repo["display_name"] in SOURCES.values() and repo["local"].get("commit"):
                catalog.inspect(context, repo["name"])
        root, manifest = active(store)
        result = validate(context, root, manifest)
        refresh_profiles(context)
        return result
    if action not in ("install", "update"):
        raise ValueError("Unknown tool action.")
    return install(context, update=action == "update")


def install(context, update=False):
    store = context.store
    context.phase("Preparing sources for a matching toolset")
    if not store.repositories("main"):
        catalog.refresh(context)
    sources = {}
    for role, name in SOURCES.items():
        name = store.source_repository(name)["name"]
        local = catalog.download(context, name)
        if update and local["status"] == "outdated":
            local = catalog.update(context, name)
        if local.get("dirty") or not local.get("commit"):
            raise ValueError(name + ": a clean source revision is required.")
        sources[role] = {"repository": name, "commit": local["commit"], "path": str(catalog.repository_path(store, store.repository(name)))}
    key = "linux-" + hashlib.sha256(json.dumps({role: item["commit"] for role, item in sources.items()}, sort_keys=True).encode()).hexdigest()[:16]
    root = store.root / "BuildTools/Toolsets" / key
    receipt = root / "toolset.json"
    if receipt.is_file():
        saved = json.loads(receipt.read_text())
        if saved.get("status") == "ready":
            validate(context, root, saved)
            store.set("active_toolset", key)
            refresh_profiles(context)
            return {"active": key, "unchanged": True}
        if store.setting("active_toolset") == key:
            raise ValueError("An active toolset must not be overwritten.")
    root.mkdir(parents=True, exist_ok=True)
    manifest = {"id": key, "status": "installing", "created": now(), "sources": sources, "versions": {}}
    write_json(receipt, manifest)
    try:
        for role, source in sources.items():
            target = root / "DevKit" if role == "DevKit" else root / "Repositories" / role
            context.phase("Source snapshot: " + role)
            clone_snapshot(context, source["path"], target, source["commit"])
            source["version"] = package_version(target)
            source.pop("path")
        if not (root / "Tools").is_symlink():
            (root / "Tools").symlink_to("Repositories/Project/Tools", target_is_directory=True)
        devkit, sdk, contract = root / "DevKit", root / "Repositories/SDK", root / "Repositories/Contract"
        (devkit / "SDK").mkdir(exist_ok=True)
        for name, target in (("Core", "../../Repositories/SDK"), ("Contract", "../../Repositories/Contract")):
            if not (devkit / "SDK" / name).is_symlink():
                (devkit / "SDK" / name).symlink_to(target, target_is_directory=True)
        pin = json.loads((devkit / "Setup/ZigToolchain.json").read_text(encoding="utf-8-sig"))
        rust = json.loads((devkit / "Setup/RustToolchain.json").read_text(encoding="utf-8-sig"))
        versions = manifest["versions"]
        versions.update(zig=pin["version"], rust=rust["rust_version"])
        pwsh = shutil.which("pwsh")
        if not pwsh:
            raise ValueError("PowerShell 7 is missing. Install this host prerequisite through the server setup.")
        ps_version, _ = context.run([pwsh, "--version"], quiet=True)
        versions["pwsh"] = ps_version.strip()
        spec = pin["hosts"]["Linux"]
        archive = download(context, spec["url"], store.root / "BuildTools/Downloads" / Path(spec["url"]).name, spec["sha256"])
        zig_root = devkit / "Toolchains/Zig"
        if not (zig_root / "zig").is_file():
            extract = root / ".zig-install"
            if extract.exists():
                shutil.rmtree(extract)
            extract.mkdir()
            with tarfile.open(archive) as package:
                package.extractall(extract, filter="data")
            zig_root.parent.mkdir(exist_ok=True)
            next(extract.iterdir()).rename(zig_root)
            extract.rmdir()
        zig = zig_root / "zig"
        zig.chmod(0o750)
        actual, _ = context.run([zig, "version"], quiet=True)
        if actual.strip() != pin["version"]:
            raise ValueError("The extracted Zig version does not match the pin.")
        context.phase("Installing pinned Rust/Cargo components")
        context.run([pwsh, "-NoLogo", "-NoProfile", "-File", devkit / "Setup/Rust.ps1", "--profile", "sdk", "--cache-root",
                     store.root / "BuildTools/Cache/Rust", "setup"], timeout=3600)
        prefix = root / "Artifacts/SDK"
        cache = store.root / "BuildTools/Cache/Zig"
        def sdk_build(options, output):
            context.run([zig, "build", "-j2", "--fork=" + str(contract), "--cache-dir", cache / key,
                         "--global-cache-dir", cache / "Global", "--prefix", output, *options], cwd=sdk, timeout=2400)
        context.phase("Building SDK HostTools and matching runtime objects")
        for flag in ("packager-only", "catalog-only", "r4l-contract-generator-only", "heap-runtime-only", "facade-runtime-only"):
            sdk_build(["-D" + flag + "=true"], prefix)
        windows = root / "Artifacts/SDK-Windows"
        for flag in ("packager-only", "catalog-only", "r4l-contract-generator-only"):
            sdk_build(["-D" + flag + "=true", "-Dtarget=x86_64-windows"], windows)
        context.phase("Exporting a matching SDK toolset with input receipts")
        tools = root / "HostTools"
        rust_manifest = store.root / "BuildTools/Cache/Rust" / ("channel-rust-" + rust["rust_version"] + ".toml")
        if not rust_manifest.exists():
            candidates = list((store.root / "BuildTools/Cache/Rust").rglob("channel-rust-" + rust["rust_version"] + ".toml"))
            if not candidates:
                raise ValueError("The pinned Rust release manifest is missing after installation.")
            rust_manifest = candidates[0]
        context.run([pwsh, "-NoLogo", "-NoProfile", "-File", devkit / "Setup/Export-RustTools.ps1",
                     "-Destination", tools, "-SdkRoot", sdk, "-ContractRoot", contract, "-ReleaseManifest", rust_manifest,
                     "-PackagerLinux", prefix / "bin/r4xbuilder", "-PackagerWindows", windows / "bin/r4xbuilder.exe",
                     "-CatalogLinux", prefix / "bin/module-catalog", "-CatalogWindows", windows / "bin/module-catalog.exe",
                     "-LibraryContractLinux", prefix / "bin/r4l-contract-gen", "-LibraryContractWindows", windows / "bin/r4l-contract-gen.exe",
                     "-HeapRuntime", prefix / "lib/r4sdk_heap.o", "-FacadeRuntime", prefix / "lib/r4sdk_facades.o",
                     "-StorageRuntime", prefix / "lib/r4sdk_storage.o"], timeout=600)
        catalog_tool = tools / "bin/x86_64-unknown-linux-gnu/module-catalog"
        capabilities, _ = context.run([catalog_tool, "capabilities", "--json", "--profile", tools / "RustToolsProfile.json"], quiet=True)
        manifest["capabilities"] = json.loads(capabilities)
        if manifest["capabilities"].get("schema") != 3:
            raise ValueError("This SDK capability schema is not supported yet.")
        manifest["files"] = {}
        for path in [zig, tools / "RustToolsProfile.json", *[p for p in (tools / "bin").rglob("*") if p.is_file()],
                     *[p for p in (tools / "Runtime").rglob("*") if p.is_file()]]:
            manifest["files"][str(path.relative_to(root))] = digest(path)
        manifest.update(status="ready", installed=now())
        write_json(receipt, manifest)
        validate(context, root, manifest)
        store.set("active_toolset", key)
        refresh_profiles(context)
        return {"active": key, "versions": versions}
    except BaseException as error:
        if store.setting("active_toolset") != key or manifest.get("status") != "ready":
            manifest.update(status="failed", error=str(error))
            write_json(receipt, manifest)
        raise
