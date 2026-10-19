"""Flightradar24 数据源适配器（预留，未接入）。

Flightradar24 官方 API 需企业订阅，没有可用的免费公开端点。
本模块只做**结构预留**：保留设置项与接口签名，让"数据源配置"这一需求完整落地。

凭据缺失或为空时明确抛出 :class:`SourceUnavailable`，
由调度层捕获后降级到 adsb.lol 并提示用户，而不是静默失败。

刻意不实现任何猜测性的抓取逻辑：那既不稳定，也会违反服务条款。
"""

from __future__ import annotations

from app.data.base import FlightSource, SourceUnavailable
from app.data.models import Flight

_PENDING_MESSAGE = (
    "Flightradar24 需要企业订阅，当前版本尚未接入该数据源。"
    "请在「设置 → 数据源」中切换为 adsb.lol，或填入有效凭据后再启用。"
)


class Fr24Source(FlightSource):
    """Flightradar24 数据源占位实现。"""

    name = "fr24"
    label = "Flightradar24"

    def __init__(self, api_key: str = "") -> None:
        self._api_key = api_key.strip()

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    def fetch(
        self, lat: float, lon: float, radius_km: float, timeout: float = 15.0
    ) -> list[Flight]:
        raise SourceUnavailable(_PENDING_MESSAGE)
