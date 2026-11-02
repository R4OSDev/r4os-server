"""Owner builds in disposable source snapshots, with immutable output receipts."""
import hashlib
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from . import catalog
from .profiles import fields
from .process import clean_env
from .store import identifier, now, under
from .toolsets import active, clone_snapshot, digest, write_json

ROLES = {"r4os-app-": "Apps", "r4os-games-": "Apps", "r4os-service-": "Services", "r4os-diagnostic-": "Diagnostics",
         "r4os-driver-": "Drivers", "r4os-protocol-": "Protocols", "r4os-subsystem-": "Subsystems"}
CENTRAL = {"r4os-kernel": "Kernel", "r4os-sdk": "SDK", "r4os-contract": "Contract",
           "r4os-libraries": "Libraries", "r4os-distribution": "Distribution", "r4os-recovery": "Recovery"}


def inputs(store, value):
    repo = store.require_repository(value.get("name", ""))
    local = repo["local"]
    target = value.get("target")
    modules = [module for module in local.get("modules", []) if module["id"] == target]
    if not modules:
        raise ValueError("The build target was not found in the repository.")
    if modules[0].get("supported") is False:
        raise ValueError(modules[0].get("reason", "A suitable build adapter for this target is missing."))
    if not local.get("commit") or local.get("dirty"):
        raise ValueError("A downloaded, clean source revision is required.")
    root, tools = active(store)
    sources = {}
    module = dict(modules[0], source_ids={})
    for name in module.get("source_repositories", []):
        source = store.source_repository(name, repo["connection_id"])
        if not source["local"].get("commit") or source["local"].get("dirty"):
            raise ValueError(name + ": required as a clean local source import. Download it or check its status first.")
        sources[source["name"]] = source["local"]["commit"]
        module["source_ids"][name] = source["name"]
    return repo, {"target": target, "module": module, "commit": local["commit"], "toolset": tools["id"], "source_dependencies": sources}


def enqueue(store, value):
    repo, payload = inputs(store, value)
    return store.enqueue("build", repo["name"], payload)


def owner_relative(name, module=None, identity=None):
    if identity and identity != name:
        return Path('Projects') / identifier(identity)
    if name in CENTRAL:
        return Path(CENTRAL[name])
    for prefix, role in ROLES.items():
        if name.startswith(prefix):
            return Path(role) / name.removeprefix(prefix)
    if module and module.get('manifest') and module.get('kind') in ('R4X', 'R4L', 'R4D', 'R4P'):
        return Path('Projects') / identifier(identity or name)
    raise ValueError("No owner build adapter is available for this repository.")


def snapshot(context, name, commit, destination):
    repo = context.store.require_repository(name)
    original = catalog.repository_path(context.store, repo)
    dirty, _ = context.git(original, "status", "--porcelain=v1", "--untracked-files=normal")
    if dirty.strip():
        raise ValueError(name + ": local changes have been preserved; provide a clean revision first.")
    destination = Path(destination)
    destination.mkdir(parents=True)
    archive = destination.parent / (destination.name + ".source.tar")
    context.git(original, "archive", "--format=tar", "--output=" + str(archive), commit)
    try:
        with tarfile.open(archive) as source:
            source.extractall(destination, filter="data")
    finally:
        archive.unlink(missing_ok=True)


def workspace(context, tools, key):
    root = context.store.root / "BuildTools/Workspaces" / identifier(key)
    if root.exists():
        raise ValueError("A build working directory with this ID already exists.")
    repos = root / "Repositories"
    repos.mkdir(parents=True)
    (root / "DevKit").symlink_to(tools / "DevKit", target_is_directory=True)
    (root / "Tools").symlink_to(tools / "Repositories/Project/Tools", target_is_directory=True)
    for name in ("SDK", "Contract", "Libraries", "Distribution"):
        (repos / name).symlink_to(tools / "Repositories" / name, target_is_directory=True)
    return root


