"""Discover production targets; the selected SDK validates them before building."""
import json
from pathlib import Path
import re
import subprocess
import tomllib

from .process import clean_env, Cancelled
from .store import identifier, under

IGNORED = {"Tests", "Test", "Smoke", "Templates", "Examples", "ThirdParty", "Legal", ".git"}


def english_cache(local):
    """Display previously cached system labels without rewriting owner content."""
    labels = {
        "kernel": ("Produktionskernel", "Production kernel"),
        "recovery": ("Recovery-Kernel", "Recovery kernel"),
        "contract-tools": ("Contract-Werkzeuge", "Contract tools"),
        "sdk-tools": ("SDK-Werkzeuge", "SDK tools"),
        "distribution-tools": ("Image-Werkzeuge", "Image tools"),
    }
    for module in local.get("modules", []):
        prior, translated = labels.get(module.get("id"), (None, None))
        if prior and "manifest" not in module and module.get("name") == prior:
            module["name"] = translated
    details = {
        "Lokale Änderungen bleiben erhalten.": "Local changes have been preserved.",
        "Repository enthält noch keine Commits.": "The repository has no commits yet.",
        "Lokaler Branch entspricht nicht dem Standardbranch.": "The local branch differs from the default branch.",
        "Die Remote-Adresse weicht vom öffentlichen Katalog ab.": "The remote address differs from the public catalogue.",
        "Ziel ist kein verwalteter Git-Checkout.": "The destination is not a managed Git checkout.",
    }
    if local.get("detail") in details:
        local["detail"] = details[local["detail"]]
    profile = local.get("profile", {})
    if profile.get("reason") == "Dieses Repository besitzt kein unterstütztes Einzelprojekt-Buildziel.":
        profile["reason"] = "This repository has no supported individual build target."
    for item in profile.get("dependencies", []):
        if item.get("purpose") == "Quellimport":
            item["purpose"] = "Source import"
    return local


def fields(path):
    result = {}
    for raw in Path(path).read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            result.setdefault(key, []).append(value)
    return result


def package_version(root):
    path = Path(root) / "build.zig.zon"
    if path.exists():
        match = re.search(r'\.version\s*=\s*"([^"]+)"', path.read_text())
        if match:
            return match[1]
    return ""


def validate(context, root, discovered):
    toolset_key = context.store.setting("active_toolset")
    if not toolset_key:
        return discovered
    tool = under(context.store.root / "BuildTools/Metadata", identifier(toolset_key)) / "HostTools/bin/module-catalog"
    if not tool.is_file():
        return discovered
    errors = list(discovered["profile"]["errors"])
    for module in discovered["modules"]:
        module["supported"] = module["language"] in ("Zig", "C", "Rust")
        if not module["supported"]:
            module["reason"] = "No build adapter is available for this project language."
        if "manifest" not in module:
            continue
        try:
            metadata, _ = context.run([tool, "metadata", "--manifest", root / module["manifest"]], quiet=True, timeout=30)
            entry = json.loads(metadata)
            plan = entry.get("plan", {})
            for key in ("kind", "name", "language", "target"):
                if module[key] != entry[key]:
                    raise ValueError("Manifest differs from the SDK plan: " + key)
            module.update(scope=entry["image_scope"], build_profile=plan.get("build_profile", entry.get("build_mode", "explicit-project")),
                          optimization=entry["optimization"], version=entry["module_version"], validated=True, validation_toolset=toolset_key)
        except Cancelled:
            raise
        except (OSError, ValueError, RuntimeError, KeyError) as error:
            module.update(supported=False, reason=str(error))
            errors.append(module["id"] + ": " + str(error))
    discovered["profile"].update(ready=bool(discovered["modules"]) and not errors, errors=errors)
    if errors:
        discovered["profile"]["reason"] = "; ".join(errors)
    return discovered


