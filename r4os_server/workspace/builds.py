"""Pinned project builds through the selected SDK's owner builders."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from . import catalog, sdk_packages, sdk_projects, toolsets
from .profiles import fields
from .process import clean_env
from .store import identifier, now, under
from .toolsets import active, clone_snapshot, digest, write_json

ROLES = {"r4os-app-":"Apps","r4os-games-":"Apps","r4os-service-":"Services","r4os-diagnostic-":"Diagnostics",
         "r4os-driver-":"Drivers","r4os-protocol-":"Protocols","r4os-subsystem-":"Subsystems"}
CENTRAL = {"r4os-kernel":"Kernel","r4os-sdk":"SDK","r4os-contract":"Contract","r4os-libraries":"Libraries",
           "r4os-distribution":"Distribution","r4os-recovery":"Recovery"}


def selection(store, name):
    options=sdk_projects.get(store,name)
    path,tools=active(store)
    if options['sdk_id'] and tools['sdk_id']!=options['sdk_id']:
        matches=[]
        for candidate in (store.root/'BuildTools/Toolsets').glob('*/toolset.json'):
            try:
                candidate_path,candidate_tools=toolsets.load(store,candidate.parent.name)
                if candidate_tools['sdk_id']==options['sdk_id']:matches.append((candidate_path,candidate_tools))
            except ValueError:continue
        if len(matches)!=1:raise ValueError('Set up the SDK selected for this repository.')
        path,tools=matches[0]
    return path,tools,sdk_packages.get(store,tools['sdk_id']),options


def inputs(store,value):
    repo=store.require_repository(value.get('name',''));local=repo['local']
    module=next((dict(item) for item in local.get('modules',[]) if item['id']==value.get('target')),None)
    if module is None:raise ValueError('The build target was not found in the repository.')
    if module.get('supported') is False:raise ValueError(module.get('reason','The SDK has not validated this target.'))
    if not local.get('commit') or local.get('dirty'):raise ValueError('A downloaded, clean source revision is required.')
    _,tools,package,options=selection(store,repo['name'])
    declared=module.get('sdk_profile')
    if not declared or declared.get('sdk_id')!=package['id']:
        role='native' if module['id'].startswith('native-') else 'module' if module.get('manifest') else 'kernel' if module['id']=='kernel' else 'recovery' if module['id']=='recovery' else 'host'
        choices=[name for name,profile in package['manifest']['profiles'].items() if profile['role']==role and profile['language']==module['language']]
        if len(choices)!=1:raise ValueError('Refresh the repository to obtain its SDK-owned profile selection.')
        declared=sdk_packages.provenance(package,choices[0])
    if declared.get('profile') not in package['manifest']['profiles']:raise ValueError('Unknown SDK profile.')
    module['sdk_profile']=declared;module['source_ids']={};sources={}
    bound={'r4os-sdk':package['manifest']['sdk_source'],**{'r4os-'+key:package['manifest']['sources'][key] for key in ('contract','libraries')}}
    if repo['display_name'] in bound and local['commit']!=bound[repo['display_name']]['commit']:
        raise ValueError('Update this source repository to the exact commit selected by the SDK before building it.')
    system={'r4os-sdk','r4os-contract','r4os-libraries'}
    for name in module.get('source_repositories',[]):
        if name in system:continue
        source=store.source_repository(name,repo['connection_id'])
        if not source['local'].get('commit') or source['local'].get('dirty'):raise ValueError(name+': a clean local source dependency is required.')
        sources[source['name']]=source['local']['commit'];module['source_ids'][name]=source['name']
    for extra in options['libraries']:
        source=store.require_repository(extra['repository'])
        if not source['local'].get('commit') or ('commit' in extra and source['local']['commit']!=extra['commit']) or source['local'].get('dirty'):raise ValueError(extra['id']+': additional library pin differs from the local source.')
        sources[source['name']]=source['local']['commit']
    payload={'target':module['id'],'module':module,'commit':local['commit'],'toolset':tools['id'],'sdk_id':package['id'],
             'sdk_profile':declared,'source_dependencies':sources,'additional_libraries':options['libraries'],'development_id':options['development_id']}
    return repo,payload


def enqueue(store,value):
    repo,payload=inputs(store,value);return store.enqueue('build',repo['name'],payload)


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


def workspace(context,tools,key):
    root=context.store.root/'BuildTools/Workspaces'/identifier(key)
    if root.exists():raise ValueError('A build working directory with this ID already exists.')
    (root/'Repositories').mkdir(parents=True)
    manifest=sdk_packages.strict_json((tools/'toolset.json').read_bytes())
    paths=toolsets.package_paths(context.store,manifest)
    shutil.copytree(paths['SDK'],root/'Repositories/SDK')
    for name in ('Contract','Libraries'):
        shutil.copytree(paths[name],root/'Repositories'/name)
    (root/'Repositories/Recovery').symlink_to(paths['Recovery'],target_is_directory=True)
    (root/'Repositories/Tools').symlink_to(paths['HostTools'],target_is_directory=True)
    (root/'Install').symlink_to(tools/'Install',target_is_directory=True)
    # Current owner Settings use this name only for the SDK-selected install location.
    (root/'DevKit').symlink_to(tools/'Install',target_is_directory=True)
    return root


def configure_owner(root,owner,tools,output):
    if owner in (root/'Repositories/SDK',root/'Repositories/Contract',root/'Repositories/Libraries',root/'Repositories/Recovery'):
        return  # SDK-bound source originals include their location-only Settings.
    values={'WORKSPACE_ROOT':str(root),'REPOSITORIES_ROOT':str(root/'Repositories'),
            'SDK_ROOT':str(root/'Repositories/SDK'),'CONTRACT_ROOT':str(root/'Repositories/Contract'),
            'LIBRARIES_ROOT':str(root/'Repositories/Libraries'),'KERNEL_ROOT':str(root/'Repositories/Kernel'),
            'DISTRIBUTION_ROOT':str(root/'Repositories/Distribution'),'INSTALL_ROOT':str(tools/'Install'),
            'DEVKIT_ROOT':str(tools/'Install'),'ARTIFACTS_ROOT':str(output),'ZIG_ROOT':str(tools/'Install/Toolchains/Zig'),
            'RUST_TOOLS_ROOT':str((root/'Repositories/SDK').resolve().parent/'Tools'),
            'APPS_ROOT':'Apps','SERVICES_ROOT':'Services','DIAGNOSTICS_ROOT':'Diagnostics','DRIVERS_ROOT':'Drivers',
            'PROTOCOLS_ROOT':'Protocols','SUBSYSTEMS_ROOT':'Subsystems','LIMINE_ROOT':str(tools/'Install/Boot/Limine'),
            'QEMU_ROOT':str(tools/'Install/Emulation/QEMU'),'DISTRIBUTION_OUTPUT_ROOT':'Distribution',
            'INPUT_ROOT':'Distribution/Inputs','PRIVATE_INJECTION_ROOT':'Distribution/PrivateInjection'}
    (owner/'Settings.R4S').write_text('# Generated SDK build paths; versions are selected by SDK profiles.\n'+'\n'.join(key+'='+value for key,value in values.items() if value)+'\n')


def prepare_source_dependencies(context,root,owner,module,tools,sources):
    for name,source_id in module.get('source_ids',{}).items():
        destination=root/'Repositories'/owner_relative(name)
        if not destination.exists():
            clone_snapshot(context,catalog.repository_path(context.store,context.store.require_repository(source_id)),destination,sources[source_id])
    extras=[]
    for extra in context.job['payload'].get('additional_libraries',[]):
        destination=root/'Additional'/extra['id'];destination.parent.mkdir(exist_ok=True)
        source=catalog.repository_path(context.store,context.store.require_repository(extra['repository']))
        if 'commit' in extra:
            clone_snapshot(context,source,destination,extra['commit'])
            item={'id':extra['id'],'root':str(destination),'commit':extra['commit']}
            if extra.get('path'):item['path']=extra['path']
        else:
            snapshot(context,extra['repository'],sources[extra['repository']],destination)
            scope=destination/extra.get('path','')
            item={'id':extra['id'],'root':str(scope),'tree':extra['tree']}
        extras.append(item)
    if extras:write_json(root/'additional-libraries.json',extras)


def private_libraries(context,root,tools):
    return root/'Repositories/Libraries'


def prepare_library_bindings(context,root,tools):
    # SDK native-input helpers own their compiler projections and receipts.
    return root/'Repositories/Libraries'


def module_build(context,root,owner,module,tools,output):
    manifest=sdk_packages.strict_json((tools/'toolset.json').read_bytes())
    paths=toolsets.package_paths(context.store,manifest)
    sdk,contract,libraries=root/'Repositories/SDK',root/'Repositories/Contract',root/'Repositories/Libraries'
    development=context.job['payload'].get('development_id');development_file=None
    if development:
        roots,development_file=sdk_projects.development(context.store,development,manifest['sdk_id'])
        sdk,contract,libraries=roots['sdk'],roots['contract'],roots['libraries']
    selected=context.job['payload']['sdk_profile'];key=manifest['sdk_id']
    extras=root/'additional-libraries.json'
    # Resolve before compilation. The SDK checks system providers and all own pins.
    if development_file:
        result=root/'development-resolution.json'
        context.run(['pwsh','-NoLogo','-NoProfile','-File',sdk/'Tools/Profile.ps1','-Action','Resolve','-Profile',selected['profile'],
                     '-ContractRoot',contract,'-LibrariesRoot',libraries,'-DevelopmentProfile',development_file,'-Output',result],timeout=600)
    else:sdk_packages.resolve(context,key,selected['profile'],additional=extras if extras.exists() else None)
    module_root=owner/Path(module['manifest']).parent
    bound=any(owner.resolve().is_relative_to((root/'Repositories'/name).resolve()) for name in ('SDK','Contract','Libraries','Recovery'))
    if (module_root/'build.zig.zon').is_file() and not bound:
        context.run(['pwsh','-NoLogo','-NoProfile','-File',sdk/'Tools/Sync-ComponentPins.ps1','-SdkRoot',sdk,'-ProjectRoot',module_root,'-Write'],timeout=120)
    # Use the SDK's explicit Rust owner entry point. In SDK 0.64.145 the generic
    # wrapper forwards a stale native exit code after a successful Rust receipt.
    script='Build-RustModule.ps1' if module['language']=='Rust' else 'Build-Project.ps1'
    args=['pwsh','-NoLogo','-NoProfile','-File',sdk/'Tools'/script,'-ProjectRoot',module_root,
          '-SdkRoot',sdk,'-ContractRoot',contract,'-LibrariesRoot',libraries,'-ToolsRoot',paths['HostTools'],
          '-InstallRoot',tools/'Install','-ZigRoot',tools/'Install/Toolchains/Zig',
          '-CacheRoot',context.store.root/'BuildTools/Cache/SDK'/manifest['sdk_id'],'-OutputRoot',output]
    if extras.exists():args+=['-AdditionalLibrariesFile',extras]
    if development_file:args+=['-DevelopmentProfile',development_file,'-Technical']
    dependency_roots=[]
    for name,source_id in module.get('source_ids',{}).items():
        dependency_roots.append(str(root/'Repositories'/owner_relative(name)))
    if extras.exists():
        for extra in sdk_packages.strict_json(extras.read_bytes()):
            dependency_roots.append(str(Path(extra['root'])/extra.get('path','')))
    if dependency_roots and module['language']=='Rust':args+=['-DependencyRootsJson',json.dumps(sorted(set(dependency_roots)))]
    context.run(args,timeout=3600)
    receipt=sdk_packages.strict_json((output/'build.json').read_bytes());proof=receipt.get('sdk_profile',{})
    if (receipt.get('status')!='success' or proof.get('id')!=selected['profile'] or proof.get('hashes')!=selected['profile_sources']
            or bool(proof.get('technical'))!=bool(development_file)):
        raise ValueError('The SDK build receipt does not match the pinned profile.')
    artifact=output/(module['id']+'.'+module['kind'])
    if not artifact.is_file() or artifact.stat().st_size==0:raise ValueError('The SDK did not produce the selected target artifact.')
    proof=receipt.get('artifact',{})
    if proof.get('file')!=artifact.name or proof.get('bytes')!=artifact.stat().st_size or proof.get('sha256')!=digest(artifact):
        raise ValueError('The artifact differs from its SDK receipt.')
    return [artifact],receipt


def execute_target(context,root,owner,module,tools,output):
    if module.get('manifest'):return module_build(context,root,owner,module,tools,output)
    from .sdk_owners import execute
    return execute(context,root,owner,module,tools,output)


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
    tools, tool_manifest = toolsets.load(store, tool_key)
    if payload.get("sdk_id") != tool_manifest["sdk_id"] or payload.get("sdk_profile", {}).get("profile_sources") != tool_manifest["sdk_profile_sources"]:
        raise ValueError("This job uses a removed or different SDK contract. Start a new SDK build.")
    if tool_manifest.get("status") != "ready":
        raise ValueError("The selected toolset is not ready.")
    sdk_packages.verify(context,payload["sdk_id"])
    local = catalog.inspect(context, name, remote=False)
    modules = [item for item in local.get("modules", []) if item["id"] == payload["target"]]
    if not modules and not payload.get('module'):
        raise ValueError("The build target is no longer available.")
    module = payload.get("module") or modules[0]
    root = workspace(context, tools, context.job["id"])
    owner = root / "Repositories" / owner_relative(store.repository(name)["display_name"], module, name)
    context.phase("Preparing source snapshot for " + name)
    if not owner.exists():snapshot(context,name,payload['commit'],owner)
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
    receipt = {"schema": 2, "status": "success", "repository": name, "commit": payload["commit"], "module": module,
               "toolset": tool_key, "tools": tool_manifest["versions"], "sources": tool_manifest["sources"],
               "plan": plan, "sdk_profile": payload["sdk_profile"], "technical": bool(payload.get("development_id")), "started": context.job.get("started"), "finished": now()}
    receipt["source_dependencies"] = payload.get("source_dependencies", {})
    receipt["additional_libraries"] = payload.get("additional_libraries", [])
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
            "sdk_profile": payload["sdk_profile"], "technical": bool(payload.get("development_id")), "files": register_outputs(store, final), "output": str(final.relative_to(store.root))}
