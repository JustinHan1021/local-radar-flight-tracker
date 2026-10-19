"""天气：当前位置的温度、风速、云量与云图。

数据来自 **Open-Meteo**（免费、无需 API Key、实测响应约 1 秒）。

云图的获取方式值得一提：免费源里没有现成的"云量栅格"可以用
（RainViewer 的卫星红外通道当前无数据帧），因此这里改为
**用网格采样自己合成**——向 Open-Meteo 批量查询视口内 N×N 个点的云量，
插值成一张低分辨率云量图，再平滑放大后叠加为半透明白色。

一个刻意的近似：云量场是按经纬度均匀网格采样的，叠加到雷达盘时按
**线性映射**处理，而没有走极坐标等距投影。在雷达视野的尺度下这个误差可以忽略
（460 km 视野覆盖约 2.3° 纬度，经度方向尺度因子的变化不到 1%），
换来的是云图可以直接用图像缩放生成平滑过渡，而不必逐像素反向投影。
"""

from __future__ import annotations

import json
import math
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from app.geo import EARTH_RADIUS_KM

_API_URL = "https://api.open-meteo.com/v1/forecast"
_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

#: WMO 天气现象编码 → 中文描述
WMO_CODES: dict[int, str] = {
    0: "晴",
    1: "少云",
    2: "多云",
    3: "阴",
    45: "雾",
    48: "冻雾",
    51: "毛毛雨",
    53: "毛毛雨",
    55: "毛毛雨",
    56: "冻毛毛雨",
    57: "冻毛毛雨",
    61: "小雨",
    63: "中雨",
    65: "大雨",
    66: "冻雨",
    67: "冻雨",
    71: "小雪",
    73: "中雪",
    75: "大雪",
    77: "米雪",
    80: "阵雨",
    81: "阵雨",
    82: "强阵雨",
    85: "阵雪",
    86: "强阵雪",
    95: "雷暴",
    96: "雷暴伴冰雹",
    99: "强雷暴伴冰雹",
}

_COMPASS = ("北", "东北偏北", "东北", "东北偏东", "东", "东南偏东", "东南", "东南偏南",
            "南", "西南偏南", "西南", "西南偏西", "西", "西北偏西", "西北", "西北偏北")


#: 天气与云图的短期缓存。
#: Open-Meteo 免费额度有限，实测短时间大量调用会返回 429；
#: 而天气本身变化也慢（温度/云量按小时级演进），没必要每次都问。
#: 缓存键对位置做粗量化——同一区域内的轻微移动复用同一份数据。
_WEATHER_CACHE: dict[tuple, tuple[float, "Weather"]] = {}
_CLOUD_CACHE: dict[tuple, tuple[float, "CloudField"]] = {}

#: 缓存有效期（秒）
_WEATHER_TTL = 600.0        # 天气 10 分钟
_CLOUD_TTL = 600.0          # 云图 10 分钟


def clear_weather_cache() -> None:
    """清空天气与云图缓存（设置面板的"立即刷新"可用）。"""
    _WEATHER_CACHE.clear()
    _CLOUD_CACHE.clear()


def _fetch_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


@dataclass(frozen=True)
class Weather:
    """某一位置的当前天气。"""

    temperature_c: float
    wind_speed_kmh: float
    wind_direction_deg: float
    cloud_cover: float        #: 云量百分比 0–100
    humidity: float           #: 相对湿度百分比
    weather_code: int
    time_text: str = ""

    @property
    def condition(self) -> str:
        return WMO_CODES.get(self.weather_code, "未知")

    @property
    def wind_direction_text(self) -> str:
        index = int((self.wind_direction_deg + 11.25) % 360.0 // 22.5)
        return _COMPASS[index]

    @property
    def cloud_text(self) -> str:
        return f"{self.cloud_cover:.0f}%"

    def summary(self) -> str:
        """一行摘要，用于雷达角标，如 ``26.3°C · 西南风 7.6km/h · 云量 0%``。"""
        return (f"{self.temperature_c:.1f}°C  ·  {self.wind_direction_text}风 "
                f"{self.wind_speed_kmh:.1f}km/h  ·  {self.condition} {self.cloud_text}")

    def detail(self) -> str:
        """更完整的描述，用于详情展示。"""
        return (f"{self.condition}  {self.temperature_c:.1f}°C  "
                f"湿度 {self.humidity:.0f}%  "
                f"{self.wind_direction_text}风 {self.wind_speed_kmh:.1f} km/h "
                f"({self.wind_direction_deg:.0f}°)  云量 {self.cloud_cover:.0f}%")


def fetch_weather(lat: float, lon: float, timeout: float = 12.0) -> Weather | None:
    """查询给定位置的当前天气。失败时返回 ``None``，由调用方决定是否降级。"""
    cache_key = (round(lat, 2), round(lon, 2))
    cached = _WEATHER_CACHE.get(cache_key)
    if cached is not None and time.time() - cached[0] < _WEATHER_TTL:
        return cached[1]

    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "current": ("temperature_2m,wind_speed_10m,wind_direction_10m,"
                    "cloud_cover,relative_humidity_2m,weather_code"),
        "timezone": "auto",
    }
    try:
        payload = _fetch_json(f"{_API_URL}?{urllib.parse.urlencode(params)}", timeout)
    except Exception:  # noqa: BLE001 - 天气是附加信息，失败不应影响主流程
        # 网络失败时退回旧缓存（哪怕过期），总比什么都不显示好
        return cached[1] if cached else None

    current = payload.get("current")
    if not isinstance(current, dict):
        return cached[1] if cached else None

    def number(key: str, default: float = 0.0) -> float:
        value = current.get(key)
        return float(value) if isinstance(value, (int, float)) else default

    weather = Weather(
        temperature_c=number("temperature_2m"),
        wind_speed_kmh=number("wind_speed_10m"),
        wind_direction_deg=number("wind_direction_10m"),
        cloud_cover=number("cloud_cover"),
        humidity=number("relative_humidity_2m"),
        weather_code=int(number("weather_code")),
        time_text=str(current.get("time") or ""),
    )
    _WEATHER_CACHE[cache_key] = (time.time(), weather)
    return weather


