"""Build exactly a saved local release selection using canonical image helpers."""
import hashlib
import json
from pathlib import Path
import re
import shutil
import zipfile

from . import builds, catalog, releases, testconfigs
from .profiles import fields
from .store import identifier, now, under
from .toolsets import active, clone_snapshot, digest, download, write_json

REQUIRED_BOOT = {"HIDREPORT", "USBHID", "USBBOT", "USBSCSI", "XHCI", "USBMSC", "AHCI", "NVME", "ATAPIO"}


def enqueue(store, key, value):
    view = releases.preview(store, key)
    release = view["release"]
    if value.get("revision") != release["revision"]:
        raise releases.Conflict("The release configuration has changed. Reload it and use the saved revision.")
    if view["issues"]:
        raise ValueError(" ".join(view["issues"]))
    version = value.get("version", "")
    if not isinstance(version, str) or not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version):
        raise ValueError("An R4OS version in MAJOR.MINOR.PATCH format is required.")
    names = {module["id"] for module in view["components"]}
    if REQUIRED_BOOT - names:
        raise ValueError("Required boot components are missing: " + ", ".join(sorted(REQUIRED_BOOT - names)))
    tools_root, tools = active(store)
    sources = {}
    for name, member in release["members"].items():
        if not member.get("selected"):
            continue
        repo = store.require_repository(name)
        if not repo["local"].get("commit"):
            raise ValueError(name + ": source revision is missing.")
        sources[name] = repo["local"]["commit"]
    payload = {"release": release, "version": version, "sources": sources,
               "components": view["components"], "toolset": tools["id"], "source_dependencies": view["build_dependencies"],
               "test_configuration": view["test_configuration"], "main_sources": view["main_sources"]}
    with store.connect() as db:
        db.execute("BEGIN IMMEDIATE")
        if releases.get_type(store, key, connection=db)["revision"] != release["revision"]:
            raise releases.Conflict("The release configuration has changed. Reload it.")
        if testconfigs.snapshot(store, release["test_configuration"], connection=db) != payload["test_configuration"]:
            raise releases.Conflict("The test configuration has changed. Reload it.")
        return store.enqueue("release", "release:" + key, payload, connection=db)


def ps_string(value):
    return "'" + str(value).replace("'", "''") + "'"


def replace_function(source, name, replacement):
    pattern = r"(?ms)^function " + re.escape(name) + r"[^\n]*\{.*?^\}"
    result, count = re.subn(pattern, lambda _: replacement, source)
    if count != 1:
        raise ValueError("The Project adapter does not match the selected source revision: " + name)
    return result


def boot_tools(context, tools):
    source = (tools / "DevKit/Setup/Setup.ps1").read_text()
    match = re.search(r"\$url\s*=\s*'(https://github.com/Limine-Bootloader/[^']+)'\s*\n\s*\$hash\s*=\s*'([a-f0-9]{64})'", source)
    if not match:
        raise ValueError("The pinned Limine source could not be read from the DevKit.")
    url, checksum = match.groups()
    root = context.store.root / "BuildTools/Boot" / checksum[:16]
    ready = root / "limine-bios.sys"
    if ready.is_file() and (root / "BOOTX64.EFI").is_file():
        context.store.set("boot_tools", {"path": str(root.relative_to(context.store.root)), "version": Path(url).name, "sha256": checksum})
        return root, {"url": url, "sha256": checksum}
    archive = download(context, url, context.store.root / "BuildTools/Downloads" / Path(url).name, checksum)
    stage = root.with_name(root.name + "-staging")
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    with zipfile.ZipFile(archive) as zipped:
        for entry in zipped.infolist():
            if Path(entry.filename).is_absolute() or ".." in Path(entry.filename).parts:
                raise ValueError("Unsicherer Pfad im Limine-Archiv.")
        zipped.extractall(stage)
    content = next(path for path in stage.iterdir() if path.is_dir())
    context.phase("Preparing the pinned Limine host tool")
    context.run(["make", "-C", content], timeout=600)
    if root.exists():
        shutil.rmtree(root)
    content.rename(root)
    stage.rmdir()
    if not ready.is_file() or not (root / "BOOTX64.EFI").is_file():
        raise ValueError("Limine boot files are missing after preparation.")
    context.store.set("boot_tools", {"path": str(root.relative_to(context.store.root)), "version": Path(url).name, "sha256": checksum})
    return root, {"url": url, "sha256": checksum}


