"""地理计算与雷达投影引擎。

本模块是"地理关系严格正确"的技术保证所在。

核心约定：屏幕上任意点到雷达圆心的**像素距离与真实大圆距离成正比**，
**像素方位角与真实方位角严格相等**。因此距离刻度圈与方位刻度是数学上
成立的，而不是视觉近似；底图海岸线与航班图标走同一变换，二者位置必然对齐。

实现方式是以用户位置为中心的方位等距投影（Azimuthal Equidistant），
以极坐标形式表达：先算出目标点相对圆心的大圆距离与初始方位角，
再按 ``像素/km`` 比例映射到屏幕。刻意不做经纬度线性映射——那种做法
在偏离赤道的位置会同时扭曲距离与方位。

本模块为纯 Python 实现，不依赖 Qt，可独立单元测试。
"""

from __future__ import annotations

import math
from typing import NamedTuple

# WGS-84 椭球平均半径。采用球面近似，在雷达尺度（数千公里内）误差可忽略。
EARTH_RADIUS_KM = 6371.0088

# 生成同心刻度圈时优先选用的步进值，保证刻度标签是"整数好看"的
_RING_STEP_CANDIDATES = (1, 2, 5, 10, 20, 25, 50, 100, 200, 250, 500, 1000, 2000, 5000)

# 16 方位罗盘名，用于详情页展示相对方位
_COMPASS_POINTS = (
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
)


class LatLon(NamedTuple):
    """经纬度坐标对，单位为十进制度。"""

    lat: float
    lon: float


def _to_rad(deg: float) -> float:
    return math.radians(deg)


def _to_deg(rad: float) -> float:
    return math.degrees(rad)


def normalize_degrees(value: float) -> float:
    """将角度归一化到 ``[0, 360)``。"""
    return value % 360.0


def angle_delta(a: float, b: float) -> float:
    """返回 ``a`` 相对 ``b`` 的最小有符号夹角，范围 ``(-180, 180]``。

    用于判断旋转中的扫描线是否刚好掠过某个方位角。
    """
    return (a - b + 180.0) % 360.0 - 180.0


