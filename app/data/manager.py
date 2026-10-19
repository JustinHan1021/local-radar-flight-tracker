"""航班数据调度：后台拉取线程与对外门面。

**拉取的逻辑本身不在这里。** 降级链、限流冷却、过滤、帧间位置平滑都在
:mod:`app.data.pipeline`，由桌面版与 Web 服务端共用同一份——那几处判断最容易
做错、也最难察觉（例如"主源限流时该保留上一帧而不是清空"）。

本模块只负责"什么时候调"：QThread、固定周期、唤醒，以及把结果转成 Qt 信号。

信号携带的 payload 结构见 :meth:`app.data.pipeline.FlightPipeline.fetch`。
"""

from __future__ import annotations

import threading
import time
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from app.data.pipeline import (
    RATE_LIMIT_COOLDOWN,
    SKIPPED,
    FetchParams,
    FlightPipeline,
    UpstreamGuard,
)

if TYPE_CHECKING:
    from app.config import Config

#: 原先定义在本模块，为兼容既有引用继续对外导出。
__all__ = ["FlightManager", "RATE_LIMIT_COOLDOWN"]


class _FetchThread(QThread):
    """后台工作线程：循环执行「取参数 → 拉取一轮 → 广播」。"""

    result_ready = Signal(object)
    status_changed = Signal(str)

    def __init__(
        self,
        config: "Config",
        guard: UpstreamGuard | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._params_lock = threading.Lock()

        manual = config.manual_location()
        self._lat: float | None = manual[0] if manual else None
        self._lon: float | None = manual[1] if manual else None
        self._radius_km = config.radius_km
        self._interval = float(config.refresh_sec)
        self._source_name = config.source
        self._api_key = config.fr24_api_key
        self._reset_positions = False

        self._pipeline = FlightPipeline(guard=guard, on_status=self.status_changed.emit)

    # ------------------------------------------------------------------
    # 参数更新（由主线程调用）
    # ------------------------------------------------------------------

    def update_params(
        self,
        lat: float | None = None,
        lon: float | None = None,
        radius_km: float | None = None,
        interval: float | None = None,
        source_name: str | None = None,
        api_key: str | None = None,
        reset_positions: bool = False,
    ) -> None:
        """更新查询参数，并唤醒线程立即用新参数拉取一次。

        ``reset_positions`` 必须在**线程内**生效：清位置历史与写位置历史如果
        分处两个线程，插值可能读到半新半旧的坐标，画出跨越半张图的拉线。
        """
        with self._params_lock:
            if lat is not None and lon is not None:
                self._lat, self._lon = lat, lon
            if radius_km is not None:
                self._radius_km = float(radius_km)
            if interval is not None:
                self._interval = max(3.0, float(interval))
            if source_name:
                self._source_name = source_name
            if api_key is not None:
                self._api_key = api_key
            if reset_positions:
                self._reset_positions = True
        self._wake_event.set()

    @property
    def has_location(self) -> bool:
        with self._params_lock:
            return self._lat is not None and self._lon is not None

    @property
    def current_source_name(self) -> str:
        with self._params_lock:
            return self._source_name

    # ------------------------------------------------------------------
    # 线程主体
    # ------------------------------------------------------------------

    def run(self) -> None:  # noqa: D102 - QThread 约定
        while not self._stop_event.is_set():
            # 进入本轮前清空唤醒标记，否则 start() 时写入的参数会残留，
            # 导致启动瞬间连拉两轮
            self._wake_event.clear()

            started = time.monotonic()
            if self.has_location:
                self._fetch_once()

            with self._params_lock:
                interval = self._interval

            # 按"固定周期"等待，而不是在拉取之后再等满一个间隔：
            # 拉取本身要 1–3 秒，后者会让实际刷新周期越走越长
            remaining = interval - (time.monotonic() - started)
            if remaining > 0:
                self._wake_event.wait(remaining)
                self._wake_event.clear()

        # 退出前把富化缓存落盘、并关闭主源连接
        self._pipeline.close()

    def request_stop(self) -> None:
        """请求线程退出，并打断正在等待的间隔。"""
        self._stop_event.set()
        self._wake_event.set()

    def request_refresh(self) -> None:
        """请求立即刷新（受最小请求间隔保护）。"""
        self._wake_event.set()

    # ------------------------------------------------------------------
    # 单轮拉取
    # ------------------------------------------------------------------

    def _fetch_once(self) -> None:
        with self._params_lock:
            if self._lat is None or self._lon is None:
                return
            params = FetchParams(
                lat=self._lat,
                lon=self._lon,
                radius_km=self._radius_km,
                source=self._source_name,
                fr24_api_key=self._api_key,
                stale_limit_sec=float(self._config.stale_limit_sec),
                show_ground=bool(self._config.show_ground),
            )
            reset = self._reset_positions
            self._reset_positions = False

        if reset:
            self._pipeline.clear_positions()

        payload = self._pipeline.fetch(params)

        if payload is SKIPPED:
            # 手动刷新过密，跳过本轮，交给自然周期
            return

        if payload is None:
            # 本轮取不到数据：沿用上一帧，避免雷达盘闪空
            stale = self._pipeline.stale_payload()
            if stale is not None:
                self.result_ready.emit(stale)
            return

        self.result_ready.emit(payload)


class FlightManager(QObject):
    """航班数据对外门面：管理后台线程生命周期，并转发结果。"""

    flights_updated = Signal(object)
    status_changed = Signal(str)

    def __init__(
        self,
        config: "Config",
        guard: UpstreamGuard | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._guard = guard
        self._thread: _FetchThread | None = None
        self._last_payload: dict | None = None

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self, lat: float, lon: float) -> None:
        """启动后台拉取。``lat`` / ``lon`` 为已确定的用户位置。"""
        self.stop()
        thread = _FetchThread(self._config, guard=self._guard)
        thread.update_params(lat=lat, lon=lon)
        thread.result_ready.connect(self._on_result)
        thread.status_changed.connect(self.status_changed)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """停止后台线程并等待其退出。"""
        thread = self._thread
        if thread is None:
            return
        thread.request_stop()
        if not thread.wait(8000):
            thread.terminate()
            thread.wait(1000)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.isRunning()

    # ------------------------------------------------------------------
    # 参数变更
    # ------------------------------------------------------------------

    def set_location(self, lat: float, lon: float) -> None:
        """圆心移动。清空位置历史，避免插值出现跨越半张图的拉线。"""
        if self._thread is not None:
            self._thread.update_params(lat=lat, lon=lon, reset_positions=True)

    def set_radius(self, radius_km: float) -> None:
        if self._thread is not None:
            self._thread.update_params(radius_km=radius_km)

    def set_interval(self, seconds: float) -> None:
        if self._thread is not None:
            self._thread.update_params(interval=seconds)

    def set_source(self, name: str, api_key: str = "") -> None:
        if self._thread is not None:
            self._thread.update_params(source_name=name, api_key=api_key)

    def refresh_now(self) -> None:
        """立即触发一次拉取，不等当前周期走完。"""
        if self._thread is not None:
            self._thread.request_refresh()

    def clear_position_history(self) -> None:
        if self._thread is not None:
            self._thread.update_params(reset_positions=True)

    # ------------------------------------------------------------------
    # 结果处理
    # ------------------------------------------------------------------

    def _on_result(self, payload: dict) -> None:
        """帧间位置平滑已由 pipeline 完成，这里只做记录与转发。"""
        self._last_payload = payload
        self.flights_updated.emit(payload)

    @property
    def last_payload(self) -> dict | None:
        return self._last_payload

    @property
    def current_source_name(self) -> str:
        return self._thread.current_source_name if self._thread else self._config.source