def configure_owner(root, owner, tools, output):
    values = {"WORKSPACE_ROOT": str(root), "REPOSITORIES_ROOT": str(root / "Repositories"),
              "SDK_ROOT": str(root / "Repositories/SDK"), "CONTRACT_ROOT": str(root / "Repositories/Contract"),
              "LIBRARIES_ROOT": str(root / "Repositories/Libraries"), "KERNEL_ROOT": str(root / "Repositories/Kernel"),
              "DISTRIBUTION_ROOT": str(root / "Repositories/Distribution"), "DEVKIT_ROOT": str(tools / "DevKit"),
              "ARTIFACTS_ROOT": str(output), "ZIG_ROOT": str(tools / "DevKit/Toolchains/Zig"),
              "RUST_TOOLS_ROOT": str(tools / "HostTools"), "RUST_TOOLS_TECHNICAL": "false",
              "APPS_ROOT": "Apps", "SERVICES_ROOT": "Services", "DIAGNOSTICS_ROOT": "Diagnostics",
              "DRIVERS_ROOT": "Drivers", "PROTOCOLS_ROOT": "Protocols", "SUBSYSTEMS_ROOT": "Subsystems",
              "LIMINE_ROOT": "Boot/Limine", "QEMU_ROOT": "Emulation/QEMU", "DEVKIT_HOSTTOOLS_ROOT": "HostTools/bin",
              "DISTRIBUTION_OUTPUT_ROOT": "Distribution", "INPUT_ROOT": "Distribution/Inputs", "PRIVATE_INJECTION_ROOT": "Distribution/PrivateInjection"}
    if owner == root / "Repositories/Distribution":
        values.update(WORKSPACE_ROOT="../..", REPOSITORIES_ROOT="..", SDK_ROOT="SDK", CONTRACT_ROOT="Contract",
                      LIBRARIES_ROOT="Libraries", KERNEL_ROOT="Kernel", DEVKIT_ROOT="DevKit",
                      ARTIFACTS_ROOT=os.path.relpath(output, root), ZIG_ROOT="Toolchains/Zig")
    (owner / "Settings.R4S").write_text("# Local build path mapping\n" + "\n".join(key + "=" + str(value) for key, value in values.items()) + "\n")


def prepare_source_dependencies(context, root, owner, module, tools, sources):
    """Keep the conventional owner paths used by cross-repository wire imports."""
    texts = ["\n".join(module.get("zig_modules", []))]
    folder = owner / Path(module.get("manifest", ".")).parent
    if not (folder / "build.zig").is_file() and (owner / "build.zig").is_file():
        folder = owner
    if (folder / "build.zig").is_file():
        texts.append((folder / "build.zig").read_text())
    aliases = re.findall(r'(?:\.\./)+(Apps|Services|Diagnostics|Drivers|Protocols|Subsystems)/([A-Za-z0-9_-]+)/', "\n".join(texts))
    for name in module.get("source_repositories", []):
        destination = root / "Repositories" / owner_relative(name)
        if not destination.exists():
            source_id = module.get("source_ids", {}).get(name, name)
            commit = sources.get(source_id)
            if not commit:
                raise ValueError("Pinned source import missing: " + name)
            snapshot(context, source_id, commit, destination)
            configure_owner(root, destination, tools, root / "Output")
        for role, label in aliases:
            if owner_relative(name) == Path(role) / label.lower():
                alias = root / "Repositories" / role / label
                if alias != destination and not alias.exists():
                    alias.symlink_to(destination.name, target_is_directory=True)