def compass_name(bearing: float) -> str:
    """把方位角转换为 16 方位罗盘名，如 ``270.5`` → ``"W"``。"""
    index = int((normalize_degrees(bearing) + 11.25) % 360.0 // 22.5)
    return _COMPASS_POINTS[index]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """两点间的大圆距离，单位 km（Haversine 公式）。"""
    phi1, phi2 = _to_rad(lat1), _to_rad(lat2)
    dphi = phi2 - phi1
    dlam = _to_rad(lon2 - lon1)
    h = math.sin(dphi * 0.5) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam * 0.5) ** 2
    return 2.0 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def initial_bearing(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """从点 1 指向点 2 的初始方位角。

    约定 ``0°`` 为正北，顺时针递增，故 ``90°`` 为正东，``270°`` 为正西。
    """
    phi1, phi2 = _to_rad(lat1), _to_rad(lat2)
    dlam = _to_rad(lon2 - lon1)
    y = math.sin(dlam) * math.cos(phi2)
    x = math.cos(phi1) * math.sin(phi2) - math.sin(phi1) * math.cos(phi2) * math.cos(dlam)
    return normalize_degrees(_to_deg(math.atan2(y, x)))


def destination_point(lat: float, lon: float, bearing_deg: float, distance_km: float) -> LatLon:
    """从给定点沿指定方位角前进指定距离后的大圆终点坐标。

    这是 :meth:`Projector.unproject` 的基础，也用于计算雷达盘的经纬度边界。
    """
    angular = distance_km / EARTH_RADIUS_KM
    phi1, lam1, theta = _to_rad(lat), _to_rad(lon), _to_rad(bearing_deg)

    sin_phi1, cos_phi1 = math.sin(phi1), math.cos(phi1)
    sin_a, cos_a = math.sin(angular), math.cos(angular)

    sin_phi2 = sin_phi1 * cos_a + cos_phi1 * sin_a * math.cos(theta)
    phi2 = math.asin(max(-1.0, min(1.0, sin_phi2)))

    lam2 = lam1 + math.atan2(
        math.sin(theta) * sin_a * cos_phi1,
        cos_a - sin_phi1 * sin_phi2,
    )
    return LatLon(_to_deg(phi2), normalize_degrees(_to_deg(lam2)))


def ring_step_km(radius_km: float, min_rings: int = 4, max_rings: int = 6) -> float:
    """为给定雷达半径挑选同心圈步进，使圈数落在 ``[min_rings, max_rings]``。

    例如半径 50 km 返回 10（5 圈），半径 200 km 返回 50（4 圈）。
    """
    if radius_km <= 0:
        return 1.0
    for step in _RING_STEP_CANDIDATES:
        if min_rings <= radius_km / step <= max_rings:
            return float(step)
    return float(min(_RING_STEP_CANDIDATES, key=lambda s: abs(radius_km / s - 5.0)))


def ring_values_km(radius_km: float, min_rings: int = 4, max_rings: int = 6) -> list[float]:
    """返回雷达盘内所有同心刻度圈的半径值（km），不含最外圈边界。"""
    step = ring_step_km(radius_km, min_rings, max_rings)
    values: list[float] = []
    value = step
    while value < radius_km - step * 0.35:
        values.append(value)
        value += step
    return values


def radius_bbox(
    lat: float, lon: float, radius_km: float, pad_ratio: float = 0.0
) -> tuple[float, float, float, float]:
    """以 ``(lat, lon)`` 为圆心、``radius_km`` 为半径的圆的经纬度包围盒。

    返回 ``(lat_min, lat_max, lon_min, lon_max)``，供数据源构造查询范围
    与底图粗裁剪使用。``pad_ratio`` 可按比例外扩，避免边缘被裁掉。

    经度半跨度用精确解 ``atan(tan δ / cos φc)``——注意用的是**圆心纬度**，
    而非纬度边界：后者会低估跨度，导致圆的东/西边缘点被切掉。
    """
    effective_radius = radius_km * (1.0 + max(0.0, pad_ratio))
    dlat_rad = effective_radius / EARTH_RADIUS_KM
    dlat = _to_deg(dlat_rad)

    lat_min = max(-90.0, lat - dlat)
    lat_max = min(90.0, lat + dlat)

    if abs(lat) + dlat >= 90.0:
        # 半径圆覆盖到极点，经度方向铺满整个半球
        dlon = 180.0
    else:
        dlon = _to_deg(math.atan(math.tan(dlat_rad) / math.cos(_to_rad(lat))))

    return lat_min, lat_max, lon - dlon, lon + dlon


class Projector:
    """把经纬度映射到圆形雷达盘内的像素坐标。

    圆心固定为用户位置，半径由 ``radius_km`` 与 ``radius_px`` 共同定义，
    ``px_per_km`` 即两者之比——这正是"距离等比"的体现。
    """

    __slots__ = (
        "center_lat", "center_lon", "radius_km",
        "center_x", "center_y", "radius_px", "px_per_km",
    )

    def __init__(
        self,
        center_lat: float,
        center_lon: float,
        radius_km: float,
        center_x: float,
        center_y: float,
        radius_px: float,
    ) -> None:
        if radius_km <= 0:
            raise ValueError("radius_km 必须为正数")
        if radius_px <= 0:
            raise ValueError("radius_px 必须为正数")

        self.center_lat = float(center_lat)
        self.center_lon = float(center_lon)
        self.radius_km = float(radius_km)
        self.center_x = float(center_x)
        self.center_y = float(center_y)
        self.radius_px = float(radius_px)
        self.px_per_km = self.radius_px / self.radius_km

    # ---------- 基础量 ----------

    def distance_bearing(self, lat: float, lon: float) -> tuple[float, float]:
        """返回目标点相对圆心的 ``(大圆距离 km, 方位角 度)``。"""
        return (
            haversine_km(self.center_lat, self.center_lon, lat, lon),
            initial_bearing(self.center_lat, self.center_lon, lat, lon),
        )

    def is_inside(self, lat: float, lon: float, margin_km: float = 0.0) -> bool:
        """判断目标点是否落在雷达半径内（可放宽 ``margin_km``）。"""
        return haversine_km(self.center_lat, self.center_lon, lat, lon) <= self.radius_km + margin_km

    # ---------- 投影 ----------

    def project(self, lat: float, lon: float) -> tuple[float, float]:
        """经纬度 → 雷达盘像素坐标。

        ``x`` 向东为正，``y`` 向北为正（屏幕 y 轴向下，故取负），
        严格保证上北下南、左西右东。
        """
        distance, bearing = self.distance_bearing(lat, lon)
        theta = _to_rad(bearing)
        scale = distance * self.px_per_km
        return (
            self.center_x + scale * math.sin(theta),
            self.center_y - scale * math.cos(theta),
        )

    def unproject(self, x: float, y: float) -> LatLon:
        """雷达盘像素坐标 → 经纬度（:meth:`project` 的逆运算）。"""
        dx = x - self.center_x
        dy = self.center_y - y
        distance_km = math.hypot(dx, dy) / self.px_per_km
        bearing = normalize_degrees(_to_deg(math.atan2(dx, dy)))
        return destination_point(self.center_lat, self.center_lon, bearing, distance_km)

    # ---------- 视口辅助 ----------

    def bbox(self, pad_ratio: float = 0.0) -> tuple[float, float, float, float]:
        """返回雷达盘外接矩形的经纬度边界 ``(lat_min, lat_max, lon_min, lon_max)``。

        用于底图坐标的粗裁剪——先把落在雷达盘之外的矢量点丢掉，
        再做昂贵的三角函数投影。
        """
        return radius_bbox(self.center_lat, self.center_lon, self.radius_km, pad_ratio)

    def px_radius_for(self, distance_km: float) -> float:
        """给定真实距离返回对应的像素半径，用于绘制刻度圈。"""
        return distance_km * self.px_per_km

    def reconfigured(
        self,
        radius_km: float | None = None,
        center_x: float | None = None,
        center_y: float | None = None,
        radius_px: float | None = None,
    ) -> "Projector":
        """基于当前投影仪生成一个新实例，用于窗口缩放或半径变更。"""
        return Projector(
            self.center_lat,
            self.center_lon,
            self.radius_km if radius_km is None else radius_km,
            self.center_x if center_x is None else center_x,
            self.center_y if center_y is None else center_y,
            self.radius_px if radius_px is None else radius_px,
        )