def selection_catalog(context, tools, root, components):
    """Adapt only image selection; retain the SDK parser, ABI and inventories."""
    sdk = tools / "Repositories/SDK"
    variant = root / "Adapters/SDK"
    shutil.copytree(sdk, variant, ignore=shutil.ignore_patterns(".git", ".zig-cache", "zig-out", "__pycache__"))
    path = variant / "Tools/ModuleCatalog/src/main.zig"
    source = path.read_text()
    targets = sorted({module["target"] for module in components if module.get("target")})
    literals = ",\n        ".join(json.dumps(target, ensure_ascii=True) for target in targets)
    replacement = """fn imageEntryIncluded(entry: manifest_contract.Manifest, mode: ImageMode, include_targets: []const []const u8) bool {
    _ = mode;
    _ = include_targets;
    if (entry.version != manifest_contract.manifest_version) return false;
    const selected = [_][]const u8{
        """ + literals + """
    };
    for (selected) |target| {
        if (std.ascii.eqlIgnoreCase(entry.target, target)) return true;
    }
    return false;
}"""
    adapted, count = re.subn(r"(?ms)^fn imageEntryIncluded\([^\n]+\) bool \{.*?^\}", lambda _: replacement, source)
    if count != 1:
        raise ValueError("The SDK selection adapter does not match the selected source revision.")
    path.write_text(adapted)
    output = root / "Adapters/HostTools"
    context.phase("Building the release selection adapter with unchanged manifest validation")
    context.run([tools / "DevKit/Toolchains/Zig/zig", "build", "-j2", "-Dcatalog-only=true",
                 "--fork=" + str(tools / "Repositories/Contract"), "--prefix", output,
                 "--cache-dir", context.store.root / "BuildTools/Cache/Zig/Release-Adapter",
                 "--global-cache-dir", context.store.root / "BuildTools/Cache/Zig/Global"], cwd=variant, timeout=1800)
    return output / "bin/module-catalog", {"source_sha256": hashlib.sha256(source.encode()).hexdigest(),
                                            "adapter_sha256": digest(path), "selection": targets}


def project_adapter(root, tool, map_path):
    script = root / "Tools/BuildWorkspace.ps1"
    source = script.read_text()
    source = replace_function(source, "Ensure-ModuleCatalog", "function Ensure-ModuleCatalog {\n    $script:moduleCatalogExe = " + ps_string(tool) + "\n}")
    source = replace_function(source, "Write-WorkspaceMap", "function Write-WorkspaceMap {\n    [IO.Directory]::CreateDirectory($distributionInputRoot) | Out-Null\n    Copy-Item -LiteralPath " + ps_string(map_path) + " -Destination $workspaceMapPath -Force\n}")
    source = replace_function(source, "Get-ImageSelectionArguments", """function Get-ImageSelectionArguments([string]$SelectedProfile, [switch]$IncludeBrowserTest) {
    return @('--workspace-map', $workspaceMapPath, '--image-mode', $SelectedProfile.ToLowerInvariant())
}""")
    script.write_text(source)
    return digest(script)


