"""Special owner builds use SDK-selected profiles, tools and component sources."""
import json
from pathlib import Path
import shutil

from . import sdk_packages, toolsets


def execute(context,root,owner,module,tools,output):
    manifest=sdk_packages.strict_json((tools/'toolset.json').read_bytes())
    package=sdk_packages.get(context.store,manifest['sdk_id'])
    selected=context.job['payload']['sdk_profile'];profile=selected['profile']
    resolved=sdk_packages.resolve(context,package['id'],profile)
    sdk=root/'Repositories/SDK';zig=tools/'Install/Toolchains/Zig/zig'
    output.mkdir(parents=True,exist_ok=True)
    if module['id']=='kernel':
        if 'Sdk' not in (owner/'Build.ps1').read_text(encoding='utf-8-sig') or 'RustToolchain.json' in (owner/'Build.ps1').read_text(encoding='utf-8-sig'):
            raise ValueError('Update the Kernel source to an SDK-profile-aware revision before building.')
        if (owner/'build.zig.zon').is_file():
            context.run(['pwsh','-NoLogo','-NoProfile','-File',sdk/'Tools/Sync-ComponentPins.ps1','-SdkRoot',sdk,'-ProjectRoot',owner,'-IncludeLibraries','-Write'],timeout=120)
        # Owner scripts select kernel code; SDK profiles select its toolchain.
        context.run(['pwsh','-NoLogo','-NoProfile','-File',owner/'Build.ps1','build'],cwd=owner,
                    env={'R4OS_SDK_ROOT':str(sdk),'R4OS_SDK_INSTALL_ROOT':str(tools/'Install')},timeout=3600)
        paths=[owner/'zig-out/bin/r4os.elf']
        record=sdk_packages.strict_json((owner/'zig-out/bin/r4os.build.json').read_bytes())
        proof=record.get('sdk_profile',{})
        if proof.get('id')!=profile or proof.get('hashes')!=selected['profile_sources'] or proof.get('technical') is not False or record.get('sha256')!=sdk_packages.digest(paths[0]):
            raise ValueError('The Kernel artifact has no matching SDK owner receipt.')
    elif module['id']=='recovery':
        # The normal recovery build consumes exactly the SDK-selected frozen tree.
        supplied=toolsets.package_paths(context.store,manifest)['Recovery']
        work=root/'RecoveryOwner'
        shutil.copytree(supplied,work)
        context.run(['pwsh','-NoLogo','-NoProfile','-File',work/'Build.ps1','-Mode','Kernel','-SdkRoot',sdk,'-Zig',zig],cwd=work,timeout=3600)
        paths=[work/'Artifacts/Kernel/bin/recovery.elf']
        proof=work/'Artifacts/SDKProfile/profile.json'
        if not proof.is_file():raise ValueError('Recovery did not produce its SDK profile receipt.')
        record=sdk_packages.strict_json(proof.read_bytes())
        if record.get('hashes')!=selected['profile_sources'] or record.get('profile')!=profile:raise ValueError('Recovery SDK profile receipt differs.')
        shutil.copy2(proof,output/'sdk-owner-profile.json')
    elif module['id'].startswith('native-'):
        unit=module['id'].removeprefix('native-').upper()
        if unit not in {item['name'] for item in package['manifest']['system_libraries'].values()}:
            raise ValueError('The native owner is not a system library selected by this SDK.')
        libraries=root/'Repositories/Libraries'
        # Paths retain the package's unmodified Settings; SDK checks all native pins.
        native=libraries/unit/'Tools/Build.ps1'
        if not native.is_file():raise ValueError('This SDK has no native owner entry point for the selected library.')
        options=['-OutputFile',output/(unit+'.a'),'-ResultFile',output/'native-result.json'] if unit=='R4NAK' else ['-OutputRoot',output]
        context.run(['pwsh','-NoLogo','-NoProfile','-File',native,*options],cwd=libraries,timeout=7200)
        paths=[path for path in output.rglob('*.a') if path.is_file()]
    else:
        # Host tools are built from a byte-identical SDK source copy through its
        # owner wrapper. No independently patched SDK catalogs are generated.
        selectors={'sdk-tools':['-Dpackager-only=true','-Dcatalog-only=true','-Dr4l-contract-generator-only=true'],
                   'contract-tools':['-Dgenerator-only=true'], 'distribution-tools':['-Dhost-tools-only=true']}
        if module['id'] not in selectors:raise ValueError('The SDK has no supported owner build adapter for this target.')
        build_owner=sdk if module['id']=='sdk-tools' else owner
        for option in selectors[module['id']]:
            script=sdk/'Tools/BuildOwner.ps1'
            command="& '"+str(script).replace("'","''")+"' -OwnerRoot '"+str(build_owner).replace("'","''")+"' -BuildArguments @('--prefix="+str(output).replace("'","''")+"', '"+option+"')"
            context.run(['pwsh','-NoLogo','-NoProfile','-Command',command],cwd=build_owner,timeout=3600)
        paths=[path for path in (output/'bin').rglob('*') if path.is_file()]
    if not paths or any(not path.is_file() or not path.stat().st_size for path in paths):raise ValueError('Expected SDK owner output is missing.')
    return paths,{'sdk_profile':selected,'resolved':resolved}
