"""One process per service, sharing persistent registration state."""

import argparse
import importlib
import json
import logging
import os
import signal
import sqlite3
import threading

from .common.config import Config


def main():
    parser = argparse.ArgumentParser(description="R4OS Server Dienste")
    parser.add_argument("service", choices=("registration", "software", "updates", "benchmarks"))
    parser.add_argument("--config", default=os.environ.get("R4OS_SERVER_CONFIG", "/etc/r4os-server/server.json"))
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = Config.load(args.config)
        if args.check_config:
            print(json.dumps(config.public(), ensure_ascii=False))
            return 0
        module = importlib.import_module(f"r4os_server.{args.service}.service")
        servers, threads = [], []
        stopped, failed = threading.Event(), threading.Event()
        def serve(server):
            try:
                server.serve_forever()
            except Exception:
                failed.set()
                logging.exception("Dienstlistener ausgefallen")
            finally:
                stopped.set()
        try:
            servers.append(module.create_server(config))
            if config.public_api_host:
                servers.append(module.create_public_server(config, servers[0]))
            for signum in (signal.SIGTERM,signal.SIGINT):
                signal.signal(signum,lambda _signum,_frame: stopped.set())
            for server in servers:
                logging.info("%s on %s:%s", args.service, *server.server_address)
                thread=threading.Thread(target=serve,args=(server,),daemon=True)
                thread.start()
                threads.append((server,thread))
            stopped.wait()
        finally:
            for server,thread in threads:
                server.shutdown()
                thread.join(timeout=5)
            for server in reversed(servers):
                server.server_close()
        if failed.is_set():
            return 1
    except (OSError, ValueError, sqlite3.Error) as error:
        logging.error("Start fehlgeschlagen: %s", error)
        return 1
    except KeyboardInterrupt:
        return 0
    return 0
