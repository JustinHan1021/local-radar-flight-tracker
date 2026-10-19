"""地名：城市名标注，按雷达半径自适应分级。

数据来自 Natural Earth 的 ``ne_10m_populated_places``（7,342 个城镇点），
带 ``NAME_ZH`` 中文字段与人口数据。

**为什么要分级**：如果把 7 千个城镇点全画出来，雷达盘会被文字糊满；
只画特大城市，缩小到 10 km 半径时又会空无一物。因此按雷达半径设人口阈值——
视野越大只留大城市，视野越小放得越细，这正是需求要的效果。

标签之间的重叠由绘制方（``radar_view``）按人口降序逐个避让解决，
本模块只负责"这个视野下应该出现哪些城市"。
"""

from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

from app.airports import base_city
from app.config import PROJECT_ROOT
from app.geo import haversine_km

DATA_DIR = PROJECT_ROOT / "assets" / "data"
CACHE_DIR = PROJECT_ROOT / "assets" / "cache"

_PLACES_URL = ("https://raw.githubusercontent.com/nvkelso/natural-earth-vector/"
               "master/geojson/ne_10m_populated_places.geojson")
_PLACES_FILE = "ne_10m_populated_places.geojson"

_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

#: pickle 结构版本
_CACHE_VERSION = 1

_MIN_VALID_BYTES = 5_000_000

#: 低于此人口的城镇直接丢弃——它们对航班追踪没有参照价值，
#: 保留只会让数据体积与查询开销变大
_MIN_POPULATION = 5_000

#: 雷达半径 → 显示所需的最低人口。
#: 视野越大越只留大城市，越小则越详细。顺序必须按半径升序。
_POPULATION_STEPS: tuple[tuple[float, int], ...] = (
    (12.0, 0),            # 极近视野：全部显示（含小镇）
    (30.0, 8_000),
    (60.0, 25_000),
    (120.0, 90_000),
    (250.0, 250_000),
    (float("inf"), 700_000),
)

#: 单帧最多画多少个城市标签，避免文字糊满雷达盘
_MAX_LABELS = 26


@dataclass(frozen=True)
class Place:
    """一个城镇点。"""

    name: str          #: 中文名（缺失时回退英文）
    name_en: str
    population: int
    lat: float
    lon: float

    @property
    def is_major(self) -> bool:
        """是否特大城市（用于加粗显示）。"""
        return self.population >= 1_000_000

    @property
    def label(self) -> str:
        return self.name or self.name_en


def population_threshold(radius_km: float) -> int:
    """根据雷达半径取人口阈值。"""
    for limit, threshold in _POPULATION_STEPS:
        if radius_km <= limit:
            return threshold
    return _POPULATION_STEPS[-1][1]


def _to_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result


class PlaceDatabase:
    """城镇点数据的加载与查询。"""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self._places: list[Place] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # 数据准备
    # ------------------------------------------------------------------

    def data_ready(self) -> bool:
        return (self.data_dir / _PLACES_FILE).exists()

    def download_missing(self, timeout: float = 240.0) -> tuple[bool, str]:
        """下载城镇点数据（约 18 MB）。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / _PLACES_FILE
        if target.exists() and target.stat().st_size >= _MIN_VALID_BYTES:
            return True, "地名数据就绪"

        try:
            response = requests.get(
                _PLACES_URL, timeout=timeout, headers={"User-Agent": _USER_AGENT}
            )
            if response.status_code != 200 or len(response.content) < _MIN_VALID_BYTES:
                return False, f"地名数据下载失败（HTTP {response.status_code}）"
            temp = target.with_name(target.name + ".part")
            temp.write_bytes(response.content)
            temp.replace(target)
        except (requests.RequestException, OSError) as exc:
            return False, f"地名数据下载失败：{type(exc).__name__}"
        return True, "地名数据就绪"

    def load(self) -> bool:
        """载入城镇点（优先走 pickle 缓存）。"""
        if self._loaded:
            return bool(self._places)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache = CACHE_DIR / f"places_v{_CACHE_VERSION}.pickle"
        source = self.data_dir / _PLACES_FILE

        if (cache.exists() and source.exists()
                and cache.stat().st_mtime >= source.stat().st_mtime):
            try:
                with cache.open("rb") as handle:
                    places = pickle.load(handle)
                if isinstance(places, list):
                    self._places = places
                    self._loaded = True
                    return bool(places)
            except (OSError, pickle.PickleError, EOFError, AttributeError):
                pass

        self._places = self._parse(source)
        self._loaded = True
        try:
            with cache.open("wb") as handle:
                pickle.dump(self._places, handle, protocol=pickle.HIGHEST_PROTOCOL)
        except OSError:
            pass
        return bool(self._places)

    def _parse(self, source: Path) -> list[Place]:
        """解析 GeoJSON，只保留必要字段并按人口过滤。"""
        if not source.exists():
            return []
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []

        places: list[Place] = []
        for feature in payload.get("features") or []:
            if not isinstance(feature, dict):
                continue
            properties = feature.get("properties") or {}
            geometry = feature.get("geometry") or {}
            coords = geometry.get("coordinates")
            if not isinstance(coords, (list, tuple)) or len(coords) < 2:
                continue

            lon = _to_float(coords[0])
            lat = _to_float(coords[1])
            if lat is None or lon is None:
                continue

            population = properties.get("POP_MAX") or properties.get("POP_MIN") or 0
            try:
                population = int(population)
            except (TypeError, ValueError):
                population = 0
            if population < _MIN_POPULATION:
                continue

            name_zh = str(properties.get("NAME_ZH") or "").strip()
            name_en = str(properties.get("NAME") or "").strip()
            if not (name_zh or name_en):
                continue

            places.append(Place(
                name=name_zh or name_en,
                name_en=name_en,
                population=population,
                lat=lat,
                lon=lon,
            ))

        return places

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def count(self) -> int:
        return len(self._places)

    def visible(self, lat: float, lon: float, radius_km: float) -> list[Place]:
        """返回该视野下应显示的城市，按人口降序（绘制方据此逐个避让）。

        先用经纬度包围盒粗筛，再按人口阈值过滤，最后限制总数。
        """
        from app.geo import radius_bbox

        lat_min, lat_max, lon_min, lon_max = radius_bbox(lat, lon, radius_km, 0.15)
        threshold = population_threshold(radius_km)

        candidates = [
            place for place in self._places
            if threshold <= place.population
            and lat_min <= place.lat <= lat_max
            and lon_min <= place.lon <= lon_max
            and haversine_km(lat, lon, place.lat, place.lon) <= radius_km * 1.1
        ]
        candidates.sort(key=lambda place: -place.population)
        return candidates[:_MAX_LABELS]
