"""``app.data.pipeline`` 的行为验证。

这一层是从桌面版的 ``_FetchThread`` 里抽出来的，桌面端与 Web 服务端共用它，
所以下面几条分支必须钉死——它们都曾被踩过，且出错时表现很隐蔽
（界面不报错，只是"航班突然变少了"或"数据延迟"一直不消失）。

用 stdlib ``unittest``，直接运行::

    python tests/test_pipeline.py
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.data import pipeline as P  # noqa: E402
from app.data.base import SourceRateLimited, SourceUnavailable  # noqa: E402
from app.data.models import Flight  # noqa: E402

CENTER_LAT, CENTER_LON = 22.30314, 114.178832
RADIUS_KM = 50.0


def flight(icao: str, lat: float, lon: float, **kwargs) -> Flight:
    """构造一架仅带必要遥测的航班。"""
    return Flight(icao24=icao, callsign=icao.upper(), lat=lat, lon=lon, **kwargs)


class FakeSource:
    """可编程的假数据源：按预设返回航班、空列表，或抛指定异常。"""

    def __init__(self, name: str, *, flights=None, error=None) -> None:
        self.name = name
        self.label = f"假源 {name}"
        self._flights = flights
        self._error = error
        self.calls = 0
        self.closed = False

    def fetch(self, lat, lon, radius_km, timeout=15.0):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return list(self._flights or [])

    def close(self) -> None:
        self.closed = True


class FakeEnricher:
    """不做任何网络请求的富化器替身。"""

    def __init__(self) -> None:
        self.seen = 0

    def enrich(self, flights) -> None:  # noqa: D102
        self.seen += len(flights)

    def close(self) -> None:  # noqa: D102
        pass


class PipelineTestCase(unittest.TestCase):
    """共同的装配：替换 create_source / FALLBACK_CHAIN，并关掉最小请求间隔。"""

    def setUp(self) -> None:
        self._real_create = P.create_source
        self._real_chain = P.FALLBACK_CHAIN
        self._real_interval = P.MIN_REQUEST_INTERVAL

        self.sources: dict[str, FakeSource] = {}
        self.created: list[str] = []

        def fake_create(name, api_key=""):
            self.created.append(name)
            if name not in self.sources:
                self.sources[name] = FakeSource(name)
            return self.sources[name]

        P.create_source = fake_create
        P.FALLBACK_CHAIN = ("second", "third")
        # 关掉节流，否则连续两次 fetch 会被 SKIPPED 挡掉；节流本身单列一个用例测
        P.MIN_REQUEST_INTERVAL = 0.0

        self.enricher = FakeEnricher()
        self.guard = P.UpstreamGuard()
        self.pipeline = P.FlightPipeline(guard=self.guard, enricher=self.enricher)

    def tearDown(self) -> None:
        P.create_source = self._real_create
        P.FALLBACK_CHAIN = self._real_chain
        P.MIN_REQUEST_INTERVAL = self._real_interval

    # ------------------------------------------------------------------

    def source(self, name: str, **kwargs) -> FakeSource:
        src = FakeSource(name, **kwargs)
        self.sources[name] = src
        return src

    def params(self, **overrides) -> P.FetchParams:
        base = dict(
            lat=CENTER_LAT,
            lon=CENTER_LON,
            radius_km=RADIUS_KM,
            source="primary",
            stale_limit_sec=60.0,
            show_ground=False,
        )
        base.update(overrides)
        return P.FetchParams(**base)

    # ------------------------------------------------------------------
    # 降级链
    # ------------------------------------------------------------------

    def test_primary_success_has_no_notice(self):
        self.source("primary", flights=[flight("aaa", CENTER_LAT, CENTER_LON)])
        payload = self.pipeline.fetch(self.params())

        self.assertEqual(payload["source"], "primary")
        self.assertEqual(payload["notice"], "")
        self.assertFalse(payload["stale"])
        self.assertEqual(len(payload["flights"]), 1)

    def test_empty_primary_falls_back_and_says_no_data(self):
        """主源"能连上但这片儿没数据"与"主源挂了"必须给不同的提示。"""
        self.source("primary", flights=[])
        self.source("second", flights=[flight("bbb", CENTER_LAT, CENTER_LON)])

        payload = self.pipeline.fetch(self.params())

        self.assertEqual(payload["source"], "second")
        self.assertIn("在该区域无数据", payload["notice"])
        self.assertIn("primary", payload["notice"])

    def test_erroring_primary_falls_back_and_says_unavailable(self):
        self.source("primary", error=SourceUnavailable("连接超时"))
        self.source("second", flights=[flight("bbb", CENTER_LAT, CENTER_LON)])

        payload = self.pipeline.fetch(self.params())

        self.assertEqual(payload["source"], "second")
        self.assertIn("不可用", payload["notice"])

    def test_empty_everywhere_is_not_an_error(self):
        """所有源都答复了、只是都没数据——这不是故障，不该给提示。"""
        self.source("primary", flights=[])
        self.source("second", flights=[])
        self.source("third", flights=[])

        payload = self.pipeline.fetch(self.params())

        self.assertEqual(payload["flights"], [])
        self.assertEqual(payload["notice"], "")
        self.assertEqual(payload["source"], "primary")

    def test_fallback_source_is_closed(self):
        """降级时临时构造的源必须关闭，否则每轮都漏一个连接。"""
        self.source("primary", flights=[])
        second = self.source("second", flights=[flight("bbb", CENTER_LAT, CENTER_LON)])

        self.pipeline.fetch(self.params())

        self.assertTrue(second.closed)

    def test_primary_source_instance_is_reused(self):
        """主源复用同一实例以保留连接池；降级源才是用完即弃。"""
        self.source("primary", flights=[flight("aaa", CENTER_LAT, CENTER_LON)])

        self.pipeline.fetch(self.params())
        self.pipeline.fetch(self.params())

        self.assertEqual(self.created.count("primary"), 1)

    # ------------------------------------------------------------------
    # 限流冷却
    # ------------------------------------------------------------------

    def test_primary_rate_limit_keeps_last_frame(self):
        """主源 429 时保留上一帧，而不是清空雷达盘。

        实测 adsb.lol 会返回 429；此时若降级到数据稀疏得多的源、
        或干脆清空，用户会误以为"附近没有航班了"。
        """
        self.source("primary", flights=[flight("aaa", CENTER_LAT, CENTER_LON)])
        self.pipeline.fetch(self.params())

        self.sources["primary"]._error = SourceRateLimited("429")
        self.assertIsNone(self.pipeline.fetch(self.params()))

        self.assertGreater(self.guard.cooldown_remaining("primary"), 0.0)

        payload = self.pipeline.stale_payload()
        self.assertIsNotNone(payload)
        self.assertTrue(payload["stale"])
        self.assertEqual(len(payload["flights"]), 1)

    def test_cooldown_skips_source_entirely(self):
        """冷却期内的源应被跳过，不去撞第二次 429。"""
        primary = self.source("primary", error=SourceRateLimited("429"))
        self.source("second", flights=[flight("bbb", CENTER_LAT, CENTER_LON)])

        self.pipeline.fetch(self.params())
        first_calls = primary.calls

        self.pipeline.fetch(self.params())

        self.assertEqual(primary.calls, first_calls)

    def test_all_sources_cooling_reports_clearly(self):
        """所有源都在冷却时，提示要说清是"限流冷却"而不是"获取失败"。"""
        self.source("primary", flights=[flight("aaa", CENTER_LAT, CENTER_LON)])
        self.pipeline.fetch(self.params())

        for name in ("primary", "second", "third"):
            self.guard.note_rate_limited(name)

        self.assertIsNone(self.pipeline.fetch(self.params()))

        payload = self.pipeline.stale_payload()
        self.assertIsNotNone(payload)
        self.assertIn("冷却", payload["notice"])
        self.assertTrue(payload["stale"])

    def test_cooldowns_are_shared_across_queries(self):
        """上游配额按服务器计：一个查询撞到 429，其他查询也必须看到这份冷却。

        若各查询各存一份冷却，10 个客户端看 10 个地点就会各自去撞同一个配额。
        """
        self.source("primary", error=SourceRateLimited("429"))
        self.source("second", flights=[flight("bbb", CENTER_LAT, CENTER_LON)])
        self.pipeline.fetch(self.params())
        calls_after_first = self.sources["primary"].calls

        other = P.FlightPipeline(guard=self.guard, enricher=FakeEnricher())
        payload = other.fetch(self.params())

        self.assertEqual(self.sources["primary"].calls, calls_after_first)
        self.assertEqual(payload["source"], "second")

    # ------------------------------------------------------------------
    # 最小请求间隔
    # ------------------------------------------------------------------

    def test_throttle_returns_skipped_not_stale(self):
        """被节流挡下必须是 SKIPPED——若返回 None，界面会冒出莫名的"数据延迟"。"""
        P.MIN_REQUEST_INTERVAL = 60.0
        self.source("primary", flights=[flight("aaa", CENTER_LAT, CENTER_LON)])

        self.assertIsNotNone(self.pipeline.fetch(self.params()))
        self.assertIs(self.pipeline.fetch(self.params()), P.SKIPPED)

    # ------------------------------------------------------------------
    # 过滤
    # ------------------------------------------------------------------

    def test_filter_drops_stale_ground_outside_and_sorts(self):
        near = flight("near", 22.35, 114.18)
        inside = flight("inside", 22.5, 114.18)
        outside = flight("outside", 22.9, 114.18)          # ~66 km，超出 50 km
        stale = flight("stale", 22.35, 114.18, seen_pos=999.0)
        grounded = flight("ground", 22.35, 114.18, on_ground=True)

        kept = P.filter_and_measure(
            [inside, near, outside, stale, grounded],
            CENTER_LAT,
            CENTER_LON,
            RADIUS_KM,
            stale_limit_sec=60.0,
            show_ground=False,
        )

        self.assertEqual([f.icao24 for f in kept], ["near", "inside"])
        # 距离必须是本地算的：adsb.lol 的 dst 单位是海里，OpenSky 干脆没有
        self.assertLess(kept[0].distance_km, kept[1].distance_km)
        self.assertGreater(kept[0].bearing, 0.0)

    def test_show_ground_keeps_grounded_flights(self):
        grounded = flight("ground", 22.35, 114.18, on_ground=True)
        kept = P.filter_and_measure(
            [grounded],
            CENTER_LAT,
            CENTER_LON,
            RADIUS_KM,
            stale_limit_sec=60.0,
            show_ground=True,
        )
        self.assertEqual([f.icao24 for f in kept], ["ground"])

    # ------------------------------------------------------------------
    # 帧平滑
    # ------------------------------------------------------------------

    def test_prev_positions_are_carried_into_next_frame(self):
        self.source("primary", flights=[flight("aaa", 22.35, 114.18)])
        self.pipeline.fetch(self.params())

        self.sources["primary"]._flights = [flight("aaa", 22.40, 114.18)]
        payload = self.pipeline.fetch(self.params())

        moved = payload["flights"][0]
        self.assertAlmostEqual(moved.prev_lat, 22.35, places=6)
        self.assertAlmostEqual(moved.lat, 22.40, places=6)

    def test_first_sighting_has_no_prev_position(self):
        """首次出现的航班不应做插值，否则会从原点飞过来。"""
        self.source("primary", flights=[flight("aaa", 22.35, 114.18)])
        payload = self.pipeline.fetch(self.params())

        self.assertIsNone(payload["flights"][0].prev_lat)
        self.assertIsNone(payload["flights"][0].prev_lon)

    def test_clear_positions_stops_interpolation(self):
        self.source("primary", flights=[flight("aaa", 22.35, 114.18)])
        self.pipeline.fetch(self.params())
        self.pipeline.clear_positions()

        self.sources["primary"]._flights = [flight("aaa", 22.40, 114.18)]
        payload = self.pipeline.fetch(self.params())

        self.assertIsNone(payload["flights"][0].prev_lat)

    # ------------------------------------------------------------------
    # 陈旧帧语义
    # ------------------------------------------------------------------

    def test_last_frame_empty_is_not_marked_stale(self):
        """上一帧本来就空（该区域确实没航班）时不能标陈旧。

        标了的话界面会一直显示"数据延迟"，让人以为还在加载。
        注意：只要还有任何一个源能答上话，得到的都是合法帧（哪怕航班数为 0），
        只有**所有源都答不上来**时才会走到沿帧分支。
        """
        self.source("primary", flights=[])
        self.source("second", flights=[])
        self.source("third", flights=[])
        self.pipeline.fetch(self.params())

        for src in self.sources.values():
            src._error = SourceUnavailable("断了")
        self.assertIsNone(self.pipeline.fetch(self.params()))

        payload = self.pipeline.stale_payload()
        self.assertIsNotNone(payload)
        self.assertFalse(payload["stale"])

    def test_partial_outage_still_yields_a_fresh_empty_frame(self):
        """主源挂了但备源答"没数据"，应得到新的空帧而不是陈旧帧。"""
        self.source("primary", flights=[])
        self.source("second", flights=[])
        self.source("third", flights=[])
        self.pipeline.fetch(self.params())

        self.sources["primary"]._error = SourceUnavailable("断了")
        payload = self.pipeline.fetch(self.params())

        self.assertIsNotNone(payload)
        self.assertEqual(payload["flights"], [])
        self.assertFalse(payload["stale"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