def private_libraries(context, root, tools):
    libraries = root / "Repositories/Libraries"
    if libraries.is_symlink():
        source = libraries.resolve()
        libraries.unlink()
        commit, _ = context.git(source, "rev-parse", "HEAD")
        clone_snapshot(context, source, libraries, commit.strip())
    marker = root / "Generated/library-script-sources.json"
    if not marker.exists():
        source, reference = libraries, "HEAD"
        if not (libraries / ".git").is_dir():
            source = catalog.repository_path(context.store, context.store.source_repository("r4os-libraries"))
            reference = context.job["payload"]["commit"]
        names, _ = context.git(source, "ls-tree", "-r", "--name-only", "-z", reference)
        names = [name for name in names.split("\0") if Path(name).suffix in (".ps1", ".bat", ".cmd") and (libraries / name).is_file()]
        before = {name: digest(libraries / name) for name in names}
        marker.parent.mkdir(parents=True, exist_ok=True)
        for name in names:
            context.check()
            raw = subprocess.run(["git", "-C", source, "cat-file", "blob", reference + ":" + name], env=clean_env(), check=True, capture_output=True, timeout=30)
            under(libraries, name).write_bytes(raw.stdout)
        changed = {name: {"checkout": value, "git_blob": digest(libraries / name)} for name, value in before.items() if digest(libraries / name) != value}
        write_json(marker, {"reason": "Canonical Git blob bytes for source/recipe hash checks; checkout EOL conversion is not a source change.", "files": changed})
        if changed:
            context.log("Library recipes prepared from canonical Git blobs: " + str(len(changed)))
    return libraries


def prepare_library_bindings(context, root, tools):
    """Regenerate compiler-owned ABI projections in a private Libraries copy."""
    marker = root / "Generated/library-bindings.json"
    if marker.is_file():
        return
    libraries = private_libraries(context, root, tools)
    sdk, contract = root / "Repositories/SDK", root / "Repositories/Contract"
    prefix = root / "Generated/LibraryTools"
    zig = tools / "DevKit/Toolchains/Zig/zig"
    paths = [libraries / "Shared/C/include/r4libraries/helpers.h", libraries / "Shared/Language/fingerprint.zig",
             libraries / "Shared/Rust/src/raw.rs"]
    before = {str(path.relative_to(libraries)): digest(path) for path in paths}
    context.phase("Preparing compiler-validated C/Rust library bindings")
    for selector, program, arguments in (("library-helpers-generator-only", "library-helpers-gen", paths[:2]),
                                          ("library-helpers-rust-generator-only", "library-helpers-rust-gen", paths[2:])):
        context.run([zig, "build", "-j2", "--fork=" + str(contract), "-Dlibrary-helpers-root=" + str(libraries),
                     "-D" + selector + "=true", "--prefix", prefix,
                     "--cache-dir", context.store.root / "BuildTools/Cache/Zig/Library-Generators",
                     "--global-cache-dir", context.store.root / "BuildTools/Cache/Zig/Global"], cwd=sdk, timeout=2400)
        context.run([prefix / "bin" / program, "write", *arguments], timeout=300)
    write_json(marker, {"before": before, "generated": {str(path.relative_to(libraries)): digest(path) for path in paths}})


