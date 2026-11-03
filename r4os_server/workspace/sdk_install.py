"""One existing queue job prepares and activates one immutable SDK release."""
from . import sdk_catalog, sdk_packages, toolsets


def request(value):
    if not isinstance(value, dict) or set(value) != {'release_url'}:
        raise ValueError('Select the SDK release_url.')
    sdk_packages.release_base(value['release_url'])
    return value


def select(context, value):
    value = request(value)
    context.phase('Preparing the selected SDK release')
    imported = sdk_packages.import_release(context, value)
    package = sdk_packages.get(context.store, imported['sdk_id'])
    names = sdk_catalog.required_tools(package)
    for environment in toolsets.overview(context.store)['toolsets']:
        if environment.get('sdk_id') == package['id'] and sdk_catalog.prepared(context.store, package, environment):
            context.phase('Checking the installed SDK environment before activation')
            return toolsets.perform(context, 'activate', {'id': environment['id']})
    context.phase('Setting up the SDK build tools and bound components')
    return toolsets.perform(context, 'install', {'sdk_id': package['id'], 'tools': names})