@dataclass
class CloudField:
    """视口内的云量场。

    ``values`` 是 ``rows × cols`` 的二维数组（0–100 的云量百分比），
    行对应纬度、列对应经度；``bounds`` 是它覆盖的经纬度范围。
    """

    rows: int
    cols: int
    values: list[list[float]]
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    @property
    def is_empty(self) -> bool:
        return self.rows < 2 or self.cols < 2 or not self.values

    def sample(self, lat: float, lon: float) -> float | None:
        """取某点云量（双线性插值）。"""
        if self.is_empty:
            return None
        if not (self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max):
            return None

        row = (lat - self.lat_min) / max(1e-9, self.lat_max - self.lat_min) * (self.rows - 1)
        col = (lon - self.lon_min) / max(1e-9, self.lon_max - self.lon_min) * (self.cols - 1)

        row0 = max(0, min(self.rows - 2, int(row)))
        col0 = max(0, min(self.cols - 2, int(col)))
        dr = row - row0
        dc = col - col0

        top = self.values[row0][col0] * (1 - dc) + self.values[row0][col0 + 1] * dc
        bottom = self.values[row0 + 1][col0] * (1 - dc) + self.values[row0 + 1][col0 + 1] * dc
        return top * (1 - dr) + bottom * dr


def _grid_bounds(lat: float, lon: float, radius_km: float, padding: float = 1.25) -> tuple:
    """以给定点为圆心、按半径算出的经纬度范围（略作外扩以覆盖整个雷达盘）。"""
    span_km = radius_km * padding
    dlat = math.degrees(span_km / EARTH_RADIUS_KM)
    cos_lat = max(0.05, math.cos(math.radians(lat)))
    dlon = dlat / cos_lat
    return (
        max(-90.0, lat - dlat),
        min(90.0, lat + dlat),
        lon - dlon,
        lon + dlon,
    )


def fetch_cloud_field(
    lat: float,
    lon: float,
    radius_km: float,
    grid: int = 14,
    timeout: float = 25.0,
) -> CloudField | None:
    """采样视口内的云量场。

    用 Open-Meteo 的多坐标批量查询一次取回 ``grid × grid`` 个点，
    再插值成云图。``grid`` 取 14 是精度与请求大小的折中：
    196 个坐标参数、URL 约 4 KB，响应 1–2 秒。
    网格太粗会让云团变成明显的方块（12 时每格铺满近 80 像素），
    太细则 URL 过长，实测 16 时大半径下会超时。
    """
    grid = max(4, min(16, grid))

    # 缓存键对位置与半径做粗量化：云量按小时级演进，
    # 视口小幅移动完全可以直接复用，没必要再发一次 196 个坐标的请求
    cache_key = (round(lat, 1), round(lon, 1), round(radius_km / 10.0))
    cached = _CLOUD_CACHE.get(cache_key)
    if cached is not None and time.time() - cached[0] < _CLOUD_TTL:
        return cached[1]

    lat_min, lat_max, lon_min, lon_max = _grid_bounds(lat, lon, radius_km)

    lats: list[str] = []
    lons: list[str] = []
    for row in range(grid):
        row_lat = lat_min + (lat_max - lat_min) * row / (grid - 1)
        for col in range(grid):
            col_lon = lon_min + (lon_max - lon_min) * col / (grid - 1)
            lats.append(f"{row_lat:.3f}")
            lons.append(f"{col_lon:.3f}")

    params = {
        "latitude": ",".join(lats),
        "longitude": ",".join(lons),
        "current": "cloud_cover",
        "timezone": "UTC",
    }
    try:
        payload = _fetch_json(f"{_API_URL}?{urllib.parse.urlencode(params)}", timeout)
    except Exception:  # noqa: BLE001 - 云图失败就当成无云处理
        return None

    # 批量查询返回的是一个数组；单点会返回对象，这里统一成列表
    items = payload if isinstance(payload, list) else [payload]

    values: list[list[float]] = []
    index = 0
    for _ in range(grid):
        row_values: list[float] = []
        for _ in range(grid):
            cloud = 0.0
            if index < len(items) and isinstance(items[index], dict):
                current = items[index].get("current") or {}
                value = current.get("cloud_cover")
                if isinstance(value, (int, float)):
                    cloud = float(value)
            row_values.append(cloud)
            index += 1
        values.append(row_values)

    field = CloudField(
        rows=grid,
        cols=grid,
        values=values,
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
    )
    _CLOUD_CACHE[cache_key] = (time.time(), field)
    return field