def distribution_adapter(root, components, test_overlay=None, *, preserve_legacy=False):
    """Apply corresponding-source requirements to the actual module selection."""
    script = root / "Repositories/Distribution/Tools/Distribution.ps1"
    source = script.read_text()
    match = re.search(r"(?ms)^function New-R4DistributionPlan\([^\n]*\) \{.*?^\}", source)
    if not match:
        raise ValueError("The Distribution selection adapter does not match the source revision.")
    function = match.group(0)
    included = any(module["id"] == "R4VIDEO" for module in components)
    for pattern in (r"(?m)^ \$videoSources=.*\n Invoke-R4Distribution[^\n]+", r"(?ms)^ \$sourcePlan=.*?^ \$arguments\+=@\('--plan',\$sourcePlan\)",
                    r"(?ms)^ \$planText=Get-Content -Raw -LiteralPath \$list\n foreach\(\$name in @\('SOURCE.TGZ','MANIFEST'\)\)\{.*?^ \}"):
        function, count = re.subn(pattern, lambda block: " if($serverIncludesVideo){\n" + block.group(0) + "\n }", function)
        if count != 1:
            raise ValueError("The R4VIDEO source adapter does not match the Distribution contract.")
    first_line, body = function.split("\n", 1)
    function = first_line + "\n $serverIncludesVideo=" + ("$true" if included else "$false") + "\n" + body
    adapted = function
    # The selected test set replaces the old profile-bound test overlays.
    if not preserve_legacy:
        for pattern in (r"(?m)^\s*if\(\$profile.TEST_OVERLAY[^\n]+\n", r"(?m)^\s*if\(\$Variant -ceq 'browser'\)\{\$arguments[^\n]+\n"):
            adapted, count = re.subn(pattern, "\n", adapted)
            if count != 1:
                raise ValueError("The test configuration adapter does not match the Distribution contract.")
    if test_overlay is not None:
        anchor = "$arguments+=@('--optional-overlay',(Join-Path $Context.output 'Injection'))"
        if adapted.count(anchor) != 1:
            raise ValueError("The test file adapter does not match the Distribution contract.")
        adapted = adapted.replace(anchor, anchor + "\n  $arguments+=@('--overlay'," + ps_string(test_overlay) + ")")
    script.write_text(source[:match.start()] + adapted + source[match.end():])
    return {"source_sha256": hashlib.sha256(source.encode()).hexdigest(), "adapter_sha256": digest(script),
            "video_sources_required": included, "test_overlay": str(test_overlay) if test_overlay is not None else None}


def prepare_video_sources(context, root, components):
    if not any(module["id"] == "R4VIDEO" for module in components):
        return
    unit = root / "Repositories/Libraries/R4VIDEO"
    pin = json.loads((unit / "ThirdParty/Sources.json").read_text())["sources"][0]
    destination = root / "Output/Native/R4VIDEO/Downloads" / ("ffmpeg-" + pin["version"] + ".tar.xz")
    download(context, pin["url"], destination, pin["sha256"])


def reusable(context, name, module, commit, toolset, dependencies):
    for job in context.store.jobs(name, limit=100):
        payload = job["payload"]
        if job["kind"] != "build" or job["status"] != "completed" or payload.get("commit") != commit or payload.get("toolset") != toolset or payload.get("target") != module["id"]:
            continue
        source_ids = module.get("source_ids", {})
        expected_dependencies = {source_ids.get(key, key): dependencies.get(source_ids.get(key, key)) for key in module.get("source_repositories", [])}
        if payload.get("source_dependencies", {}) != expected_dependencies:
            continue
        for item in job["result"].get("files", []):
            expected_name = "r4os.elf" if module["id"] == "kernel" else module["id"] + "." + module["kind"]
            if Path(item["name"]).name == expected_name:
                path = under(context.store.root, item["path"])
                if path.is_file() and digest(path) == item["sha256"]:
                    return path
    return None