def module_build(context, root, owner, module, tools, output):
    output.mkdir(parents=True, exist_ok=True)
    zig = tools / "DevKit/Toolchains/Zig/zig"
    sdk, contract, libraries = [root / "Repositories" / name for name in ("SDK", "Contract", "Libraries")]
    cache = context.store.root / "BuildTools/Cache/Zig"
    module_root = owner / Path(module["manifest"]).parent
    catalog_exe = context.store.root / "BuildTools/Metadata" / tools.name / "HostTools/bin/module-catalog"
    action = "metadata"
    if not catalog_exe.is_file():
        catalog_exe, action = tools / "HostTools/bin/x86_64-unknown-linux-gnu/module-catalog", "plan"
        if not catalog_exe.is_file():
            catalog_exe = tools / "HostTools/bin/module-catalog"
    facts, _ = context.run([catalog_exe, action, "--manifest", owner / module["manifest"]], quiet=True)
    plan = json.loads(facts)
    if module["language"] == "Rust" or not any((folder / "build.zig").is_file() for folder in (module_root, owner)):
        cargo = module_root / "Cargo.toml"
        needs_helpers = cargo.is_file() and "r4os-library-helpers" in cargo.read_text()
        if needs_helpers:
            prepare_library_bindings(context, root, tools)
        context.run(["pwsh", "-NoLogo", "-NoProfile", "-File", sdk / "Tools/Build-Project.ps1",
                     "-ProjectRoot", module_root, "-SdkRoot", sdk, "-ContractRoot", contract, "-ToolsRoot", tools / "HostTools",
                     "-InstallRoot", tools / "DevKit", "-CacheRoot", context.store.root / ("BuildTools/Cache/Rust-Build" if module["language"] == "Rust" else "BuildTools/Cache/Manifest-Build"),
                     "-OutputRoot", output, *(["-LibraryHelpersRoot", libraries] if needs_helpers else []), "-ZigRoot", zig.parent], timeout=3600)
    else:
        if not (module_root / "build.zig").is_file() and (owner / "build.zig").is_file():
            module_root = owner
        forks = ["--fork=" + str(sdk), "--fork=" + str(contract)]
        zone = module_root / "build.zig.zon"
        if zone.is_file() and re.search(r"\.r4os_libraries\s*=", zone.read_text()):
            libraries = private_libraries(context, root, tools)
            forks.append("--fork=" + str(libraries))
        if zone.is_file():
            declared = set(re.findall(r'https://github.com/R4OSDev/([A-Za-z0-9_.-]+)/archive/', zone.read_text()))
            for name in module.get("source_repositories", []):
                if name in declared:
                    forks.append("--fork=" + str(root / "Repositories" / owner_relative(name)))
        context.run([zig, "build", "-j2", "--prefix", output, "--cache-dir", cache / (tools.name + "-" + module["id"]),
                     "--global-cache-dir", cache / "Global", *forks], cwd=module_root, timeout=3600)
    expected = module["id"] + "." + module["kind"]
    matches = [path for path in output.rglob(expected) if ".cache" not in path.parts and path.is_file()]
    if len(matches) != 1 or not matches[0].stat().st_size:
        raise ValueError("Expected module is missing or ambiguous: " + expected)
    return matches, plan


def execute_target(context, root, owner, module, tools, output):
    if "manifest" in module:
        return module_build(context, root, owner, module, tools, output)
    output.mkdir(parents=True, exist_ok=True)
    zig, contract = tools / "DevKit/Toolchains/Zig/zig", root / "Repositories/Contract"
    key = module["id"]
    if key == "kernel":
        context.run(["sh", owner / "Build.sh", "build"], cwd=owner, timeout=3600)
        paths = [owner / "zig-out/bin/r4os.elf"]
    elif key == "recovery":
        context.run(["pwsh", "-NoLogo", "-NoProfile", "-File", owner / "Build.ps1", "-Mode", "Kernel", "-Zig", zig], cwd=owner, timeout=3600)
        paths = [owner / "Artifacts/Kernel/bin/recovery.elf"]
    else:
        cache = context.store.root / "BuildTools/Cache/Zig"
        flags = [] if key == "contract-tools" else ["--fork=" + str(contract)]
        if key == "distribution-tools":
            flags += ["--fork=" + str(root / "Repositories/SDK"), "--fork=" + str(root / "Repositories/Libraries")]
        selectors = ["packager-only", "catalog-only", "r4l-contract-generator-only"] if key == "sdk-tools" else [None]
        for selector in selectors:
            context.run([zig, "build", "-j2", "--prefix", output, "--cache-dir", cache / (tools.name + "-" + key),
                         "--global-cache-dir", cache / "Global", *flags, *(["-D" + selector + "=true"] if selector else [])], cwd=owner, timeout=3600)
        paths = [path for path in (output / "bin").glob("*") if path.is_file()]
    if not paths or any(not path.is_file() or not path.stat().st_size for path in paths):
        raise ValueError("The owner build did not produce the expected outputs.")
    return paths, {}


def register_outputs(store, root):
    files = []
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.is_symlink():
            relative = str(path.relative_to(store.root))
            files.append({"id": hashlib.sha256(relative.encode()).hexdigest()[:24], "path": relative,
                          "name": str(path.relative_to(root)), "bytes": path.stat().st_size, "sha256": digest(path)})
    return files


