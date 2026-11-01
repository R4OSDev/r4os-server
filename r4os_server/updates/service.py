"""Update listener with client downloads and separately authorized management."""
from pathlib import Path
import json
import sqlite3
import re
from urllib.parse import parse_qs,urlsplit

from ..common.access import AccessPolicy
from ..common.http import HTTPError, configure_server
from ..common import settings
from ..registration.state import StateStore as AccessStore
from .state import StateStore
from .catalog import Catalog
from .update_gateway import UpdateGatewayServer, UpdateGatewayHandler
from .uploads import Uploads


class Handler(UpdateGatewayHandler):
    def route(self):
        try:
            path=urlsplit(self.path).path
            if path=="/api/health" and self.command in ("GET","HEAD"):
                return self.json(200,{"service":"updates","status":"ok","version":self.server.version})
            if path.startswith("/api/settings/"):
                status,result=settings.dispatch(self,self.server.policy)
                return self.json(status,result)
            if path.startswith("/api/updates/"):
                status,result=self.server.management(self,path)
                return self.json(status,result)
            if self.command not in ("GET","HEAD"):
                raise HTTPError(405,"method_not_allowed")
            return self._handle(self.command=="HEAD")
        except HTTPError as error:
            self.json(error.status,error.body)
        except (ValueError, UnicodeError) as error:
            self.json(422,{"error":"validation_error","detail":str(error)})
        except sqlite3.Error:
            self.json(503,{"error":"database_unavailable"})
        except (BrokenPipeError,ConnectionResetError):
            return

    do_GET=do_HEAD=do_PUT=do_POST=route


class Server(UpdateGatewayServer):
    def management(self,handler,path):
        actor=self.policy.require_role(handler,("admin","publisher"))
        if handler.command=="PUT" and path in ("/api/updates/uploads/package","/api/updates/uploads/manifest"):
            return 202,self.uploads.receive(handler,path.rsplit('/',1)[1],actor)
        match=re.fullmatch(r"/api/updates/uploads/([0-9a-f]{32})(/publish)?",path)
        if match:
            if match[2] and handler.command=="POST":
                self.policy.require_role(handler,("admin",))
                # Publication is an explicit action with a small JSON object.
                if json.loads(handler.body()) != {}:
                    raise HTTPError(422,"validation_error","An empty JSON object is required")
                return 200,self.uploads.publish(match[1],handler.source_ip())
            if not match[2] and handler.command in ("GET","HEAD"):
                item=self.uploads.get(match[1])
                item['publication']=self.uploads.readiness(item)
                return 200,item
            raise HTTPError(405,"method_not_allowed")
        if handler.command in ("GET","HEAD") and path=="/api/updates/uploads":
            query=parse_qs(urlsplit(handler.path).query,keep_blank_values=True,max_num_fields=4)
            if set(query)-{'limit','offset','kind','exclude_published'} or any(len(v)!=1 for v in query.values()):
                raise HTTPError(422,"invalid_pagination")
            exclude=query.get('exclude_published',['false'])[0]
            if exclude not in ('true','false'):
                raise HTTPError(422,'validation_error')
            return 200,self.uploads.list(int(query.get('limit',['200'])[0]),int(query.get('offset',['0'])[0]),query.get('kind',[None])[0],exclude=='true')
        if handler.command in ("GET","HEAD") and path=="/api/updates/state":
            return 200,{"incoming":[p.to_dict() for p in self.catalog.list_incoming()],
                        "released":[p.to_dict() for p in self.catalog.list_released()],
                        "incoming_manifests":[p.to_dict() for p in self.catalog.list_incoming_release_manifests()],
                        "released_manifests":[p.to_dict() for p in self.catalog.list_released_release_manifests()],
                        "catalog":self.catalog.public_catalog(self.public_port),
                        "catalog_errors":self.catalog.catalog_errors(),
                        "uploads":self.uploads.list(),
                        "upload_counts":self.uploads.counts(),
                        "audit":[a for a in self.store.list_audit() if a['service']=="updates"]}
        raise HTTPError(404,"not_found")

    def server_close(self):
        super().server_close()
        if hasattr(self,"uploads") and getattr(self,"owns_uploads",True):
            self.uploads.close()


def create_server(config):
    access=AccessStore(config.data_dir/"registration/state.sqlite3",60)
    state=StateStore(config.data_dir/"updates/state.sqlite3")
    catalog=Catalog(state,config.data_dir/"updates/incoming",config.data_dir/"updates/releases")
    catalog.base_url=config.updates_url
    server=Server((config.host,config.updates_port),access,catalog,config.attempt_limit,
                  config.block_seconds,config.updates_port)
    server.RequestHandlerClass=Handler
    server.policy=AccessPolicy(config,access,"updates")
    server.version=(Path(__file__).resolve().parents[2]/"VERSION").read_text().strip()
    configure_server(server,config)
    try:
        server.uploads=Uploads(config,catalog,access)
    except BaseException:
        server.server_close()
        raise
    return server


def create_public_server(config, private):
    server=Server((config.public_api_host,config.updates_port),private.store,private.catalog,
                  config.attempt_limit,config.block_seconds,config.updates_port)
    server.RequestHandlerClass=Handler
    server.policy=private.policy
    server.version=private.version
    server.uploads=private.uploads
    server.owns_uploads=False
    try:
        return configure_server(server,config,public=True)
    except BaseException:
        server.server_close()
        raise
