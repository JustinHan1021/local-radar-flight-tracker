"""帧缓存与单飞。

上游 ADS-B 有配额（实测 adsb.lol 约 60 秒窗口内 3 次），公网下 N 个客户端
绝不能变成 N 倍上游请求。两层保护：

1. **帧缓存**：按 (位置, 半径) 分键，TTL 取用户设置的刷新间隔。
   同一片空域的所有客户端共享一帧。
2. **单飞**：同键的并发请求只放一次上游拉取，其余等同一个结果。
   缓存只在拉取**完成后**才写入，所以没有单飞的话，N 个同时到达的请求
   会同时看到空缓存、一起冲上游——这正是最容易触发 429 的时刻。

上游限流状态由 :class:`app.data.pipeline.UpstreamGuard` 全局共享，
跨所有查询位置生效（配额是按服务器的，不是按查询的）。
"""

from __future__ import annotations

import asyncio
import time

from app.data.enrich import Enricher
from app.data.pipeline import (
    MIN_REQUEST_INTERVAL,
    SKIPPED,
    FetchParams,
    FlightPipeline,
    UpstreamGuard,
)

from webapp.serialize import empty_frame, frame_to_dict

#: 同时保留的查询位置上限。每个位置一份 pipeline（各自记得冷却、上一帧、
#: 位置历史），但**富化器是共享的**。
MAX_PIPELINES = 24

#: 缓存键上经纬度保留的小数位。3 位约 110 m，远小于任何量程下的一像素。
KEY_PRECISION = 3


class _SharedEnricher:
    """把同一个 :class:`Enricher` 借给多个 pipeline。

    ``Enricher`` 自带线程池与磁盘缓存，每个查询位置各建一个会变成几百个线程。
    但 :meth:`FlightPipeline.close` 会关掉自己的 enricher——淘汰一个查询位置时
    若不拦住，会把所有位置共用的富化器一起关掉。所以这里的 ``close()`` 是空操作，
    由 :class:`FrameService` 在关停时统一关闭。
    """

    def __init__(self, enricher: Enricher) -> None:
        self._enricher = enricher

    def enrich(self, flights) -> None:
        self._enricher.enrich(flights)

    def close(self) -> None:
        pass


class FrameService:
    """按需拉取 + 缓存 + 单飞。线程安全的阻塞拉取放在工作线程里跑。"""

    def __init__(self, config, guard: UpstreamGuard | None = None) -> None:
        self._config = config
        self._guard = guard or UpstreamGuard()
        self._enricher = Enricher()
        self._pipelines: dict[tuple[int, int, float], FlightPipeline] = {}
        self._cache: dict[tuple[int, int, float], tuple[float, dict]] = {}
        self._inflight: dict[tuple[int, int, float], asyncio.Future] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    @property
    def guard(self) -> UpstreamGuard:
        return self._guard

    def cooling(self) -> dict[str, int]:
        return self._guard.cooling_snapshot()

    async def frame(self, lat: float, lon: float, radius_km: float) -> dict:
        """取一帧 JSON。命中缓存直接返回，否则触发（或等待）一次拉取。"""
        key = self._key(lat, lon, radius_km)
        unit = str(self._config.get("altitude_unit", "m"))

        cached = self._cache.get(key)
        if cached is not None and cached[0] > time.monotonic():
            return frame_to_dict(cached[1], unit, self.cooling())

        owner = False
        async with self._lock:
            # 二次检查：等锁期间可能已被别的请求填好
            cached = self._cache.get(key)
            if cached is not None and cached[0] > time.monotonic():
                return frame_to_dict(cached[1], unit, self.cooling())

            future = self._inflight.get(key)
            if future is None:
                future = asyncio.get_running_loop().create_future()
                self._inflight[key] = future
                owner = True

        if owner:
            try:
                payload = await asyncio.to_thread(
                    self._fetch, key, lat, lon, radius_km
                )
            except BaseException as exc:  # noqa: BLE001 - 必须让等待方也收到
                async with self._lock:
                    self._inflight.pop(key, None)
                if not future.done():
                    future.set_exception(exc)
                raise
            async with self._lock:
                self._inflight.pop(key, None)
            if not future.done():
                future.set_result(payload)
            return frame_to_dict(payload, unit, self.cooling())

        payload = await future
        return frame_to_dict(payload, unit, self.cooling())

    def close(self) -> None:
        for pipeline in self._pipelines.values():
            pipeline.close()
        self._pipelines.clear()
        self._enricher.close()

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _key(lat: float, lon: float, radius_km: float) -> tuple[int, int, float]:
        scale = 10 ** KEY_PRECISION
        return (round(lat * scale), round(lon * scale), float(radius_km))

    def _pipeline_for(self, key) -> FlightPipeline:
        pipeline = self._pipelines.get(key)
        if pipeline is not None:
            return pipeline

        if len(self._pipelines) >= MAX_PIPELINES:
            # 淘汰最早的一个。位置历史与上一帧随之丢掉，可以接受——
            # 它意味着这个位置很久没人看了。
            oldest = next(iter(self._pipelines))
            self._pipelines.pop(oldest).close()
            self._cache.pop(oldest, None)

        pipeline = FlightPipeline(
            guard=self._guard, enricher=_SharedEnricher(self._enricher)
        )
        self._pipelines[key] = pipeline
        return pipeline

    def _fetch(self, key, lat: float, lon: float, radius_km: float) -> dict:
        """在工作线程里跑一轮拉取。返回 payload（永不为 None）。"""
        pipeline = self._pipeline_for(key)
        params = FetchParams(
            lat=lat,
            lon=lon,
            radius_km=radius_km,
            source=self._config.source,
            fr24_api_key=self._config.fr24_api_key,
            stale_limit_sec=float(self._config.stale_limit_sec),
            show_ground=bool(self._config.show_ground),
        )

        payload = pipeline.fetch(params)

        if payload is SKIPPED:
            # 撞上全局最小请求间隔（另一个查询刚拉过）。稍等重试一次，
            # 否则首个请求会拿到空帧、界面闪一下。
            time.sleep(MIN_REQUEST_INTERVAL)
            payload = pipeline.fetch(params)
            if payload is SKIPPED:
                payload = pipeline.stale_payload() or pipeline.last_payload

        if payload is None:
            # 本轮取不到数据：沿用上一帧，保留"数据延迟"的提示
            payload = pipeline.stale_payload()

        if payload is None:
            # 从未成功过又没有提示可用——给一个结构完整的空帧
            return empty_frame(lat, lon, radius_km, pipeline.last_notice)

        ttl = max(3.0, float(self._config.refresh_sec))
        self._cache[key] = (time.monotonic() + ttl, payload)
        return payload
