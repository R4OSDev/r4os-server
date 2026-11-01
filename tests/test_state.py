from pathlib import Path
import tempfile
import unittest

from r4os_server.registration.state import AccessDecision, StateStore


class StateStoreTest(unittest.TestCase):
    def test_magic_registration_and_expiring_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3", 60)
            self.assertTrue(store.register_magic("192.0.2.10", now=10.0))
            self.assertEqual("192.0.2.10", store.list_allowed()[0]["ip"])
            store.block_ip("192.0.2.10", "invalid-magic-number", 10, now=20.0)
            self.assertTrue(store.is_blocked("192.0.2.10", now=29.0))
            self.assertFalse(store.is_blocked("192.0.2.10", now=31.0))

    def test_third_gateway_attempt_blocks_and_control_unblocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3", 60)
            first = store.record_gateway_attempt("198.51.100.7", 3, 86400, now=100.0)
            second = store.record_gateway_attempt("198.51.100.7", 3, 86400, now=10000.0)
            third = store.record_gateway_attempt("198.51.100.7", 3, 86400, now=20000.0)
            self.assertEqual(AccessDecision.STOP_NOW, first)
            self.assertEqual(AccessDecision.LAST_CHANCE, second)
            self.assertEqual(AccessDecision.BLOCKED, third)
            self.assertEqual("198.51.100.7", store.list_blocked(now=20001.0)[0]["ip"])
            self.assertTrue(store.unblock("198.51.100.7", "Test", now=20002.0))
            self.assertEqual([], store.list_blocked(now=20003.0))
            self.assertEqual("unblocked", store.list_audit()[0]["event"])

    def test_magic_registration_resets_gateway_retries(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = StateStore(Path(directory) / "state.sqlite3", 60)
            self.assertEqual(
                AccessDecision.STOP_NOW,
                store.record_gateway_attempt("203.0.113.8", 3, 86400, now=100.0),
            )
            self.assertEqual(
                AccessDecision.LAST_CHANCE,
                store.record_gateway_attempt("203.0.113.8", 3, 86400, now=101.0),
            )
            self.assertTrue(store.register_magic("203.0.113.8", now=102.0))
            with store._connect() as connection:
                connection.execute("DELETE FROM allowlist WHERE ip = ?", ("203.0.113.8",))
            self.assertEqual(
                AccessDecision.STOP_NOW,
                store.record_gateway_attempt("203.0.113.8", 3, 86400, now=103.0),
            )

    def test_gateway_rate_setting_is_validated_and_persistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "state.sqlite3"
            store = StateStore(database, 60)
            self.assertEqual(60, store.gateway_requests_per_second_per_ip())
            store.set_gateway_requests_per_second_per_ip(25, now=100.0)
            self.assertEqual(25, store.gateway_requests_per_second_per_ip())
            self.assertEqual(25, StateStore(database, 60).gateway_requests_per_second_per_ip())
            self.assertEqual("setting_changed", store.list_audit()[0]["event"])
            with self.assertRaises(ValueError):
                store.set_gateway_requests_per_second_per_ip(0)
            with self.assertRaises(ValueError):
                store.set_gateway_requests_per_second_per_ip(10001)


if __name__ == "__main__":
    unittest.main()
