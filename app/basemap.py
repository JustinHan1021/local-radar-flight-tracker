"""矢量海岸线底图：下载、加载与投影缓存。

选用 Natural Earth **1:10m** 精度数据，而非 1:50m：后者在 50 km 视口下
会把香港一带的岛屿简化成几边形，一眼就能看出"多边形块"的痕迹。
1:10m 的陆地有约 44.6 万坐标点（1:50m 只有 6 万），精度提升 7.4 倍。

选矢量数据而非在线瓦片的原因：

1. **风格完全可控**——需求要求"深墨绿陆地 + 藏蓝海洋 + 浅灰白海岸线细线"，
   在线瓦片的配色由服务方决定，改不了；
2. **离线可用**——首次下载后本地缓存，之后无网络也能出图；
3. **严格对齐**——海岸线与航班图标走**同一个**极坐标等距投影，
   位置天然一致，不会像瓦片那样因缩放层级或投影差异出现错位。

两个图层用不同的裁剪策略
------------------------
1:10m 的陆地只有 11 个 feature，最大的一块（欧亚大陆）就有 27 万点。
如果每次视口变化都完整投影，需要约 165 ms，滚轮缩放时会明显卡顿。

* **陆地填充**用 `shapely` 做多边形裁剪（``intersection(box)``），
  裁完只剩几百到几千点，裁剪加投影合计 15–33 ms。
  裁剪必须保持**多边形闭合**——曾经试过"只投影视口内可见段再闭合"，
  描边没问题，但填充会塌掉：多边形被截断后不再是原来的区域，
  珠江口以西的陆地会被漏填成海洋色。
* **海岸线描边**用向量化的"可见连续段"提取。线不需要闭合，
  段提取正确且更快（3–11 ms），段两端各多取一个点伸到视口之外，
  避免在裁剪边界上出现生硬断头。

视口完全落在陆地**内部**时（例如在北京看 50 km），视口里一条海岸线都没有，
但整块盘都要填成陆地色——这种情况用 shapely 的 ``contains`` 单独判定。

解析结果缓存为 pickle：JSON 解析 10 MB 需要约 1 秒，而 pickle 只要几十毫秒。

**Qt 依赖一律延迟到函数内部导入。** Web 服务端跑在 Linux 上、不装 PySide6，
而数据的下载、解析与缓存跟 Qt 毫无关系；只有 `paint()` 这类绘制路径才真的需要它。
顶层 import 会让整个模块在服务端无法导入，把可复用的部分一起拖住。
"""

from __future__ import annotations

import json
import math
import pickle
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Iterable, Sequence

import numpy as np
import requests
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box, shape

from app.config import PROJECT_ROOT
from app.geo import Projector

if TYPE_CHECKING:  # 只在类型标注里出现；运行期由各函数自行延迟导入
    from PySide6.QtGui import QColor, QPainter, QPainterPath

    from app.theme import Theme

DATA_DIR = PROJECT_ROOT / "assets" / "data"
CACHE_DIR = PROJECT_ROOT / "assets" / "cache"

_BASE_URL = "https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson"

#: 图层 → 数据文件名。1:10m 精度。
DATA_FILES = {
    "land": "ne_10m_land.geojson",
    "coastline": "ne_10m_coastline.geojson",
}

_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

# pickle 结构版本。解析逻辑或数据精度变更时递增，以作废旧缓存。
# v5：陆地改用 shapely 几何对象 + 多边形裁剪
_PARSE_CACHE_VERSION = 5

# 下载内容小于此字节数视为失败（1:10m 文件约 10 MB）
_MIN_VALID_BYTES = 1_000_000

# 投影路径缓存上限
_PATH_CACHE_LIMIT = 8

# 包围盒外扩比例：留出余量，避免海岸线在雷达盘边缘被生硬切断
_BBOX_PADDING = 0.2


@dataclass
class _Ring:
    """一条海岸线折线。坐标以 numpy 数组保存，便于向量化做视口剔除。"""

    lons: np.ndarray
    lats: np.ndarray
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float

    @property
    def count(self) -> int:
        return int(self.lons.size)

    @staticmethod
    def build(coords: Sequence) -> "_Ring | None":
        points: list[tuple[float, float]] = []
        for pair in coords:
            if not isinstance(pair, (list, tuple)) or len(pair) < 2:
                continue
            try:
                lon = float(pair[0])
                lat = float(pair[1])
            except (TypeError, ValueError):
                continue
            points.append((lon, lat))

        if len(points) < 2:
            return None

        array = np.asarray(points, dtype=np.float64)
        lons = np.ascontiguousarray(array[:, 0])
        lats = np.ascontiguousarray(array[:, 1])
        return _Ring(
            lons=lons,
            lats=lats,
            lat_min=float(lats.min()),
            lat_max=float(lats.max()),
            lon_min=float(lons.min()),
            lon_max=float(lons.max()),
        )


