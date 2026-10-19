"""航班数据编排：拉取、降级、限流冷却、过滤与帧平滑。

**本模块刻意不依赖 PySide6。** 桌面版（``app/data/manager.py``）与 Web 服务端
（``webapp/``）共用这里的同一套逻辑——否则"主源限流时保留上一帧"这类判断会在
两个实现里慢慢漂移，而它恰恰是最容易做错、也最难察觉的部分。

与桌面版的对应关系：``_FetchThread`` 只负责"什么时候调"，本模块负责"怎么调"。
线程/信号/事件循环那层留在各自的宿主里。

上游限流是**按服务器**计的，而查询位置是**按客户端**的。所以冷却状态必须由
:class:`UpstreamGuard` 在进程内全局共享：若每个查询各存一份冷却，
10 个客户端看 10 个地点就会各自去撞同一个 429。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from app.data.base import SourceError, SourceRateLimited, SourceUnavailable
from app.data.enrich import Enricher
from app.data.models import Flight
from app.data.sources import FALLBACK_CHAIN, create_source

#: 撞上 429 后该源的冷却时长（秒）。
#: adsb.lol 的配额实测是「约 60 秒窗口内 3 次」，留足余量取 90 秒；
#: 否则降级链每轮（10 秒）都去试，只会一直撞 429。
RATE_LIMIT_COOLDOWN = 90.0

#: 两次请求之间的最小间隔（秒）。手动刷新过密会触发数据源限流，
#: 实测 adsb.lol 在短时间内重复请求时会直接返回 429。
MIN_REQUEST_INTERVAL = 2.0

#: :meth:`FlightPipeline.fetch` 的返回值之一：本轮被最小请求间隔挡下，什么都没做。
#:
#: 必须与"拉取失败"（返回 ``None``）区分开——失败的语义是"发陈旧帧并提示"，
#: 而挡下的语义是"保持现状、连提示都不要"。混为一谈会让手速连点刷新时
#: 冒出一句莫名其妙的"数据延迟"。
SKIPPED = object()


@dataclass(frozen=True)
class FetchParams:
    """一次拉取所需的全部参数（不可变，便于跨线程传递）。"""

    lat: float
    lon: float
    radius_km: float
    source: str
    fr24_api_key: str = ""
    stale_limit_sec: float = 60.0
    show_ground: bool = False


class UpstreamGuard:
    """跨查询共享的上游限流状态。

    两个职责，都是"按服务器"而非"按查询"的量：

    * **冷却**：某源返回 429 后，在一段时间内所有查询都跳过它，
      避免每个客户端、每轮刷新都去撞同一个配额。
    * **最小请求间隔**：人工刷新按钮可以被连点，这里做全局节流。
    """

    def __init__(self) -> None:
        self._cooldown: dict[str, float] = {}
        self._lock = threading.Lock()
        self._last_request_at = float("-inf")

    def is_cooling(self, name: str) -> bool:
        with self._lock:
            return self._cooldown.get(name, 0.0) > time.monotonic()

    def cooldown_remaining(self, name: str) -> float:
        with self._lock:
            return max(0.0, self._cooldown.get(name, 0.0) - time.monotonic())

    def note_rate_limited(self, name: str) -> None:
        with self._lock:
            self._cooldown[name] = time.monotonic() + RATE_LIMIT_COOLDOWN

    def cooling_snapshot(self) -> dict[str, int]:
        """当前处于冷却中的源 → 剩余秒数（整数，供接口展示）。"""
        now = time.monotonic()
        with self._lock:
            return {
                name: int(deadline - now)
                for name, deadline in self._cooldown.items()
                if deadline > now
            }

    def allow_request(self) -> bool:
        """是否已越过最小请求间隔。返回 False 表示本次调用应被丢弃。"""
        now = time.monotonic()
        with self._lock:
            if now - self._last_request_at < MIN_REQUEST_INTERVAL:
                return False
            self._last_request_at = now
            return True


class FlightPipeline:
    """一轮完整的「拉取 → 富化 → 过滤 → 组帧」。

    每个查询位置一个实例（各自维护上一帧与位置历史），
    但共享同一个 :class:`UpstreamGuard`。
    """

    def __init__(
        self,
        guard: UpstreamGuard | None = None,
        enricher: Enricher | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> None:
        self._guard = guard or UpstreamGuard()
        self._on_status = on_status or (lambda _message: None)
        self.enricher = enricher if enricher is not None else Enricher()
        #: 上一轮成功的结果，用于在取不到新数据时兜底
        self._last_good: dict | None = None
        #: 上一帧各航班的位置，供界面做插值
        self._positions: dict[str, tuple[float, float]] = {}
        #: 主源被限流时记下的"实际生效源"，供陈旧帧展示
        self._last_source = ""
        #: 最近一次失败的提示语，供陈旧帧展示
        self._last_notice = ""
        #: 主源实例缓存。主源每轮复用同一个连接会话；降级源才是用完即弃。
        self._primary_source = None
        self._primary_key: tuple[str, str] = ("", "")

    # ------------------------------------------------------------------
    # 对外
    # ------------------------------------------------------------------

    @property
    def last_payload(self) -> dict | None:
        return self._last_good

    @property
    def last_notice(self) -> str:
        """最近一次的提示语（降级/限流/失败），供陈旧帧或空帧展示。"""
        return self._last_notice

    def clear_positions(self) -> None:
        """圆心移动后调用：清掉位置历史，否则插值会画出跨越半张图的拉线。"""
        self._positions.clear()

    def close(self) -> None:
        self.enricher.close()
        _close_quietly(self._primary_source)
        self._primary_source = None
        self._primary_key = ("", "")

    def _get_primary(self, name: str, api_key: str):
        """取（必要时重建）主源实例。复用会话以保留连接池。"""
        key = (name, api_key or "")
        if self._primary_source is None or self._primary_key != key:
            _close_quietly(self._primary_source)
            self._primary_source = create_source(name, api_key or "")
            self._primary_key = key
        return self._primary_source

    def fetch(self, params: FetchParams) -> dict | None | object:
        """执行一轮。

        返回三种值，调用方必须分别处理：

        * ``dict`` —— 新的帧
        * ``None`` —— 本轮取不到数据，调用方应改用 :meth:`stale_payload`
        * :data:`SKIPPED` —— 被最小请求间隔挡下，调用方**什么都不做**
        """
        if not self._guard.allow_request():
            return SKIPPED

        self._on_status("正在拉取航班数据…")
        flights, used_source, notice = self._fetch_with_fallback(params)
        if flights is None:
            self._last_source = used_source or self._last_source
            self._last_notice = notice or self._last_notice
            return None

        self._on_status("正在补全航班信息…")
        self.enricher.enrich(flights)

        filtered = filter_and_measure(
            flights,
            params.lat,
            params.lon,
            params.radius_km,
            stale_limit_sec=params.stale_limit_sec,
            show_ground=params.show_ground,
        )
        self._apply_prev_positions(filtered)

        payload = {
            "flights": filtered,
            "source": used_source,
            "notice": notice,
            "center": (params.lat, params.lon),
            "radius_km": params.radius_km,
            "fetched_at": datetime.now(timezone.utc),
            "stale": False,
        }
        self._last_good = payload
        self._last_source = used_source
        return payload

    def stale_payload(self) -> dict | None:
        """本轮未取到新数据时，沿用上一帧内容并标记为陈旧。

        雷达盘突然清空会被误读成"附近没有航班"，保留旧数据才是合理的降级表现。

        注意：上一帧本来就是空的（比如该区域没有任何航班）时**不标陈旧**——
        那时"数据延迟"只会让人以为还在加载，实际是这片儿本来就没数据。
        """
        previous = self._last_good
        if previous is None:
            return None
        payload = dict(previous)
        payload["stale"] = bool(previous.get("flights"))
        if self._last_notice:
            payload["notice"] = self._last_notice
        if self._last_source:
            payload["source"] = self._last_source
        return payload

    # ------------------------------------------------------------------
    # 降级链
    # ------------------------------------------------------------------

    def _fetch_with_fallback(
        self, params: FetchParams
    ) -> tuple[list[Flight] | None, str, str]:
        """按「主源 → 降级链」顺序尝试，返回 ``(航班, 实际源, 提示)``。

        **返回空列表也算"这个源在这儿没数据"，继续试下一个源。**
        实测各源的覆盖是互补的：香港 adsb.fi 返回 11 架而 adsb.lol 返回 21 架，
        北京 adsb.fi 返回 0 而 adsb.lol 返回 8 架——只认第一个源会白白丢掉航班。
        多个源都空时才认定"该区域确实没有航班"。

        全部失败（网络/限流）时返回 ``(None, "", 错误信息)``。
        """
        primary = params.source
        chain = [primary] + [name for name in FALLBACK_CHAIN if name != primary]
        last_error = "未知错误"
        primary_error = ""
        empty_source = ""
        any_available = False

        for name in chain:
            # 冷却期内的源直接跳过，别去撞 429
            if self._guard.is_cooling(name):
                continue
            any_available = True

            is_primary = name == primary
            source = (
                self._get_primary(name, params.fr24_api_key)
                if is_primary
                else create_source(name)
            )

            try:
                flights = source.fetch(params.lat, params.lon, params.radius_km)
            except SourceRateLimited as exc:
                self._guard.note_rate_limited(name)
                last_error = str(exc)
                if is_primary:
                    primary_error = last_error
                    self._on_status(
                        f"{source.label} 请求过于频繁，"
                        f"{int(RATE_LIMIT_COOLDOWN)} 秒后自动重试"
                    )
                flights = None
                # 主源限流就保留上一帧等冷却；降级源限流则继续试下一个
                if is_primary:
                    return None, name, str(exc)
            except (SourceUnavailable, SourceError) as exc:
                last_error = str(exc)
                if is_primary:
                    primary_error = last_error
                flights = None
            except Exception as exc:  # noqa: BLE001 - 兜底，避免调用方被异常终结
                last_error = f"{type(exc).__name__}: {exc}"
                if is_primary:
                    primary_error = last_error
                flights = None
            finally:
                # 降级时临时构造的源用完即弃，避免连接泄漏
                if not is_primary:
                    _close_quietly(source)

            if flights is None:
                continue

            if flights:
                if is_primary:
                    return flights, name, ""
                # 降级成功。提示要说清主源到底是"挂了"还是"这片儿没数据"，
                # 两种情况的处理建议完全不同。
                if primary_error:
                    notice = f"主源 {primary} 不可用：{primary_error}"
                else:
                    notice = f"主源 {primary} 在该区域无数据，已改用 {name}"
                return flights, name, notice

            # 空结果：记下来，但继续问下一个源
            if not empty_source:
                empty_source = name

        if empty_source:
            # 有源答复了、只是都没数据——多半是该区域接收站覆盖少
            return [], empty_source, ""

        if not any_available:
            # 所有源都在冷却期：保留上一帧，把最可能的原因说清楚
            cooling = self._guard.cooling_snapshot()
            detail = "、".join(f"{name}({secs}s)" for name, secs in cooling.items())
            return None, "", "所有数据源都在限流冷却中：" + detail

        self._on_status(f"数据获取失败：{last_error}")
        return None, "", last_error

    # ------------------------------------------------------------------
    # 帧平滑
    # ------------------------------------------------------------------

    def _apply_prev_positions(self, flights: list[Flight]) -> None:
        """把上一帧的位置写进本帧，供界面插值；并更新位置历史。"""
        for flight in flights:
            previous = self._positions.get(flight.icao24)
            if previous is not None:
                flight.prev_lat, flight.prev_lon = previous
            # 首次出现的航班 prev 保持 None，界面据此不做插值、直接落位

        self._positions = {flight.icao24: (flight.lat, flight.lon) for flight in flights}


def filter_and_measure(
    flights: list[Flight],
    lat: float,
    lon: float,
    radius_km: float,
    *,
    stale_limit_sec: float = 60.0,
    show_ground: bool = False,
) -> list[Flight]:
    """按半径与陈旧度过滤，并重算距离与方位。

    距离与方位一律本地计算：adsb.lol 返回的 ``dst`` 单位是**海里**，
    直接采信会造成单位混用；OpenSky 则根本不提供这两个字段。
    """
    kept: list[Flight] = []
    for flight in flights:
        if flight.seen_pos > stale_limit_sec:
            # 位置太旧，画出来会误导用户
            continue
        if flight.on_ground and not show_ground:
            # 机场停机坪上的航班数量极多，默认隐藏以免淹没雷达盘
            continue

        flight.update_relative(lat, lon)
        if flight.distance_km > radius_km:
            # OpenSky 按矩形查询，圆外结果需要剔除
            continue
        kept.append(flight)

    kept.sort(key=lambda item: item.distance_km)
    return kept


def _close_quietly(source) -> None:
    """关闭数据源连接，忽略任何异常（部分源没有 close 方法）。"""
    closer = getattr(source, "close", None)
    if callable(closer):
        try:
            closer()
        except Exception:  # noqa: BLE001
            pass
