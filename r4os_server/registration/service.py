"""Common TCP registration; its state is shared with all data services."""
from ..common.http import configure_server
from .authentication import AuthenticationServer
from .state import StateStore


def create_server(config):
    store = StateStore(config.data_dir / "registration/state.sqlite3", 60)
    server = AuthenticationServer((config.host, config.registration_port), store,
                                  config.registration_secret, config.block_seconds)
    return configure_server(server, config, tls=False)


def create_public_server(config, private):
    server = AuthenticationServer((config.public_api_host, config.registration_port), private.store,
                                  config.registration_secret, config.block_seconds)
    try:
        return configure_server(server, config, public=True)
    except BaseException:
        server.server_close()
        raise