def _empty_path() -> "QPainterPath":
    """构造一个空的绘制路径（Qt 延迟导入，见模块开头）。"""
    from PySide6.QtGui import QPainterPath

    return QPainterPath()


@dataclass
class BasemapPaths:
    """某一视图下投影完成的绘制路径。"""

    land: QPainterPath = field(default_factory=_empty_path)
    coastline: QPainterPath = field(default_factory=_empty_path)
    #: 视口完全落在陆地内部时为真——此时整块雷达盘都要填成陆地色
    land_fills_dish: bool = False

    @property
    def is_empty(self) -> bool:
        return (
            not self.land_fills_dish
            and self.land.isEmpty()
            and self.coastline.isEmpty()
        )


def _iter_coastline_rings(geometry: dict) -> Iterable[Sequence]:
    """产出 GeoJSON geometry 中的折线坐标序列。"""
    if not isinstance(geometry, dict):
        return
    kind = geometry.get("type")
    coords = geometry.get("coordinates")
    if kind == "LineString":
        yield coords or []
    elif kind == "MultiLineString":
        for line in coords or []:
            yield line


def _iter_polygons(geometry) -> Iterable[Polygon]:
    """把一个 shapely 几何展开成一个个 Polygon。

    ``intersection`` 的结果可能是 GeometryCollection（含退化成的线段与点），
    这里只取真正的多边形部分。
    """
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from _iter_polygons(part)


def _count_coords(geometry) -> int:
    """统计 shapely 几何包含的坐标点数（含内环），仅用于诊断。"""
    total = 0
    for polygon in _iter_polygons(geometry):
        total += len(polygon.exterior.coords)
        for interior in polygon.interiors:
            total += len(interior.coords)
    return total


def parse_land(path: Path) -> list:
    """解析陆地 GeoJSON，保留为 shapely 几何对象。

    **刻意不把 MultiPolygon 拆成一个个独立环**：外环与内环（洞）的归属关系
    一旦拆散就丢了，填充时洞会被当成实心陆地。
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    features = payload.get("features")
    if not isinstance(features, list):
        return []

    geometries = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        try:
            parsed = shape(geometry)
        except Exception:  # noqa: BLE001 - 个别 feature 畸形时跳过即可
            continue
        if parsed is not None and not parsed.is_empty:
            geometries.append(parsed)
    return geometries


def parse_coastline(path: Path) -> list[_Ring]:
    """解析海岸线 GeoJSON，返回折线列表。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []

    features = payload.get("features")
    if not isinstance(features, list):
        return []

    rings: list[_Ring] = []
    for feature in features:
        if not isinstance(feature, dict):
            continue
        for coords in _iter_coastline_rings(feature.get("geometry") or {}):
            ring = _Ring.build(coords)
            if ring is not None:
                rings.append(ring)
    return rings


def _viewport_runs(ring: _Ring, bounds: tuple[float, float, float, float]) -> list[tuple[int, int, bool]]:
    """找出折线上落在视口内的连续点段。

    返回 ``[(起始下标, 结束下标, 是否整条), ...]``。

    用 numpy 布尔运算一次筛完：海岸线有四十多万点，
    逐个 Python 比较会让每次缩放都卡上几十毫秒。
    """
    lat_min, lat_max, lon_min, lon_max = bounds
    inside = (
        (ring.lats >= lat_min)
        & (ring.lats <= lat_max)
        & (ring.lons >= lon_min)
        & (ring.lons <= lon_max)
    )

    if not inside.any():
        return []
    if inside.all():
        return [(0, ring.count - 1, True)]

    indices = np.flatnonzero(inside)
    gaps = np.flatnonzero(np.diff(indices) > 1)
    starts = np.concatenate(([0], gaps + 1))
    ends = np.concatenate((gaps, [len(indices) - 1]))
    return [
        (int(indices[start]), int(indices[end]), False)
        for start, end in zip(starts, ends)
    ]