def prepared_component(context, payload, name, module):
    """Reuse a checked component when a previous image/package phase failed."""
    keys = ("release", "version", "sources", "components", "toolset")
    for previous in context.store.jobs("release:" + payload["release"]["id"], limit=40):
        if previous["id"] == context.job["id"] or previous["status"] not in ("failed", "interrupted"):
            continue
        if any(previous["payload"].get(key) != payload.get(key) for key in keys):
            continue
        required = module.get("source_repositories", [])
        if any(previous["payload"].get("source_dependencies", {}).get(key) != payload.get("source_dependencies", {}).get(key) for key in required):
            continue
        stage = context.store.root / "Release/.staging" / previous["id"]
        record = stage / "manifests" / ("R4OS-SOURCES-" + payload["version"] + ".json")
        if not record.is_file():
            continue
        data = json.loads(record.read_text())
        if data.get("sources") != payload["sources"] or data.get("toolset", {}).get("id") != payload["toolset"]:
            continue
        receipt = next((entry for entry in data.get("components", []) if entry["repository"] == name and entry["module"] == module["id"]), None)
        if not receipt:
            continue
        expected = "r4os.elf" if module["id"] == "kernel" else module["id"] + "." + module["kind"]
        file = next((entry for entry in receipt["files"] if entry["name"] == expected), None)
        if not file:
            continue
        workspace = context.store.root / "BuildTools/Workspaces" / previous["id"]
        if module["id"] == "kernel":
            matches = [workspace / "Repositories/Kernel/zig-out/bin/r4os.elf"]
        else:
            matches = list((workspace / "Output/Modules" / name / module["id"]).rglob(expected))
        matches = [path for path in matches if path.is_file() and not path.is_symlink()]
        if len(matches) == 1 and digest(matches[0]) == file["sha256"]:
            return matches[0]
    return None


