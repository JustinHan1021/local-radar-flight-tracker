"""地形高程：给陆地上海拔色，低处绿、高处红。

数据来自 **AWS Open Data 的 Terrain Tiles**（Terrarium 编码 PNG，免费无 Key）：
像素值按 ``elevation = R * 256 + G + B / 256 - 32768`` 解出高程（米），
海平面以下自然是负值，因此不需要额外的陆地遮罩——直接跳过负值即可。

瓦片按视口大小选层级并拼接：层级太高会一次拉十几个瓦片（慢），
太低则高程分辨率不够。这里控制在 2×2 个瓦片以内，单个瓦片约 130 KB。

瓦片内容落盘缓存——地形基本不变，同一个位置只需下载一次。

**Qt 依赖延迟到函数内部导入**：Web 服务端不装 PySide6，而下载、拼接、
采样这些事跟 Qt 无关，只有 `elevation_color()` 要返回 QColor。
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import requests

from app.config import PROJECT_ROOT
from app.geo import EARTH_RADIUS_KM

if TYPE_CHECKING:  # 只在类型标注里出现；运行期由 elevation_color() 自行导入
    from PySide6.QtGui import QColor

CACHE_DIR = PROJECT_ROOT / "assets" / "cache" / "terrain"

_TILE_URL = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

#: Terrarium 编码的偏移量
_ELEVATION_OFFSET = 32768.0

#: 层级范围与单边最大瓦片数
_MIN_ZOOM = 5
_MAX_ZOOM = 11
_MAX_TILES_PER_SIDE = 2

#: 高程色带：(高程米, RGB)。低处深绿，高处转红，符合"红到绿表示高到低"。
_ELEVATION_STOPS: tuple[tuple[float, tuple[int, int, int]], ...] = (
    (0.0, (34, 92, 62)),
    (200.0, (60, 118, 68)),
    (600.0, (104, 146, 74)),
    (1200.0, (168, 172, 84)),
    (2000.0, (207, 160, 76)),
    (3000.0, (203, 112, 62)),
    (4500.0, (186, 72, 60)),
    (6500.0, (150, 44, 58)),
)


def _lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    """经纬度 → 瓦片浮点坐标。"""
    count = 2 ** zoom
    x = (lon + 180.0) / 360.0 * count
    lat_rad = math.radians(max(-85.0, min(85.0, lat)))
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * count
    return x, y


def _tile_to_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    """瓦片浮点坐标 → 经纬度。"""
    count = 2 ** zoom
    lon = x / count * 360.0 - 180.0
    n = math.pi - 2.0 * math.pi * y / count
    lat = math.degrees(math.atan(math.sinh(n)))
    return lon, lat


def choose_zoom(lat: float, lon: float, radius_km: float) -> int:
    """选一个让视口落在 2×2 个瓦片以内的层级。"""
    span_km = radius_km * 2.0 * 1.2          # 留 20% 余量
    span_deg = math.degrees(span_km / EARTH_RADIUS_KM)

    for zoom in range(_MAX_ZOOM, _MIN_ZOOM - 1, -1):
        tile_span = 360.0 / (2 ** zoom)
        if span_deg / tile_span <= _MAX_TILES_PER_SIDE:
            return zoom
    return _MIN_ZOOM


def _download_tile(zoom: int, x: int, y: int, timeout: float) -> bytes | None:
    """下载单个地形瓦片（带磁盘缓存）。"""
    path = CACHE_DIR / str(zoom) / str(x) / f"{y}.png"
    if path.exists() and path.stat().st_size > 100:
        try:
            return path.read_bytes()
        except OSError:
            pass

    try:
        response = requests.get(
            _TILE_URL.format(z=zoom, x=x, y=y),
            timeout=timeout,
            headers={"User-Agent": _USER_AGENT},
        )
        if response.status_code != 200 or len(response.content) < 100:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".part")
        temp.write_bytes(response.content)
        temp.replace(path)
        return response.content
    except (requests.RequestException, OSError):
        return None


def _decode_terrarium(raw: bytes) -> np.ndarray | None:
    """把 Terrarium PNG 解成高程数组（米）。"""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(raw)).convert("RGB")
        pixels = np.asarray(image, dtype=np.float64)
    except Exception:  # noqa: BLE001 - 图片损坏时跳过该瓦片
        return None

    return (pixels[:, :, 0] * 256.0 + pixels[:, :, 1] + pixels[:, :, 2] / 256.0
            - _ELEVATION_OFFSET)


@dataclass
class TerrainLayer:
    """某个视口的高程栅格。"""

    elevations: np.ndarray            #: (rows, cols)，单位米
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    zoom: int

    @property
    def rows(self) -> int:
        return int(self.elevations.shape[0])

    @property
    def cols(self) -> int:
        return int(self.elevations.shape[1])

    @property
    def has_land(self) -> bool:
        """栅格里是否存在海平面以上的地形。"""
        return bool((self.elevations > 0).any())

    def sample(self, lat: float, lon: float) -> float | None:
        """取某点高程（最近邻）。"""
        if not (self.lat_min <= lat <= self.lat_max and self.lon_min <= lon <= self.lon_max):
            return None
        row = int((self.lat_max - lat) / max(1e-9, self.lat_max - self.lat_min) * (self.rows - 1))
        col = int((lon - self.lon_min) / max(1e-9, self.lon_max - self.lon_min) * (self.cols - 1))
        row = max(0, min(self.rows - 1, row))
        col = max(0, min(self.cols - 1, col))
        return float(self.elevations[row, col])


def fetch_terrain(
    lat: float, lon: float, radius_km: float, timeout: float = 20.0
) -> TerrainLayer | None:
    """拉取并拼接视口内的地形栅格。

    返回 ``None`` 表示取不到数据（网络失败或该区域没有瓦片），
    调用方应当静默跳过海拔着色。
    """
    from app.geo import radius_bbox

    zoom = choose_zoom(lat, lon, radius_km)
    lat_min, lat_max, lon_min, lon_max = radius_bbox(lat, lon, radius_km, 0.2)

    # 视口覆盖的瓦片范围
    x0, y0 = _lonlat_to_tile(lon_min, lat_max, zoom)     # 左上
    x1, y1 = _lonlat_to_tile(lon_max, lat_min, zoom)     # 右下
    tx0, ty0 = int(math.floor(x0)), int(math.floor(y0))
    tx1, ty1 = int(math.floor(x1)), int(math.floor(y1))

    cols = tx1 - tx0 + 1
    rows = ty1 - ty0 + 1
    if cols <= 0 or rows <= 0 or cols > 8 or rows > 8:
        return None

    mosaic: np.ndarray | None = None
    for ty in range(ty0, ty1 + 1):
        row_tiles: list[np.ndarray] = []
        for tx in range(tx0, tx1 + 1):
            raw = _download_tile(zoom, tx, ty, timeout)
            if raw is None:
                return None
            decoded = _decode_terrarium(raw)
            if decoded is None:
                return None
            row_tiles.append(decoded)
        strip = np.concatenate(row_tiles, axis=1)
        mosaic = strip if mosaic is None else np.concatenate([mosaic, strip], axis=0)

    if mosaic is None:
        return None

    # 按瓦片边界推算整块拼接图的经纬度范围，再裁剪到视口
    mosaic_lon_min, mosaic_lat_max = _tile_to_lonlat(tx0, ty0, zoom)
    mosaic_lon_max, mosaic_lat_min = _tile_to_lonlat(tx1 + 1, ty1 + 1, zoom)

    height, width = mosaic.shape
    col_start = int((lon_min - mosaic_lon_min) / (mosaic_lon_max - mosaic_lon_min) * width)
    col_end = int((lon_max - mosaic_lon_min) / (mosaic_lon_max - mosaic_lon_min) * width) + 1
    row_start = int((mosaic_lat_max - lat_max) / (mosaic_lat_max - mosaic_lat_min) * height)
    row_end = int((mosaic_lat_max - lat_min) / (mosaic_lat_max - mosaic_lat_min) * height) + 1

    col_start = max(0, min(width - 1, col_start))
    row_start = max(0, min(height - 1, row_start))
    col_end = max(col_start + 1, min(width, col_end))
    row_end = max(row_start + 1, min(height, row_end))

    return TerrainLayer(
        elevations=mosaic[row_start:row_end, col_start:col_end],
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
        zoom=zoom,
    )


def elevation_color(elevation_m: float, alpha: float = 1.0) -> QColor:
    """把高程映射成色带上的颜色（低绿 → 高红）。"""
    from PySide6.QtGui import QColor

    stops = _ELEVATION_STOPS
    if elevation_m <= stops[0][0]:
        base = stops[0][1]
    elif elevation_m >= stops[-1][0]:
        base = stops[-1][1]
    else:
        base = stops[-1][1]
        for index in range(len(stops) - 1):
            low_alt, low_color = stops[index]
            high_alt, high_color = stops[index + 1]
            if low_alt <= elevation_m <= high_alt:
                ratio = (elevation_m - low_alt) / max(1e-9, high_alt - low_alt)
                base = (
                    int(low_color[0] + (high_color[0] - low_color[0]) * ratio),
                    int(low_color[1] + (high_color[1] - low_color[1]) * ratio),
                    int(low_color[2] + (high_color[2] - low_color[2]) * ratio),
                )
                break

    color = QColor(base[0], base[1], base[2])
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color
