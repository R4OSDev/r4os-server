"""Software HTTPS listener using the shared registration and access policy."""
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

from ..common.access import AccessPolicy
from ..common.http import Handler as BaseHandler, HTTPError, LANHTTPServer, configure_server
from ..registration.state import StateStore


class Handler(BaseHandler):
    def route(self):
        try:
            path = urlsplit(self.path).path
            if self.command not in ('GET', 'HEAD'):
                raise HTTPError(405, 'method_not_allowed')
            if path == '/api/health':
                return self.json(200, {'service':'software','status':'ok','version':self.server.version})
            self.server.policy.client(self)
            if path == '/api/v1/packages' or path.startswith('/api/v1/packages/'):
                raise HTTPError(503, 'catalog_not_ready', 'The software catalogue is not available yet.')
            raise HTTPError(404, 'not_found')
        except HTTPError as error:
            self.json(error.status, error.body)
        except (sqlite3.Error, OSError):
            self.json(503, {'error':'storage_unavailable'})

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = route


def create_server(config):
    server = LANHTTPServer((config.host, config.software_port), Handler)
    server.workspace_root = Path(config.workspace_dir or config.data_dir/'workspace')
    access = StateStore(config.data_dir/'registration/state.sqlite3', 60)
    server.policy = AccessPolicy(config, access, 'software')
    server.version = (Path(__file__).resolve().parents[2]/'VERSION').read_text().strip()
    try:
        return configure_server(server, config)
    except BaseException:
        server.server_close()
        raise


def create_public_server(config, private):
    server = LANHTTPServer((config.public_api_host, config.software_port), Handler)
    server.workspace_root, server.policy, server.version = private.workspace_root, private.policy, private.version
    try:
        return configure_server(server, config, public=True)
    except BaseException:
        server.server_close()
        raise
