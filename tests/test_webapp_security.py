"""Web 服务端安全原语的验证。

公网可访问意味着这里的每一条都直接对应一个可利用的缺口，
所以不测"能不能跑通"，而是测**边界与失败路径**：
错口令、被篡改的哈希、会话过期、锁定阈值、滑窗边界、密钥脱敏。

用 stdlib ``unittest``::

    python tests/test_webapp_security.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import Config  # noqa: E402
from webapp.auth import (  # noqa: E402
    LoginThrottle,
    SessionStore,
    hash_password,
    verify_password,
)
from webapp.limits import SlidingWindowLimiter  # noqa: E402
from webapp.serialize import config_to_dict, flight_phase  # noqa: E402


class PasswordTestCase(unittest.TestCase):
    def test_roundtrip(self):
        stored = hash_password("correct horse battery staple")
        self.assertTrue(verify_password("correct horse battery staple", stored))
        self.assertFalse(verify_password("wrong", stored))

    def test_salt_is_per_call(self):
        """同一口令两次哈希必须不同——否则彩虹表可以复用。"""
        self.assertNotEqual(hash_password("same"), hash_password("same"))

    def test_hash_never_contains_plaintext(self):
        stored = hash_password("hunter2")
        self.assertNotIn("hunter2", stored)
        self.assertTrue(stored.startswith("scrypt$"))

    def test_malformed_hash_is_rejected_not_raised(self):
        """配置被改坏时应当是"登录失败"，而不是把服务打成 500。"""
        for broken in ("", "not-a-hash", "scrypt$bad", "md5$1$2$3$4$5", "scrypt$1$1$1$zz$zz"):
            with self.subTest(broken=broken):
                self.assertFalse(verify_password("anything", broken))

    def test_empty_password_never_matches(self):
        self.assertFalse(verify_password("", hash_password("x")))
        self.assertFalse(verify_password("x", ""))


class SessionStoreTestCase(unittest.TestCase):
    def test_create_and_validate(self):
        store = SessionStore(ttl=60)
        token = store.create()
        self.assertTrue(store.valid(token))

    def test_unknown_and_missing_tokens_rejected(self):
        store = SessionStore(ttl=60)
        self.assertFalse(store.valid("forged"))
        self.assertFalse(store.valid(None))
        self.assertFalse(store.valid(""))

    def test_expiry(self):
        store = SessionStore(ttl=0)
        token = store.create()
        # ttl=0 的会话应立即失效
        self.assertFalse(store.valid(token))

    def test_revoke(self):
        store = SessionStore(ttl=60)
        token = store.create()
        store.revoke(token)
        self.assertFalse(store.valid(token))

    def test_tokens_are_unpredictable_and_unique(self):
        store = SessionStore(ttl=60)
        tokens = {store.create() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        self.assertTrue(all(len(t) >= 32 for t in tokens))

    def test_purge_expired(self):
        store = SessionStore(ttl=0)
        store.create()
        store.purge_expired()
        self.assertEqual(len(store), 0)


class LoginThrottleTestCase(unittest.TestCase):
    def test_below_threshold_is_not_locked(self):
        throttle = LoginThrottle(max_failures=5)
        for _ in range(4):
            throttle.note_failure("1.2.3.4")
        self.assertEqual(throttle.locked_for("1.2.3.4"), 0.0)

    def test_threshold_locks(self):
        throttle = LoginThrottle(max_failures=3)
        for _ in range(3):
            throttle.note_failure("1.2.3.4")
        self.assertGreater(throttle.locked_for("1.2.3.4"), 0.0)

    def test_lockout_grows(self):
        """每多失败一次锁得更久，让在线爆破的代价指数上升。"""
        throttle = LoginThrottle(max_failures=2, base_lockout=10.0)
        throttle.note_failure("ip")
        throttle.note_failure("ip")
        first = throttle.locked_for("ip")
        throttle.note_failure("ip")
        second = throttle.locked_for("ip")
        self.assertGreater(second, first)

    def test_lockout_is_capped(self):
        throttle = LoginThrottle(max_failures=1, base_lockout=10.0, cap_lockout=25.0)
        for _ in range(30):
            throttle.note_failure("ip")
        self.assertLessEqual(throttle.locked_for("ip"), 25.0)

    def test_success_clears_failures(self):
        """本人打错几次后不该被自己锁在门外。"""
        throttle = LoginThrottle(max_failures=2, base_lockout=999.0)
        throttle.note_failure("ip")
        throttle.note_failure("ip")
        self.assertGreater(throttle.locked_for("ip"), 0.0)
        throttle.note_success("ip")
        self.assertEqual(throttle.locked_for("ip"), 0.0)

    def test_per_ip_isolation(self):
        throttle = LoginThrottle(max_failures=2)
        throttle.note_failure("a")
        throttle.note_failure("a")
        self.assertGreater(throttle.locked_for("a"), 0.0)
        self.assertEqual(throttle.locked_for("b"), 0.0)


class SlidingWindowLimiterTestCase(unittest.TestCase):
    def test_allows_up_to_max_then_denies(self):
        limiter = SlidingWindowLimiter(max_events=3, window=60.0)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))

    def test_keys_are_independent(self):
        limiter = SlidingWindowLimiter(max_events=1, window=60.0)
        self.assertTrue(limiter.allow("a"))
        self.assertTrue(limiter.allow("b"))
        self.assertFalse(limiter.allow("a"))

    def test_window_slides(self):
        """滑窗必须真的滑动：过了窗口就该重新有额度。"""
        limiter = SlidingWindowLimiter(max_events=1, window=0.05)
        self.assertTrue(limiter.allow("a"))
        self.assertFalse(limiter.allow("a"))
        time.sleep(0.08)
        self.assertTrue(limiter.allow("a"))

    def test_retry_after_positive_when_blocked(self):
        limiter = SlidingWindowLimiter(max_events=1, window=30.0)
        limiter.allow("a")
        self.assertGreater(limiter.retry_after("a"), 0.0)
        self.assertEqual(limiter.retry_after("never-seen"), 0.0)

    def test_prune_drops_stale_buckets(self):
        """按 IP 建桶的字典不能被扫描流量无限撑大。"""
        limiter = SlidingWindowLimiter(max_events=5, window=0.05)
        limiter.allow("a")
        limiter._last_prune = 0.0  # 强制下一次 allow 触发清理
        time.sleep(0.08)
        limiter.allow("b")
        self.assertNotIn("a", limiter._events)


class RedactionTestCase(unittest.TestCase):
    """密钥绝不能出现在回给前端的配置里。"""

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = Path(self.tmpdir.name) / "config.json"
        self.config = Config(self.path)
        self.config.update(fr24_api_key="SUPER-SECRET-KEY")

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_api_key_is_stripped(self):
        payload = config_to_dict(self.config)
        self.assertNotIn("fr24_api_key", payload)
        self.assertNotIn("SUPER-SECRET-KEY", json.dumps(payload))

    def test_presence_flag_is_reported(self):
        payload = config_to_dict(self.config)
        self.assertTrue(payload["has_fr24_api_key"])

    def test_presence_flag_false_when_unset(self):
        empty = Config(Path(self.tmpdir.name) / "other.json")
        payload = config_to_dict(empty)
        self.assertFalse(payload["has_fr24_api_key"])

    def test_other_keys_survive(self):
        payload = config_to_dict(self.config)
        for key in ("source", "radius_km", "refresh_sec", "show_ground", "location"):
            self.assertIn(key, payload)


class FlightPhaseTestCase(unittest.TestCase):
    def test_phases(self):
        from app.data.models import Flight

        ground = Flight(icao24="a", on_ground=True)
        self.assertEqual(flight_phase(ground), "地面")

        climbing = Flight(icao24="b", vertical_rate_fpm=1200.0, ground_speed_kt=400.0)
        self.assertEqual(flight_phase(climbing), "爬升中")

        descending = Flight(icao24="c", vertical_rate_fpm=-1200.0, ground_speed_kt=400.0)
        self.assertEqual(flight_phase(descending), "下降中")

        level = Flight(icao24="d", vertical_rate_fpm=0.0, ground_speed_kt=430.0)
        self.assertEqual(flight_phase(level), "巡航")

        slow = Flight(icao24="e", vertical_rate_fpm=0.0, ground_speed_kt=40.0)
        self.assertEqual(flight_phase(slow), "低速")

    def test_ground_wins_over_vertical_rate(self):
        """停机坪上的航班可能因气压波动报出垂直速度，不能据此判成爬升。"""
        from app.data.models import Flight

        grounded = Flight(
            icao24="f", on_ground=True, vertical_rate_fpm=900.0, ground_speed_kt=5.0
        )
        self.assertEqual(flight_phase(grounded), "地面")


if __name__ == "__main__":
    unittest.main(verbosity=2)