def build(context, payload):
    store = context.store
    release, version = payload["release"], payload["version"]
    base = {"slim": "Slim", "full": "Full", "test": "Test"}[release["base"]]
    tools = under(store.root / "BuildTools/Toolsets", identifier(payload["toolset"]))
    manifest = json.loads((tools / "toolset.json").read_text())
    if manifest.get("status") != "ready":
        raise ValueError("The selected toolset is not ready.")
    if shutil.disk_usage(store.root).free < 55 * 1024**3:
        raise ValueError("At least 55 GiB of free space is required for the image, package and intermediate copies.")
    context.phase("Preparing release configuration and source snapshots")
    root = builds.workspace(context, tools, context.job["id"])
    output = root / "Output"
    output.mkdir()
    (root / "Artifacts").symlink_to(output, target_is_directory=True)
    (root / "Tools").unlink()
    shutil.copytree(tools / "Repositories/Project/Tools", root / "Tools", ignore=shutil.ignore_patterns("Credentials"))
    docs_key = payload.get("main_sources", {}).get("r4os-docs", "r4os-docs")
    docs_commit = payload.get("source_dependencies", {}).get(docs_key)
    if not docs_commit:
        raise ValueError("A pinned Docs revision for the existing inventory check is missing.")
    clone_snapshot(context, catalog.repository_path(store, store.require_repository(docs_key)), root / "Docs", docs_commit)
    owners = {}
    for name, commit in payload["sources"].items():
        try:
            repo = store.require_repository(name)
            relative = builds.owner_relative(repo["display_name"])
        except ValueError:
            continue
        owner = root / "Repositories" / relative
        if owner.is_symlink():
            owner.unlink()
        clone_snapshot(context, catalog.repository_path(store, repo), owner, commit)
        if name not in ("r4os-sdk", "r4os-contract"):
            builds.configure_owner(root, owner, tools, output)
        owners[name] = owner
        if repo["connection_id"] == "main":
            owners[repo["display_name"]] = owner
    # Build tools use their matched source versions even when a technical owner
    # repository has moved ahead; both identities remain in the release receipt.
    for central in ("SDK", "Contract"):
        path = root / "Repositories" / central
        if not path.exists():
            path.symlink_to(tools / "Repositories" / central, target_is_directory=True)
    distribution = owners.get("r4os-distribution")
    kernel = owners.get("r4os-kernel")
    if not distribution or not kernel:
        raise ValueError("Kernel and Distribution must be selected as foundations.")
    boot, boot_record = boot_tools(context, tools)
    (root / "DevKit").unlink()
    (root / "DevKit/Boot").mkdir(parents=True)
    for name in ("Setup", "Toolchains", "SDK", "HostTools", "Emulation"):
        source = tools / "DevKit" / name
        if source.exists():
            (root / "DevKit" / name).symlink_to(source, target_is_directory=True)
    (root / "DevKit/Boot/Limine").symlink_to(boot, target_is_directory=True)
    for owner in owners.values():
        settings = owner / "Settings.R4S"
        if not settings.is_file():
            continue
        replacement = settings.read_text().replace("DEVKIT_ROOT=" + str(tools / "DevKit"), "DEVKIT_ROOT=" + str(root / "DevKit"))
        if replacement != settings.read_text():
            settings.write_text(replacement)
    for module in payload["components"]:
        builds.prepare_source_dependencies(context, root, owners[module["repository"]], module, tools, payload.get("source_dependencies", {}))
    if "r4os-libraries" in owners:
        builds.private_libraries(context, root, tools)
    version_file = distribution / "Injection/R4OS/CONFIG/VERSION.R4S"
    text = version_file.read_text(encoding="utf-8-sig")
    text, count = re.subn(r"(?m)^RELEASE_VERSION=.*$", "RELEASE_VERSION=" + version, text)
    if count != 1:
        raise ValueError("The R4OS version field is missing from Distribution.")
    version_file.write_text(text)
    if any(module["language"] == "Rust" and (owners[module["repository"]] / "Cargo.toml").is_file()
           and "r4os-library-helpers" in (owners[module["repository"]] / "Cargo.toml").read_text()
           for module in payload["components"]):
        builds.prepare_library_bindings(context, root, tools)
    artifacts, component_receipts = {}, []
    for index, module in enumerate(payload["components"], 1):
        context.check()
        name = module["repository"]
        owner = owners.get(name)
        if owner is None:
            raise ValueError("Source snapshot for module is missing: " + name)
        context.phase(f"Component {index}/{len(payload['components'])}: {module['id']}")
        module_output = output / "Modules" / name / module["id"]
        reused = reusable(context, name, module, payload["sources"][name], payload["toolset"], payload.get("source_dependencies", {}))
        reused = reused or prepared_component(context, payload, name, module)
        if reused:
            module_output.mkdir(parents=True)
            local_path = module_output / reused.name
            shutil.copy2(reused, local_path)
            paths, plan = [local_path], {"reused": True, "artifact": str(reused.relative_to(store.root)), "sha256": digest(reused)}
            if module["id"] == "kernel":
                (owner / "zig-out/bin").mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_path, owner / "zig-out/bin/r4os.elf")
        else:
            paths, plan = builds.execute_target(context, root, owner, module, tools, module_output)
        if "manifest" in module:
            artifacts[str(owner / module["manifest"])] = str(paths[0])
        if module["id"] == "R4VIDEO":
            mirror = root / "Repositories/Libraries/R4VIDEO/zig-out/R4VIDEO.R4L"
            mirror.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(paths[0], mirror)
        component_receipts.append({"repository": name, "module": module["id"], "commit": payload["sources"][name],
                                   "files": [{"name": path.name, "sha256": digest(path)} for path in paths], "plan": plan})
    context.phase("Building Distribution host tools and resources")
    zig = tools / "DevKit/Toolchains/Zig/zig"
    dist_prefix = output / "Distribution/HostTools"
    context.run([zig, "build", "-j2", "-Doptimize=ReleaseSafe", "--prefix", dist_prefix,
                 "--cache-dir", store.root / "BuildTools/Cache/Zig/Distribution",
                 "--global-cache-dir", store.root / "BuildTools/Cache/Zig/Global",
                 "--fork=" + str(root / "Repositories/SDK"), "--fork=" + str(root / "Repositories/Contract"),
                 "--fork=" + str(root / "Repositories/Libraries")], cwd=distribution, timeout=3600)
    catalog_exe, adapter = selection_catalog(context, tools, root, payload["components"])
    map_path = root / "ServerInputs/WorkspaceModules.map"
    map_path.parent.mkdir()
    map_path.write_text("\n".join(key + "|" + value for key, value in sorted(artifacts.items())) + "\n")
    adapter["project_adapter_sha256"] = project_adapter(root, catalog_exe, map_path)
    if "test_configuration" in payload:
        frozen_tests = payload["test_configuration"]
        context.phase("Test configuration: " + (frozen_tests["name"] + " · Revision " + str(frozen_tests["revision"]) if frozen_tests else "None"))
        test_overlay = testconfigs.materialize(context, frozen_tests, root / "ServerTestOverlay")
        adapter["distribution"] = distribution_adapter(root, payload["components"], test_overlay)
    else:
        # Older queued jobs keep their original profile-bound behavior.
        adapter["distribution"] = distribution_adapter(root, payload["components"], preserve_legacy=True)
    prepare_video_sources(context, root, payload["components"])
    stage = store.root / "Release/.staging" / context.job["id"]
    stage.mkdir(parents=True)
    for name in ("images", "packages", "manifests", "logs"):
        (stage / name).mkdir()
    source_record = {"schema": 1, "local": True, "release_version": version, "release_type": release,
                     "base_profile": base, "toolset": manifest, "sources": payload["sources"],
                     "components": component_receipts, "selection_adapter": adapter, "boot_tools": boot_record,
                     "created": now(), "technical": True}
    if "test_configuration" in payload:
        source_record["test_configuration"] = payload["test_configuration"]
    source_record["source_dependencies"] = payload.get("source_dependencies", {})
    generated = root / "Generated/library-bindings.json"
    if generated.is_file():
        source_record["generated_library_bindings"] = json.loads(generated.read_text())
    scripts = root / "Generated/library-script-sources.json"
    if scripts.is_file():
        source_record["library_script_sources"] = json.loads(scripts.read_text())
    sources_path = stage / "manifests" / ("R4OS-SOURCES-" + version + ".json")
    write_json(sources_path, source_record)
    result_path = root / "release-result.json"
    request_path = root / "release-request.json"
    write_json(request_path, {"workspace": str(root), "base": base, "version": version,
                              "manifest": str(sources_path), "staging": str(stage / "packages"), "result": str(result_path)})
    context.phase("Assembling, validating and packaging the release image locally")
    context.run(["pwsh", "-NoLogo", "-NoProfile", "-File", Path(__file__).with_name("release_driver.ps1"),
                 "-RequestFile", request_path], timeout=7200, env={"R4OS_PUBLIC_IMAGE": "1"})
    result = json.loads(result_path.read_text())
    inventory = json.loads(Path(result["inventory"]).read_text())
    expected = {(item["id"], item["kind"]) for item in payload["components"] if item.get("manifest")}
    actual = {(item["name"], item["kind"]) for item in inventory.get("entries", []) if item.get("kind") != "KERNEL"}
    if inventory.get("schema") != 4 or actual != expected:
        raise ValueError("The generated image inventory does not match the saved release selection.")
    write_json(stage / "manifests/server-selection.json", {"expected": sorted(expected), "release_type": release["id"], "inventory": inventory})
    shutil.move(result["image"], stage / "images/disk.img")
    shutil.copy2(result["image_record"], stage / "manifests/image.json")
    shutil.copy2(result["inventory"], stage / "manifests/MODULES.JSON")
    shutil.copy2(result["image_plan"], stage / "manifests/image-adds.txt")
    package = Path(result["package"])
    target_package = stage / "packages" / ("R4OS-" + version + "-" + release["id"] + "-x86_64.zip")
    if package != target_package:
        package.rename(target_package)
    # The packager keeps scratch copies in .packages; they are not final assets.
    packages_scratch = stage / "packages/.packages"
    if packages_scratch.exists():
        shutil.rmtree(packages_scratch)
    context.log("Release prepared successfully on the server.")
    shutil.copyfile(context.log_path, stage / "logs/build.log")
    sums = [digest(path) + "  " + str(path.relative_to(stage)) for path in sorted(stage.rglob("*")) if path.is_file()]
    (stage / "SHA256SUMS.txt").write_text("\n".join(sums) + "\n")
    context.check()
    final = store.root / "Release" / identifier(release["id"]) / identifier(version) / context.job["id"]
    final.parent.mkdir(parents=True, exist_ok=True)
    stage.rename(final)
    return {"release_type": release["id"], "version": version, "configuration_revision": release["revision"],
            "output": str(final.relative_to(store.root)), "files": builds.register_outputs(store, final)}
