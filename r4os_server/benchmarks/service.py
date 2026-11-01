"""Spectra and SNES share LAN admission, but keep independent data contracts."""
import logging
import sqlite3
from urllib.parse import urlsplit

from ..common.access import AccessPolicy
from ..common.http import Handler, HTTPError, LANHTTPServer, configure_server
from ..registration.state import StateStore as AccessStore
from .api import Application
from .models import InvalidData, loads


def api_handler(app,policy):
    class API(Handler):
        def route(self):
            try:
                path=urlsplit(self.path).path.rstrip('/')
                if not (path=='/api/health' and self.command in ('GET','HEAD')):
                    if self.command not in ('GET','HEAD','PUT'):
                        raise HTTPError(405,'method_not_allowed')
                    policy.client(self,write=self.command=='PUT')
                document=loads(self.body(policy.config.benchmark_limit)) if self.command=='PUT' else None
                status,result=app.dispatch(self.command,self.path,document)
                if self.command=='PUT':
                    policy.store.audit('benchmark_stored' if status in (200,201) else 'benchmark_rejected',self.source_ip(),path+' '+str(status),'benchmarks')
                self.json(status,result)
            except HTTPError as error:
                self.json(error.status,error.body,{'WWW-Authenticate':'Bearer realm="R4OS Server"'} if error.status==401 else None)
            except InvalidData as error:
                self.json(422,{'error':'validation_error','path':error.path,'detail':error.message})
            except sqlite3.Error:
                logging.exception('Benchmark database unavailable')
                self.json(503,{'error':'database_unavailable'})
            except (BrokenPipeError,ConnectionResetError):
                return

        do_GET=do_HEAD=do_PUT=do_POST=do_PATCH=do_DELETE=route
    return API


def create_server(config,*,store=None):
    access=AccessStore(config.data_dir/'registration/state.sqlite3',60)
    app=Application(config,store=store)
    policy=AccessPolicy(config,access,'benchmarks')
    server=LANHTTPServer((config.host,config.benchmarks_port),api_handler(app,policy))
    server.app=app
    server.policy=policy
    return configure_server(server,config)


def create_public_server(config, private):
    server=LANHTTPServer((config.public_api_host,config.benchmarks_port),api_handler(private.app,private.policy))
    server.app=private.app
    server.policy=private.policy
    try:
        return configure_server(server,config,public=True)
    except BaseException:
        server.server_close()
        raise
