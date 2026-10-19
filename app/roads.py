"""主要路网：从矢量瓦片里提取高速与主干道，绘制为蓝色线条。

数据来自 **OpenFreeMap**（免费、无需 Key 的矢量瓦片服务）。之所以不用
Overpass API，是因为实测三个公开镜像全部不可用（超时、504、证书错误），
而矢量瓦片稳定得多，且只传几何、不传样式，配成蓝色是自由的。

瓦片是 Mapbox Vector Tile（protobuf）格式，因此这里手写了一个精简的 MVT
解析器：只需要 ``Layer.name`` / ``Feature.tags`` / ``Feature.geometry``
三个字段，不必引入完整的 protobuf 依赖。

只在放大到一定程度（``zoom >= 10``）时才取路网——需求里也是这么要求的，
而且高层级瓦片覆盖范围小，一屏只需 1–4 个，下载量可控。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path

import requests

from app.config import PROJECT_ROOT

CACHE_DIR = PROJECT_ROOT / "assets" / "cache" / "roads"

_INDEX_URL = "https://tiles.openfreemap.org/planet"
_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

#: 层级范围。z8 的单个瓦片覆盖约 1.4°，50 km 视野正好落在 4 个瓦片内；
#: 层级太低（视野过大）时路网会糊成一团，此时不显示。
MIN_ZOOM = 8
_MAX_ZOOM = 14
_MAX_TILES_PER_SIDE = 2

#: 视为"主要道路"的等级（OpenMapTiles 规范的 class 取值）
MAJOR_ROAD_CLASSES = ("motorway", "trunk", "primary")

#: 可能承载道路的图层名
_ROAD_LAYER_HINTS = ("transportation", "road", "roads", "highway")

_version_cache: str | None = None


# ---------------------------------------------------------------------------
# 极简 MVT（protobuf）解析
# ---------------------------------------------------------------------------

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    """读一个 varint，返回 (值, 新位置)。"""
    result = 0
    shift = 0
    while True:
        if pos >= len(data):
            raise ValueError("varint 越界")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return result, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint 过长")


def _iter_fields(data: bytes):
    """遍历 protobuf 字段，产出 ``(字段号, wire_type, 值)``。"""
    pos = 0
    length = len(data)
    while pos < length:
        key, pos = _read_varint(data, pos)
        field_number = key >> 3
        wire_type = key & 0x07

        if wire_type == 0:                      # varint
            value, pos = _read_varint(data, pos)
        elif wire_type == 2:                    # length-delimited
            size, pos = _read_varint(data, pos)
            value = data[pos:pos + size]
            pos += size
        elif wire_type == 5:                    # 32-bit
            value = data[pos:pos + 4]
            pos += 4
        elif wire_type == 1:                    # 64-bit
            value = data[pos:pos + 8]
            pos += 8
        else:
            return
        yield field_number, wire_type, value


def _zigzag(value: int) -> int:
    return (value >> 1) ^ (-(value & 1))


def _decode_geometry(commands: list[int]) -> list[list[tuple[int, int]]]:
    """把 MVT 几何命令解成若干条折线的整数坐标。

    命令字编码为 ``(重复次数 << 3) | 命令号``；坐标是 zigzag 编码的**增量**，
    因此必须逐点累加。
    """
    lines: list[list[tuple[int, int]]] = []
    current: list[tuple[int, int]] = []
    x = y = 0
    pos = 0
    total = len(commands)

    while pos < total:
        command_int = commands[pos]
        pos += 1
        command = command_int & 0x07
        count = command_int >> 3

        if command == 1:                        # MoveTo
            for _ in range(count):
                if pos + 1 >= total:
                    return lines
                x += _zigzag(commands[pos]); pos += 1
                y += _zigzag(commands[pos]); pos += 1
                current = [(x, y)]
                lines.append(current)
        elif command == 2:                      # LineTo
            for _ in range(count):
                if pos + 1 >= total:
                    return lines
                x += _zigzag(commands[pos]); pos += 1
                y += _zigzag(commands[pos]); pos += 1
                if current:
                    current.append((x, y))
        elif command == 7:                      # ClosePath
            if current:
                current.append(current[0])

    return lines


@dataclass
class _Feature:
    tags: dict[str, str]
    lines: list[list[tuple[int, int]]]


@dataclass
class _Layer:
    name: str
    extent: int
    features: list[_Feature] = field(default_factory=list)


def _parse_layer(data: bytes) -> _Layer | None:
    name = ""
    extent = 4096
    keys: list[str] = []
    values: list[str] = []
    raw_features: list[bytes] = []

    for field_number, wire_type, value in _iter_fields(data):
        if field_number == 1 and wire_type == 2:
            name = value.decode("utf-8", errors="replace")
        elif field_number == 2 and wire_type == 2:
            raw_features.append(value)
        elif field_number == 3 and wire_type == 2:
            keys.append(value.decode("utf-8", errors="replace"))
        elif field_number == 4 and wire_type == 2:
            # Value 是嵌套消息，这里只需要字符串值
            text = ""
            for sub_field, sub_wire, sub_value in _iter_fields(value):
                if sub_field == 1 and sub_wire == 2:
                    text = sub_value.decode("utf-8", errors="replace")
                    break
            values.append(text)
        elif field_number == 5 and wire_type == 0:
            extent = int(value) or 4096

    if not name:
        return None

    layer = _Layer(name=name, extent=extent)

    for raw in raw_features:
        tag_indices: list[int] = []
        geometry: list[int] = []
        for field_number, wire_type, value in _iter_fields(raw):
            if field_number == 2 and wire_type == 2:
                pos = 0
                while pos < len(value):
                    number, pos = _read_varint(value, pos)
                    tag_indices.append(number)
            elif field_number == 4 and wire_type == 2:
                pos = 0
                while pos < len(value):
                    number, pos = _read_varint(value, pos)
                    geometry.append(number)

        if not geometry:
            continue

        tags: dict[str, str] = {}
        for index in range(0, len(tag_indices) - 1, 2):
            key_index = tag_indices[index]
            value_index = tag_indices[index + 1]
            if key_index < len(keys) and value_index < len(values):
                tags[keys[key_index]] = values[value_index]

        lines = _decode_geometry(geometry)
        if lines:
            layer.features.append(_Feature(tags=tags, lines=lines))

    return layer


def parse_tile(data: bytes) -> dict[str, _Layer]:
    """解析一个 MVT 瓦片，返回 ``图层名 → 图层``。"""
    layers: dict[str, _Layer] = {}
    try:
        for field_number, wire_type, value in _iter_fields(data):
            if field_number == 3 and wire_type == 2:
                layer = _parse_layer(value)
                if layer is not None:
                    layers[layer.name] = layer
    except (ValueError, IndexError):
        return layers
    return layers


# ---------------------------------------------------------------------------
# 瓦片下载
# ---------------------------------------------------------------------------

def _tilejson_version(timeout: float) -> str | None:
    """取当前瓦片数据集的版本路径。"""
    global _version_cache
    if _version_cache:
        return _version_cache
    try:
        response = requests.get(_INDEX_URL, timeout=timeout,
                                headers={"User-Agent": _USER_AGENT})
        if response.status_code != 200:
            return None
        payload = response.json()
        template = (payload.get("tiles") or [""])[0]
        # https://tiles.openfreemap.org/planet/<version>/{z}/{x}/{y}.pbf
        parts = template.split("/")
        if len(parts) >= 5:
            _version_cache = parts[4]
            return _version_cache
    except (requests.RequestException, ValueError, KeyError, IndexError):
        return None
    return None


def _lonlat_to_tile(lon: float, lat: float, zoom: int) -> tuple[float, float]:
    count = 2 ** zoom
    x = (lon + 180.0) / 360.0 * count
    lat_rad = math.radians(max(-85.0, min(85.0, lat)))
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * count
    return x, y


def _tile_point_to_lonlat(
    px: float, py: float, tile_x: int, tile_y: int, zoom: int, extent: int
) -> tuple[float, float]:
    """瓦片内整数坐标 → 经纬度。"""
    count = 2 ** zoom
    x = (tile_x + px / extent) / count
    y = (tile_y + py / extent) / count
    lon = x * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y))))
    return lon, lat


def _download_tile(version: str, zoom: int, x: int, y: int, timeout: float) -> bytes | None:
    path = CACHE_DIR / version / str(zoom) / str(x) / f"{y}.pbf"
    if path.exists() and path.stat().st_size > 40:
        try:
            return path.read_bytes()
        except OSError:
            pass

    url = f"https://tiles.openfreemap.org/planet/{version}/{zoom}/{x}/{y}.pbf"
    try:
        response = requests.get(url, timeout=timeout,
                                headers={"User-Agent": _USER_AGENT})
        if response.status_code != 200 or len(response.content) < 40:
            return None
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".part")
        temp.write_bytes(response.content)
        temp.replace(path)
        return response.content
    except (requests.RequestException, OSError):
        return None


@dataclass
class RoadLayer:
    """某视口的主要道路。"""

    lines: list[list[tuple[float, float]]]     #: 经纬度折线
    classes: list[str]
    zoom: int

    @property
    def is_empty(self) -> bool:
        return not self.lines

    def statistics(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for road_class in self.classes:
            counts[road_class] = counts.get(road_class, 0) + 1
        return counts


def choose_zoom(lat: float, lon: float, radius_km: float) -> int:
    """依据视口跨度选瓦片层级（控制在 2×2 个瓦片内）。

    如果连最低层级都覆盖不住（视野太大），返回 ``MIN_ZOOM - 1``
    表示"这种视野不该显示路网"，而不是硬拉一大堆瓦片。
    """
    from app.geo import EARTH_RADIUS_KM

    span_deg = math.degrees(radius_km * 2.0 * 1.2 / EARTH_RADIUS_KM)
    for zoom in range(_MAX_ZOOM, MIN_ZOOM - 1, -1):
        if span_deg / (360.0 / (2 ** zoom)) <= _MAX_TILES_PER_SIDE:
            return zoom
    return MIN_ZOOM - 1


#: 单帧最多绘制的道路折线数。瓦片里一个要素可能含多条折线，
#: 4 个瓦片全收会超过一万条，其中绝大多数在视口之外。
_MAX_LINES = 4000


def fetch_roads(
    lat: float,
    lon: float,
    radius_km: float,
    zoom: int | None = None,
    timeout: float = 25.0,
) -> RoadLayer | None:
    """拉取视口内的主要道路。

    视野过大（层级低于 ``MIN_ZOOM``）时直接返回空层——那时候路网会糊成一团，
    画出来只是噪声。
    """
    from app.geo import radius_bbox

    if zoom is None:
        zoom = choose_zoom(lat, lon, radius_km)
    if zoom < MIN_ZOOM:
        return RoadLayer(lines=[], classes=[], zoom=zoom)

    version = _tilejson_version(timeout)
    if version is None:
        return None

    lat_min, lat_max, lon_min, lon_max = radius_bbox(lat, lon, radius_km, 0.1)
    x0, y0 = _lonlat_to_tile(lon_min, lat_max, zoom)
    x1, y1 = _lonlat_to_tile(lon_max, lat_min, zoom)
    tx0, ty0 = int(math.floor(x0)), int(math.floor(y0))
    tx1, ty1 = int(math.floor(x1)), int(math.floor(y1))

    cols = tx1 - tx0 + 1
    rows = ty1 - ty0 + 1
    if cols <= 0 or rows <= 0 or cols > 8 or rows > 8:
        return None

    lines: list[list[tuple[float, float]]] = []
    classes: list[str] = []
    got_any = False

    for ty in range(ty0, ty1 + 1):
        for tx in range(tx0, tx1 + 1):
            raw = _download_tile(version, zoom, tx, ty, timeout)
            if raw is None:
                continue
            got_any = True
            layers = parse_tile(raw)
            for name, layer in layers.items():
                if not any(hint in name.lower() for hint in _ROAD_LAYER_HINTS):
                    continue
                for feature in layer.features:
                    road_class = (feature.tags.get("class")
                                  or feature.tags.get("highway")
                                  or "").strip()
                    if road_class not in MAJOR_ROAD_CLASSES:
                        continue
                    for line in feature.lines:
                        if len(line) < 2:
                            continue
                        converted = [
                            _tile_point_to_lonlat(px, py, tx, ty, zoom, layer.extent)
                            for px, py in line
                        ]
                        # 裁到视口内：一个瓦片覆盖的范围远大于雷达视野，
                        # 不过滤的话会把整片瓦片的道路都带进来（实测上万条）
                        if not _intersects_bbox(converted, lat_min, lat_max, lon_min, lon_max):
                            continue
                        lines.append(converted)
                        classes.append(road_class)
                        if len(lines) >= _MAX_LINES:
                            break
                    if len(lines) >= _MAX_LINES:
                        break
                if len(lines) >= _MAX_LINES:
                    break
            if len(lines) >= _MAX_LINES:
                break
        if len(lines) >= _MAX_LINES:
            break

    if not got_any:
        return None
    return RoadLayer(lines=lines, classes=classes, zoom=zoom)


def _intersects_bbox(
    line: list[tuple[float, float]],
    lat_min: float, lat_max: float, lon_min: float, lon_max: float,
) -> bool:
    """折线的经纬度包围盒是否与视口相交。"""
    lons = [point[0] for point in line]
    lats = [point[1] for point in line]
    return not (
        max(lats) < lat_min or min(lats) > lat_max
        or max(lons) < lon_min or min(lons) > lon_max
    )