def build(context, name, payload):
    store = context.store
    tool_key = identifier(payload["toolset"])
    tools = under(store.root / "BuildTools/Toolsets", tool_key)
    tool_manifest = json.loads((tools / "toolset.json").read_text())
    if tool_manifest.get("status") != "ready":
        raise ValueError("The selected toolset is not ready.")
    local = catalog.inspect(context, name, remote=False)
    modules = [item for item in local.get("modules", []) if item["id"] == payload["target"]]
    if not modules and not payload.get('module'):
        raise ValueError("The build target is no longer available.")
    module = payload.get("module") or modules[0]
    root = workspace(context, tools, context.job["id"])
    owner = root / "Repositories" / owner_relative(store.repository(name)["display_name"], module, name)
    if owner.is_symlink():
        owner.unlink()
    context.phase("Preparing source snapshot for " + name)
    snapshot(context, name, payload["commit"], owner)
    if payload.get('github_release') and module.get('manifest'):
        values = fields(owner / module['manifest'])
        if any(values.get(key) != [expected] for key, expected in (('NAME', module['id']), ('KIND', module['kind']), ('VERSION', module['version']))):
            raise ValueError('The source snapshot differs from the queued release manifest.')
    output = root / "Output"
    configure_owner(root, owner, tools, output)
    prepare_source_dependencies(context, root, owner, module, tools, payload.get("source_dependencies", {}))
    context.phase(module["name"] + " build (" + module["language"] + ")")
    paths, plan = execute_target(context, root, owner, module, tools, output)
    final = store.root / "Artifacts" / identifier(name) / identifier(module["id"]) / context.job["id"]
    destination = store.root / "Artifacts/.staging" / context.job["id"]
    destination.mkdir(parents=True)
    files_dir = destination / "files"
    files_dir.mkdir()
    for path in paths:
        shutil.copy2(path, files_dir / path.name)
    receipt = {"schema": 1, "status": "success", "repository": name, "commit": payload["commit"], "module": module,
               "toolset": tool_key, "tools": tool_manifest["versions"], "sources": tool_manifest["sources"],
               "plan": plan, "started": context.job.get("started"), "finished": now()}
    receipt["source_dependencies"] = payload.get("source_dependencies", {})
    if receipt["started"]:
        receipt["duration_seconds"] = max(0, (datetime.fromisoformat(receipt["finished"]) - datetime.fromisoformat(receipt["started"])).total_seconds())
    native_receipts = [(output / "build.json", "sdk-build.json"), (owner / "zig-out/bin/r4os.build.json", "kernel-build.json")]
    for native_receipt, label in native_receipts:
        if not native_receipt.is_file():
            continue
        (destination / "manifests").mkdir(exist_ok=True)
        shutil.copy2(native_receipt, destination / "manifests" / label)
        receipt[label] = "manifests/" + label
    generated = root / "Generated/library-bindings.json"
    if generated.is_file():
        receipt["generated_library_bindings"] = json.loads(generated.read_text())
    scripts = root / "Generated/library-script-sources.json"
    if scripts.is_file():
        receipt["library_script_sources"] = json.loads(scripts.read_text())
    write_json(destination / "build.json", receipt)
    context.log("Build succeeded: " + ", ".join(path.name for path in paths))
    shutil.copyfile(context.log_path, destination / "build.log")
    checks = [digest(path) + "  " + str(path.relative_to(destination)) for path in sorted(destination.rglob("*")) if path.is_file()]
    (destination / "checksums.sha256").write_text("\n".join(checks) + "\n")
    context.check()
    final.parent.mkdir(parents=True, exist_ok=True)
    destination.rename(final)
    return {"repository": name, "target": module["id"], "commit": payload["commit"], "toolset": tool_key,
            "files": register_outputs(store, final), "output": str(final.relative_to(store.root))}
