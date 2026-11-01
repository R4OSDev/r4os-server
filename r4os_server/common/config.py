"""Private administration and explicit public API bindings; never serialize secrets."""

from dataclasses import dataclass, fields
import ipaddress
import json
from pathlib import Path
from urllib.parse import urlsplit

LAN_NETWORKS = tuple(ipaddress.ip_network(n) for n in
                     ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "127.0.0.0/8"))
PORTS = {"web": 4020, "registration": 4021, "software": 4022, "updates": 4023, "benchmarks": 4024}


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    data_dir: Path = Path("/var/lib/r4os-server")
    workspace_dir: str = ""
    project_root: str = ""
    allowed_networks: tuple = tuple(str(n) for n in LAN_NETWORKS)
    trusted_proxies: tuple = ()
    public_api_host: str = ""
    public_api_networks: tuple = ("0.0.0.0/0",)
    web_port: int = 4020
    registration_port: int = 4021
    software_port: int = 4022
    updates_port: int = 4023
    benchmarks_port: int = 4024
    registration_secret: str = ""
    publisher_token: str = ""
    benchmark_token: str = ""
    admin_token: str = ""
    session_secret: str = ""  # Ignored legacy WebApp settings, retained for config compatibility.
    admin_password_hash: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    tls_ca: str = ""
    public_updates_url: str = ""
    upload_limit: int = 4 * 1024**3
    benchmark_limit: int = 64 * 1024**2
    request_timeout: int = 30
    upload_timeout: int = 600
    attempt_limit: int = 3
    block_seconds: int = 3600
    validation_workers: int = 2

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        unknown = set(data) - {f.name for f in fields(cls)}
        if unknown:
            raise ValueError("Unknown configuration fields: " + ", ".join(sorted(unknown)))
        if "data_dir" in data:
            data["data_dir"] = Path(data["data_dir"])
        for key in ("allowed_networks", "trusted_proxies", "public_api_networks"):
            if key in data:
                data[key] = tuple(data[key])
        config = cls(**data)
        config.validate()
        return config

    def validate(self):
        address = ipaddress.IPv4Address(self.host)
        if not any(address in network for network in LAN_NETWORKS):
            raise ValueError("The bind address must be a specific LAN or loopback address.")
        networks = tuple(ipaddress.ip_network(n) for n in self.allowed_networks)
        if not networks or any(not any(n.subnet_of(lan) for lan in LAN_NETWORKS) for n in networks):
            raise ValueError("Allowed networks must be limited to LAN/loopback.")
        if self.public_api_host:
            if not ipaddress.IPv4Address(self.public_api_host).is_global:
                raise ValueError("public_api_host must be a specific public IPv4 address.")
            public_networks = tuple(ipaddress.ip_network(n) for n in self.public_api_networks)
            if not public_networks or any(n.version != 4 for n in public_networks):
                raise ValueError("The public API requires valid IPv4 source networks.")
            if not self.tls_cert or not self.tls_key:
                raise ValueError("Public APIs including registration require TLS.")
        for proxy in self.trusted_proxies:
            ipaddress.ip_address(proxy)
        ports = [getattr(self, name + "_port") for name in PORTS]
        if any(type(p) is not int or not 1 <= p <= 65535 for p in ports) or len(set(ports)) != 5:
            raise ValueError("Five distinct valid ports are required.")
        for name in ("registration_secret", "publisher_token", "benchmark_token", "admin_token"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.isascii() or not 24 <= len(value) <= 4096 or any(c.isspace() for c in value):
                raise ValueError(name + ": a local secret with at least 24 ASCII characters is required.")
        if len({self.publisher_token, self.benchmark_token, self.admin_token, self.registration_secret}) != 4:
            raise ValueError("Registration, publisher, benchmark and admin secrets must be distinct.")
        if not self.data_dir.is_absolute():
            raise ValueError("data_dir must be absolute.")
        for name in ("workspace_dir", "project_root"):
            value = getattr(self, name)
            if value and (not isinstance(value, str) or not Path(value).is_absolute()):
                raise ValueError(name + " must be an absolute path.")
        if bool(self.tls_cert) != bool(self.tls_key):
            raise ValueError("Configure the TLS certificate and key together.")
        if not address.is_loopback and not self.tls_cert:
            raise ValueError("LAN data services require a TLS certificate and key.")
        for name, low, high in (("upload_limit", 1024, 64*1024**3), ("benchmark_limit", 1024, 256*1024**2),
                                ("request_timeout", 1, 300), ("upload_timeout", 1, 3600),
                                ("attempt_limit", 2, 100), ("block_seconds", 1, 86400*30), ("validation_workers", 1, 8)):
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ValueError("Invalid value: " + name)
        if self.public_updates_url:
            url = urlsplit(self.public_updates_url)
            if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password or url.query or url.fragment or url.path not in ("", "/"):
                raise ValueError("public_updates_url must be a fixed HTTP(S) base address.")
            if self.tls_cert and url.scheme != "https":
                raise ValueError("TLS delivery requires an HTTPS download address.")

    @property
    def updates_url(self):
        return self.public_updates_url.rstrip("/") or f"{'https' if self.tls_cert else 'http'}://{self.public_api_host or self.host}:{self.updates_port}"

    def public(self):
        return {"host": self.host, "ports": {name: getattr(self, name + "_port") for name in PORTS},
                "tls": bool(self.tls_cert), "updates_url": self.updates_url,
                "allowed_networks": list(self.allowed_networks),
                "public_api_host": self.public_api_host,
                "public_api_networks": list(self.public_api_networks) if self.public_api_host else [],
                "public_registration_tls": bool(self.public_api_host)}
