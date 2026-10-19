"""数据源工厂：按名称构造适配器实例，并给出降级链。

新增数据源只需实现 :class:`~app.data.base.FlightSource`，
再在此登记一行即可，调用方无需改动。
"""

from __future__ import annotations

from app.data.base import FlightSource, SourceError, SourceRateLimited, SourceUnavailable
from app.data.source_fr24 import Fr24Source
from app.data.source_opensky import OpenSkySource
from app.data.source_tar1090 import AdsbfiSource, AdsblolSource

__all__ = [
    "FALLBACK_CHAIN",
    "FlightSource",
    "SourceError",
    "SourceRateLimited",
    "SourceUnavailable",
    "available_sources",
    "create_source",
]

#: 默认数据源。实测 adsb.fi 是唯一能支撑 10 秒刷新的免费源。
DEFAULT_SOURCE = "adsbfi"

#: 数据源标识 → 构造成员
_BUILDERS = {
    "adsbfi": lambda api_key: AdsbfiSource(),
    "adsblol": lambda api_key: AdsblolSource(),
    "opensky": lambda api_key: OpenSkySource(),
    "fr24": lambda api_key: Fr24Source(api_key),
}

#: 降级链：主源不可用时依次尝试。
#: 顺序依据实测：adsb.fi 不限流但需确保可用 → adsb.lol 数据多但配额严 → OpenSky 兜底。
FALLBACK_CHAIN = ("adsbfi", "adsblol", "opensky")


def available_sources() -> tuple[str, ...]:
    """返回全部可选数据源标识。"""
    return tuple(_BUILDERS.keys())


def create_source(name: str, api_key: str = "") -> FlightSource:
    """按名称创建数据源实例。

    未知名称不抛异常，而是退回默认源——配置文件被改坏时仍能正常出图。
    """
    builder = _BUILDERS.get((name or "").strip().lower()) or _BUILDERS[DEFAULT_SOURCE]
    return builder(api_key)