def scan(root, name):
    root = Path(root)
    result = subprocess.run(["git", "-C", root, "ls-files", "-z", "--", "*.R4MF"],
                            env=clean_env(), capture_output=True, check=True, timeout=15)
    modules, errors = [], []
    for relative in result.stdout.decode("utf-8").split("\0"):
        if not relative or any(part in IGNORED for part in Path(relative).parts) or ("Fixtures" in Path(relative).parts and not relative.startswith("Fixtures/StandardChild/")):
            continue
        try:
            values = fields(under(root, relative))
            if values.get("R4OS_MODULE_MANIFEST") != ["2"]:
                raise ValueError("Manifest version is not supported: " + relative)
            get = lambda key, default="": values.get(key, [default])[0]
            module_name, kind = identifier(get("NAME")), get("KIND")
            if kind not in ("R4X", "R4L", "R4D", "R4P"):
                raise ValueError("Unknown module type: " + kind)
            scope = get("IMAGE_SCOPE").lower()
            if scope not in ("slim", "full", "test", "none"):
                raise ValueError("Invalid IMAGE_SCOPE: " + relative)
            modules.append({"id": module_name, "name": module_name, "kind": kind, "version": get("VERSION"),
                            "language": get("LANGUAGE"), "scope": scope, "manifest": relative,
                            "target": get("TARGET"), "role": get("MODULE_ROLE", get("APP_CLASS")),
                            "imports": values.get("IMPORT", []), "exports": values.get("EXPORT", []), "sources": values.get("SOURCE", []),
                            "native_archives": values.get("NATIVE_ARCHIVE", []), "includes": values.get("C_INCLUDE", []),
                            "zig_modules": values.get("ZIG_MODULE", []),
                            "validated": False})
        except (OSError, ValueError) as error:
            errors.append(str(error))
    special = {
        "r4os-kernel": [("kernel", "Production kernel", "ELF", "Rust")],
        "r4os-recovery": [("recovery", "Recovery kernel", "ELF", "Zig")],
        "r4os-contract": [("contract-tools", "Contract tools", "HostTools", "Zig")],
        "r4os-sdk": [("sdk-tools", "SDK tools", "HostTools", "Zig")],
        "r4os-distribution": [("distribution-tools", "Image tools", "HostTools", "Zig")],
    }
    for key, label, kind, language in special.get(name, []):
        modules.append({"id": key, "name": label, "kind": kind, "language": language, "scope": "none", "validated": False})
    dependencies = []
    for path in [root / "build.zig.zon", *[root / Path(module["manifest"]).parent / "build.zig.zon" for module in modules if "manifest" in module]]:
        if path.exists():
            for repo, commit in re.findall(r"https://github.com/R4OSDev/([A-Za-z0-9_.-]+)/archive/([0-9a-f]{40})", path.read_text()):
                item = {"repository": repo, "commit": commit, "source": str(path.relative_to(root))}
                if item not in dependencies:
                    dependencies.append(item)
    roles = {"Apps": "r4os-app-", "Services": "r4os-service-", "Diagnostics": "r4os-diagnostic-", "Drivers": "r4os-driver-", "Protocols": "r4os-protocol-", "Subsystems": "r4os-subsystem-"}
    for module in modules:
        if "manifest" not in module:
            continue
        folder = root / Path(module["manifest"]).parent
        if not (folder / "build.zig").is_file() and (root / "build.zig").is_file():
            folder = root
        texts = [(folder / "build.zig").read_text() if (folder / "build.zig").is_file() else "", "\n".join(module["zig_modules"])]
        required = {roles[role] + owner.lower() for role, owner in re.findall(r'(?:\.\./)+(Apps|Services|Diagnostics|Drivers|Protocols|Subsystems)/([A-Za-z0-9_-]+)/', "\n".join(texts))}
        zone = folder / "build.zig.zon"
        if zone.is_file():
            required |= {repo for repo in re.findall(r'https://github.com/R4OSDev/([A-Za-z0-9_.-]+)/archive/', zone.read_text()) if repo not in {"r4os-sdk", "r4os-contract", "r4os-libraries"}}
        module["source_repositories"] = sorted(required - {name})
        for repo in module["source_repositories"]:
            item = {"repository": repo, "purpose": "Source import", "source": module["manifest"]}
            if item not in dependencies:
                dependencies.append(item)
    cargo_package_version = ""
    cargo = root / "Cargo.toml"
    if cargo.is_file():
        manifest = tomllib.loads(cargo.read_text())
        cargo_package_version = manifest.get("package", {}).get("version", "")
        for package, spec in manifest.get("dependencies", {}).items():
            if not isinstance(spec, dict):
                continue
            entry = {"package": package, "source": "Cargo.toml"}
            for key in ("git", "rev", "path", "version", "features"):
                if key in spec:
                    entry[key] = spec[key]
            if len(entry) > 2:
                dependencies.append(entry)
    return {"modules": modules, "profile": {"detected": bool(modules), "errors": errors, "dependencies": dependencies,
             "package_version": package_version(root), "cargo_package_version": cargo_package_version,
             "reason": "" if modules else "This repository has no supported individual build target."}}
