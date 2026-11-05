from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from contextlib import contextmanager
import ipaddress
import hashlib
import secrets
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any, Iterator


class AccessDecision(str, Enum):
    ALLOWED = "allowed"
    STOP_NOW = "stop_now"
    LAST_CHANCE = "last_chance"
    BLOCKED = "blocked"


GATEWAY_RATE_SETTING = "gateway_requests_per_second_per_ip"
GATEWAY_RATE_MINIMUM = 1
GATEWAY_RATE_MAXIMUM = 10000


def validate_gateway_rate(value: int) -> int:
    if value < GATEWAY_RATE_MINIMUM or value > GATEWAY_RATE_MAXIMUM:
        raise ValueError(
            f"Service requests per second must be between {GATEWAY_RATE_MINIMUM} and {GATEWAY_RATE_MAXIMUM} liegen"
        )
    return value


def normalize_ip(value: str) -> str:
    address = ipaddress.ip_address(value.strip())
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return str(address.ipv4_mapped)
    return address.compressed


def utc_text(timestamp: float | None) -> str | None:
    if timestamp is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(timestamp))


class StateStore:
    def __init__(self, database: str | Path, initial_gateway_rate: int):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._settings_lock = threading.Lock()
        self._gateway_rate = validate_gateway_rate(initial_gateway_rate)
        self._initialize(initial_gateway_rate)


    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()


    def _initialize(self, initial_gateway_rate: int) -> None:
        with self._connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] not in (0, 1):
                raise ValueError("Unsupported registration database version")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS allowlist (
                    ip TEXT PRIMARY KEY,
                    authenticated_at REAL NOT NULL,
                    last_seen_at REAL NOT NULL,
                    source TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blocklist (
                    ip TEXT PRIMARY KEY,
                    reason TEXT NOT NULL,
                    blocked_at REAL NOT NULL,
                    expires_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS unauthenticated_attempts (
                    ip TEXT NOT NULL,
                    service TEXT NOT NULL,
                    first_at REAL NOT NULL,
                    last_at REAL NOT NULL,
                    attempts INTEGER NOT NULL,
                    PRIMARY KEY (ip, service)
                );
                CREATE TABLE IF NOT EXISTS audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event TEXT NOT NULL,
                    service TEXT NOT NULL DEFAULT 'registration',
                    ip TEXT,
                    note TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS client_credentials (
                    digest TEXT PRIMARY KEY,
                    issued_ip TEXT NOT NULL,
                    issued_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS client_credentials_issuance
                    ON client_credentials(issued_ip, issued_at);
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    integer_value INTEGER NOT NULL
                );
                """
            )
            connection.execute("PRAGMA user_version=1")
            connection.execute(
                "INSERT OR IGNORE INTO settings(key, integer_value) VALUES (?, ?)",
                (GATEWAY_RATE_SETTING, initial_gateway_rate),
            )
            row = connection.execute(
                "SELECT integer_value FROM settings WHERE key = ?",
                (GATEWAY_RATE_SETTING,),
            ).fetchone()
        if row is None:
            raise RuntimeError("Gateway rate limit could not be initialised")
        self._gateway_rate = validate_gateway_rate(int(row["integer_value"]))


    @staticmethod
    def _audit(connection: sqlite3.Connection, event: str, ip: str | None, note: str, now: float, service: str = "registration") -> None:
        connection.execute(
            "INSERT INTO audit(event, ip, note, created_at, service) VALUES (?, ?, ?, ?, ?)",
            (event, ip, note, now, service),
        )


    @staticmethod
    def _expire_block(connection: sqlite3.Connection, ip: str, now: float) -> bool:
        row = connection.execute(
            "SELECT expires_at FROM blocklist WHERE ip = ?",
            (ip,),
        ).fetchone()
        if row is None:
            return False
        expires_at = row["expires_at"]
        if expires_at is None or float(expires_at) > now:
            return True
        connection.execute("DELETE FROM blocklist WHERE ip = ?", (ip,))
        StateStore._audit(connection, "block_expired", ip, "Blocklist entry expired", now)
        return False


    def is_blocked(self, ip: str, now: float | None = None) -> bool:
        normalized = normalize_ip(ip)
        current = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            blocked = self._expire_block(connection, normalized, current)
            connection.commit()
            return blocked


    def register_magic(self, ip: str, source: str = "magic", now: float | None = None) -> bool:
        normalized = normalize_ip(ip)
        current = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._expire_block(connection, normalized, current):
                connection.commit()
                return False
            connection.execute(
                """
                INSERT INTO allowlist(ip, authenticated_at, last_seen_at, source)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(ip) DO UPDATE SET
                    authenticated_at = excluded.authenticated_at,
                    last_seen_at = excluded.last_seen_at,
                    source = excluded.source
                """,
                (normalized, current, current, source),
            )
            connection.execute("DELETE FROM unauthenticated_attempts WHERE ip = ?", (normalized,))
            self._audit(connection, "authenticated", normalized, source, current)
            connection.commit()
            return True


    def issue_client(self, ip: str, now: float | None = None) -> str | None:
        """Issue a registration/read credential; persist only its hash.

        None means the shared ban or the durable 64/hour/source quota denied
        issuance. Previously issued installations remain valid across IP changes.
        """
        normalized = normalize_ip(ip)
        current = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._expire_block(connection, normalized, current):
                return None
            count = connection.execute(
                "SELECT COUNT(*) FROM client_credentials WHERE issued_ip=? AND issued_at>?",
                (normalized, current - 3600),
            ).fetchone()[0]
            if count >= 64:
                return None
            token = "R4C1_" + secrets.token_hex(32)
            connection.execute("INSERT INTO client_credentials VALUES (?, ?, ?)",
                               (hashlib.sha256(token.encode("ascii")).hexdigest(), normalized, current))
            self._audit(connection, "client_issued", normalized, "registration-read-v1", current)
            return token

    def register_client(self, ip: str, token: bytes) -> bool:
        if len(token) != 69 or not token.startswith(b"R4C1_") or any(c not in b"0123456789abcdef" for c in token[5:]):
            return False
        digest = hashlib.sha256(token).hexdigest()
        with self._connect() as connection:
            known = connection.execute("SELECT 1 FROM client_credentials WHERE digest=?", (digest,)).fetchone()
        return known is not None and self.register_magic(ip, "registration-read-v1")


    def block_ip(self, ip: str, reason: str, duration_seconds: int, now: float | None = None) -> None:
        normalized = normalize_ip(ip)
        current = time.time() if now is None else now
        expires_at = current + duration_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM allowlist WHERE ip = ?", (normalized,))
            connection.execute("DELETE FROM unauthenticated_attempts WHERE ip = ?", (normalized,))
            connection.execute(
                """
                INSERT INTO blocklist(ip, reason, blocked_at, expires_at, attempts)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(ip) DO UPDATE SET
                    reason = excluded.reason,
                    blocked_at = excluded.blocked_at,
                    expires_at = excluded.expires_at,
                    attempts = blocklist.attempts + 1
                """,
                (normalized, reason, current, expires_at),
            )
            self._audit(connection, "blocked", normalized, reason, current)
            connection.commit()


    def unblock(self, ip: str, note: str, now: float | None = None) -> bool:
        normalized = normalize_ip(ip)
        current = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute("DELETE FROM blocklist WHERE ip = ?", (normalized,))
            connection.execute("DELETE FROM unauthenticated_attempts WHERE ip = ?", (normalized,))
            self._audit(connection, "unblocked", normalized, note.strip() or "ControlCenter", current)
            connection.commit()
            return cursor.rowcount > 0


    def list_allowed(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT ip, authenticated_at, last_seen_at, source FROM allowlist ORDER BY authenticated_at DESC"
            ).fetchall()
        return [
            {
                "ip": row["ip"],
                "authenticated_at": utc_text(float(row["authenticated_at"])),
                "last_seen_at": utc_text(float(row["last_seen_at"])),
                "source": row["source"],
            }
            for row in rows
        ]


    def list_blocked(self, now: float | None = None) -> list[dict[str, Any]]:
        current = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            expired = connection.execute(
                "SELECT ip FROM blocklist WHERE expires_at IS NOT NULL AND expires_at <= ?",
                (current,),
            ).fetchall()
            for row in expired:
                connection.execute("DELETE FROM blocklist WHERE ip = ?", (row["ip"],))
                self._audit(connection, "block_expired", row["ip"], "Blocklist entry expired", current)
            rows = connection.execute(
                "SELECT ip, reason, blocked_at, expires_at, attempts FROM blocklist ORDER BY blocked_at DESC"
            ).fetchall()
            connection.commit()
        return [
            {
                "ip": row["ip"],
                "reason": row["reason"],
                "blocked_at": utc_text(float(row["blocked_at"])),
                "expires_at": utc_text(float(row["expires_at"])) if row["expires_at"] is not None else None,
                "attempts": int(row["attempts"]),
            }
            for row in rows
        ]


    def list_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event, ip, note, created_at, service FROM audit ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "event": row["event"],
                "service": row["service"],
                "ip": row["ip"],
                "note": row["note"],
                "created_at": utc_text(float(row["created_at"])),
            }
            for row in rows
        ]

    def get_rate(self, service):
        if service not in ("updates", "benchmarks", "software", "registration"):
            raise ValueError("Unknown service")
        key = GATEWAY_RATE_SETTING if service == "updates" else service + ".requests_per_second_per_ip"
        with self._connect() as connection:
            row = connection.execute("SELECT integer_value FROM settings WHERE key=?", (key,)).fetchone()
        return int(row[0]) if row else 60

    def set_rate(self, service, value):
        self.get_rate(service)
        if type(value) is not int:
            raise ValueError("Rate must be an integer")
        validate_gateway_rate(value)
        key = GATEWAY_RATE_SETTING if service == "updates" else service + ".requests_per_second_per_ip"
        with self._connect() as connection:
            connection.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET integer_value=excluded.integer_value", (key, value))
            self._audit(connection, "setting_changed", None, f"requests_per_second_per_ip={value}", time.time(), service)

    def gateway_requests_per_second_per_ip(self):
        return self.get_rate("updates")

    def set_gateway_requests_per_second_per_ip(self, value, now=None):
        self.set_rate("updates", value)

    def settings(self):
        return {service: self.get_rate(service) for service in ("updates", "benchmarks", "software", "registration")}

    def audit(self, event, ip, note, service):
        with self._connect() as connection:
            self._audit(connection, event, ip, note, time.time(), service)

    def revoke(self, ip):
        normalized = normalize_ip(ip)
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM allowlist WHERE ip=?", (normalized,))
            self._audit(connection, "revoked", normalized, "Registration revoked", time.time())
        return bool(cursor.rowcount)

    def record_gateway_attempt(self, ip, limit, block_seconds, now=None, service="updates"):
        self.get_rate(service)
        normalized = normalize_ip(ip)
        current = time.time() if now is None else now
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._expire_block(connection, normalized, current):
                return AccessDecision.BLOCKED
            if connection.execute("SELECT 1 FROM allowlist WHERE ip=?", (normalized,)).fetchone():
                connection.execute("UPDATE allowlist SET last_seen_at=? WHERE ip=?", (current, normalized))
                connection.execute("DELETE FROM unauthenticated_attempts WHERE ip=? AND service=?", (normalized, service))
                return AccessDecision.ALLOWED
            row = connection.execute("SELECT first_at,attempts FROM unauthenticated_attempts WHERE ip=? AND service=?", (normalized,service)).fetchone()
            attempts = int(row["attempts"]) + 1 if row else 1
            first_at = row["first_at"] if row else current
            if attempts >= limit:
                connection.execute("DELETE FROM unauthenticated_attempts WHERE ip=?", (normalized,))
                connection.execute("DELETE FROM allowlist WHERE ip=?", (normalized,))
                connection.execute("INSERT INTO blocklist VALUES (?,?,?,?,?) ON CONFLICT(ip) DO UPDATE SET reason=excluded.reason,blocked_at=excluded.blocked_at,expires_at=excluded.expires_at,attempts=excluded.attempts", (normalized,service+"-without-registration",current,current+block_seconds,attempts))
                self._audit(connection,"blocked",normalized,"Data service accessed without registration",current,service)
                return AccessDecision.BLOCKED
            connection.execute("INSERT INTO unauthenticated_attempts VALUES (?,?,?,?,?) ON CONFLICT(ip,service) DO UPDATE SET last_at=excluded.last_at,attempts=excluded.attempts", (normalized,service,first_at,current,attempts))
            decision = AccessDecision.LAST_CHANCE if attempts == limit - 1 else AccessDecision.STOP_NOW
            self._audit(connection,"registration_required",normalized,"LAST CHANCE!" if decision == AccessDecision.LAST_CHANCE else "STOP NOW!",current,service)
            return decision