class Basemap:
    """矢量底图的数据管理与投影绘制。"""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self._land: list = []              # shapely 多边形
        self._coastline: list[_Ring] = []  # 折线
        self._loaded = False
        self._path_cache: dict[tuple, BasemapPaths] = {}

    # ------------------------------------------------------------------
    # 数据准备
    # ------------------------------------------------------------------

    def data_ready(self) -> bool:
        """两个数据文件是否都已就位。"""
        return all((self.data_dir / name).exists() for name in DATA_FILES.values())

    def download_missing(self, timeout: float = 180.0) -> tuple[bool, str]:
        """下载缺失的数据文件。

        返回 ``(是否全部就绪, 说明文本)``。任何失败都不抛异常——
        底图只是方位参照，缺了它雷达盘依然能正确显示航班。
        """
        self.data_dir.mkdir(parents=True, exist_ok=True)
        failures: list[str] = []

        for key, name in DATA_FILES.items():
            target = self.data_dir / name
            if target.exists() and target.stat().st_size >= _MIN_VALID_BYTES:
                continue
            url = f"{_BASE_URL}/{name}"
            try:
                response = requests.get(
                    url, timeout=timeout, headers={"User-Agent": _USER_AGENT}
                )
                if response.status_code != 200:
                    failures.append(f"{key}(HTTP {response.status_code})")
                    continue
                if len(response.content) < _MIN_VALID_BYTES:
                    failures.append(f"{key}(内容不完整)")
                    continue
                temp = target.with_name(target.name + ".part")
                temp.write_bytes(response.content)
                temp.replace(target)
            except (requests.RequestException, OSError) as exc:
                failures.append(f"{key}({type(exc).__name__})")

        if failures:
            return False, "底图数据下载失败：" + "、".join(failures)
        return True, "底图数据就绪"

    def load(self) -> bool:
        """载入并解析数据（优先走 pickle 缓存）。返回是否可用。"""
        if self._loaded:
            return bool(self._land or self._coastline)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._land = self._load_layer("land", parse_land)
        self._coastline = self._load_layer("coastline", parse_coastline)
        self._loaded = True
        return bool(self._land or self._coastline)

    def _load_layer(self, key: str, parser):
        """载入单个图层，必要时解析 GeoJSON 并写入 pickle 缓存。"""
        source = self.data_dir / DATA_FILES[key]
        if not source.exists():
            return []

        cache = CACHE_DIR / f"{key}_v{_PARSE_CACHE_VERSION}.pickle"
        if cache.exists() and cache.stat().st_mtime >= source.stat().st_mtime:
            try:
                with cache.open("rb") as handle:
                    items = pickle.load(handle)
                if isinstance(items, list):
                    return items
            except (OSError, pickle.PickleError, EOFError, AttributeError, ModuleNotFoundError):
                pass  # 缓存损坏或结构过期，重新解析

        items = parser(source)
        try:
            with cache.open("wb") as handle:
                pickle.dump(items, handle, protocol=pickle.HIGHEST_PROTOCOL)
        except OSError:
            pass
        return items

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def ring_counts(self) -> tuple[int, int]:
        """返回 ``(陆地面数, 海岸线环数)``。"""
        return len(self._land), len(self._coastline)

    @property
    def point_counts(self) -> tuple[int, int]:
        """返回 ``(陆地坐标点数, 海岸线坐标点数)``，便于诊断数据精度。"""
        return (
            sum(_count_coords(geometry) for geometry in self._land),
            sum(ring.count for ring in self._coastline),
        )

    # ------------------------------------------------------------------
    # 投影
    # ------------------------------------------------------------------

    def paths(self, projector: Projector) -> BasemapPaths:
        """取得当前视图的绘制路径（带缓存）。

        缓存键包含圆心与半径：任一变化都会重新投影，
        但同一视图（例如每帧重绘）直接复用，不会重复计算。
        """
        key = (
            round(projector.center_lat, 6),
            round(projector.center_lon, 6),
            round(projector.radius_km, 3),
            round(projector.radius_px, 1),
        )
        cached = self._path_cache.get(key)
        if cached is not None:
            return cached

        land, fills_dish = self._project_land(projector)
        coastline = self._project_coastline(projector)
        paths = BasemapPaths(land=land, coastline=coastline, land_fills_dish=fills_dish)

        if len(self._path_cache) >= _PATH_CACHE_LIMIT:
            self._path_cache.clear()
        self._path_cache[key] = paths
        return paths

    def _project_land(self, projector: Projector) -> tuple[QPainterPath, bool]:
        """把陆地多边形按视口裁剪后投影。

        裁剪交给 shapely 做（``intersection(box)``）：它会保留多边形的
        闭合性与内环（洞），填充因此是正确的。直接在投影阶段截取
        "视口内可见的点段"是不行的——多边形一被截断就不再代表原来的区域，
        填充会塌成一条薄带，大片陆地会漏成海洋色。
        """
        path = _empty_path()
        if not self._land:
            return path, False

        lat_min, lat_max, lon_min, lon_max = projector.bbox(_BBOX_PADDING)
        clip = box(lon_min, lat_min, lon_max, lat_max)
        project = projector.project
        touched = False

        for geometry in self._land:
            # 先用包围盒快速排除，避免对每个大洲都做一次昂贵的 intersection
            gx_min, gy_min, gx_max, gy_max = geometry.bounds
            if gx_max < lon_min or gx_min > lon_max or gy_max < lat_min or gy_min > lat_max:
                continue

            clipped = geometry.intersection(clip)
            if clipped.is_empty:
                continue
            touched = True

            for polygon in _iter_polygons(clipped):
                first = True
                for lon, lat in polygon.exterior.coords:
                    x, y = project(lat, lon)
                    if first:
                        path.moveTo(x, y)
                        first = False
                    else:
                        path.lineTo(x, y)
                if not first:
                    path.closeSubpath()

        fills_dish = False
        if not touched:
            # 视口内没有任何陆地边界：可能整个落在陆地内部
            # （例如在北京看 50 km 范围），此时整块盘都要填成陆地色
            center = Point(projector.center_lon, projector.center_lat)
            for geometry in self._land:
                if geometry.contains(center):
                    fills_dish = True
                    break

        return path, fills_dish

    def _project_coastline(self, projector: Projector) -> QPainterPath:
        """把海岸线投影为路径。

        折线用"可见连续段"提取而非整体投影：海岸线有四十多万点，
        段提取只要几毫秒，而且线条不需要闭合，不存在填充塌陷的问题。
        """
        path = _empty_path()
        if not self._coastline:
            return path

        bounds = projector.bbox(_BBOX_PADDING)
        project = projector.project

        for ring in self._coastline:
            if (
                ring.lat_max < bounds[0]
                or ring.lat_min > bounds[1]
                or ring.lon_max < bounds[2]
                or ring.lon_min > bounds[3]
            ):
                continue

            for start, end, whole in _viewport_runs(ring, bounds):
                if whole:
                    first_index, last_index = 0, ring.count - 1
                else:
                    # 两端各多取一个点，让线条伸到视口之外——
                    # 否则海岸线会在裁剪边界上出现生硬的断头
                    first_index = max(0, start - 1)
                    last_index = min(ring.count - 1, end + 1)

                first = True
                for index in range(first_index, last_index + 1):
                    x, y = project(float(ring.lats[index]), float(ring.lons[index]))
                    if first:
                        path.moveTo(x, y)
                        first = False
                    else:
                        path.lineTo(x, y)

        return path

    # ------------------------------------------------------------------
    # 绘制
    # ------------------------------------------------------------------

    def paint(
        self,
        painter: QPainter,
        projector: Projector,
        theme: Theme,
        dish_radius_px: float,
    ) -> None:
        """绘制底图：先铺海洋底色，再填陆地，最后描海岸线。

        所有绘制都被裁剪到雷达盘圆形内——陆地多边形的范围远超雷达盘，
        不裁剪的话会溢到界面的四个角落。底图整体做低透明度淡化处理，
        仅作方位参照，不干扰航班与扫描线。
        """
        from PySide6.QtCore import QPointF, Qt
        from PySide6.QtGui import QPainter, QPainterPath, QPen

        from app.theme import COASTLINE, LAND, SEA

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        # 圆形裁剪区：底图与刻度圈都只应出现在盘内
        dish = QPainterPath()
        dish.addEllipse(
            projector.center_x - dish_radius_px,
            projector.center_y - dish_radius_px,
            dish_radius_px * 2.0,
            dish_radius_px * 2.0,
        )
        painter.setClipPath(dish)

        # 海洋底色铺满整个雷达盘
        painter.fillPath(dish, _color(SEA, 0.55))

        paths = self.paths(projector)
        if paths.is_empty:
            painter.restore()
            return

        # 陆地填充
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_color(LAND, 0.75))
        if paths.land_fills_dish:
            painter.drawEllipse(QPointF(projector.center_x, projector.center_y),
                                dish_radius_px, dish_radius_px)
        else:
            painter.drawPath(paths.land)

        # 海岸线：细线勾勒。setCosmetic 让线宽不随变换缩放，
        # 这样缩放雷达半径时线条粗细保持一致，观感更精致。
        pen = QPen(_color(COASTLINE, 0.45))
        pen.setWidthF(0.8)
        pen.setCosmetic(True)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(paths.coastline)

        painter.restore()

    def invalidate(self) -> None:
        """清空投影缓存（例如数据文件被替换后调用）。"""
        self._path_cache.clear()


def _color(hex_color: str, alpha: float) -> QColor:
    """由十六进制色与透明度构造 QColor。"""
    from PySide6.QtGui import QColor

    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color
