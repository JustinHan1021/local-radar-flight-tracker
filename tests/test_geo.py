"""geo 模块单元测试：验证投影、距离、方位角的数学正确性。

自检方式：``python tests/test_geo.py``

测试基准取自可独立验算的已知量：
  * 赤道上 1° 经度 ≈ 111.32 km，1° 纬度 ≈ 111.19 km
  * 香港国际机场 (22.3089, 113.9145) 与用户实测位置 (22.3031, 114.1788)
    相距约 27.2 km、方位约 89.6°（近乎正东），可手工复算。
"""

from __future__ import annotations

import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.geo import (  # noqa: E402
    EARTH_RADIUS_KM,
    Projector,
    angle_delta,
    compass_name,
    destination_point,
    haversine_km,
    initial_bearing,
    normalize_degrees,
    ring_step_km,
    ring_values_km,
)

# 香港国际机场 VHHH 与用户实测位置（香港九龙）
VHHH = (22.3089, 113.9145)
USER = (22.3031, 114.1788)


class TestDistance(unittest.TestCase):
    def test_one_degree_latitude(self):
        """正北 1° 纬度 = π/180 × 地球平均半径 ≈ 111.195 km。"""
        expected = math.pi / 180.0 * EARTH_RADIUS_KM
        self.assertAlmostEqual(haversine_km(0, 0, 1, 0), expected, places=6)

    def test_one_degree_longitude_at_equator(self):
        """赤道上 1° 经度与 1° 纬度等长——同为 1° 大圆弧长。"""
        expected = math.pi / 180.0 * EARTH_RADIUS_KM
        self.assertAlmostEqual(haversine_km(0, 0, 0, 1), expected, places=6)

    def test_longitude_shrinks_with_latitude(self):
        """高纬度处同样 1° 经度的实际距离应按 cos(lat) 收缩。"""
        one_degree = math.pi / 180.0 * EARTH_RADIUS_KM
        expected = one_degree * math.cos(math.radians(60.0))
        self.assertAlmostEqual(haversine_km(60, 0, 60, 1), expected, delta=0.05)

    def test_airport_to_user_distance(self):
        """机场到用户位置约 27.2 km（手工复算可核）。"""
        self.assertAlmostEqual(haversine_km(*VHHH, *USER), 27.2, delta=0.3)

    def test_zero_distance(self):
        self.assertEqual(haversine_km(22.3, 114.1, 22.3, 114.1), 0.0)

    def test_symmetry(self):
        """距离必须对称：A→B 等于 B→A。"""
        self.assertAlmostEqual(
            haversine_km(*VHHH, *USER), haversine_km(*USER, *VHHH), places=9
        )


class TestBearing(unittest.TestCase):
    def test_cardinal_directions(self):
        self.assertAlmostEqual(initial_bearing(0, 0, 1, 0), 0.0, places=6)      # 正北
        self.assertAlmostEqual(initial_bearing(0, 0, 0, 1), 90.0, places=6)     # 正东
        self.assertAlmostEqual(initial_bearing(0, 0, -1, 0), 180.0, places=6)   # 正南
        self.assertAlmostEqual(initial_bearing(0, 0, 0, -1), 270.0, places=6)   # 正西

    def test_user_to_airport_is_west_northwest(self):
        """用户位置看机场：机场纬度略高（更靠北），故方位为西偏北，约 271.4°。"""
        self.assertAlmostEqual(initial_bearing(*USER, *VHHH), 271.4, delta=0.1)

    def test_airport_to_user_is_east_southeast(self):
        """反向应为东偏南（约 91.4°）。"""
        self.assertAlmostEqual(initial_bearing(*VHHH, *USER), 91.4, delta=0.1)

    def test_forward_and_reverse_bearings_are_nearly_opposite(self):
        """往返方位角接近、但不必精确相差 180°。

        球面上"A 看 B"与"B 看 A"的方位角只有在同一经线或赤道上才严格互补；
        其余情形因经线会聚而有微小偏差，本例约 0.1°。这是球面几何性质，
        不是计算缺陷，因此这里用 0.5° 容差。
        """
        forward = initial_bearing(*VHHH, *USER)
        backward = initial_bearing(*USER, *VHHH)
        self.assertAlmostEqual(abs(angle_delta(forward, backward)), 180.0, delta=0.5)

    def test_normalize(self):
        self.assertAlmostEqual(normalize_degrees(-90.0), 270.0)
        self.assertAlmostEqual(normalize_degrees(450.0), 90.0)


class TestAngleDelta(unittest.TestCase):
    def test_wraparound(self):
        """跨越 0°/360° 边界时夹角应取最小值。"""
        self.assertAlmostEqual(angle_delta(5.0, 355.0), 10.0)
        self.assertAlmostEqual(angle_delta(355.0, 5.0), -10.0)

    def test_range(self):
        for a, b in ((0, 180), (0, 181), (90, 270), (359, 1), (180, 0)):
            self.assertLessEqual(abs(angle_delta(a, b)), 180.0)


class TestDestinationPoint(unittest.TestCase):
    def test_north_and_east(self):
        lat, lon = destination_point(0, 0, 0, 111.19)
        self.assertAlmostEqual(lat, 1.0, delta=0.01)
        self.assertAlmostEqual(lon, 0.0, delta=0.01)

        lat, lon = destination_point(0, 0, 90, 111.32)
        self.assertAlmostEqual(lat, 0.0, delta=0.01)
        self.assertAlmostEqual(lon, 1.0, delta=0.01)

    def test_destination_lands_at_exact_distance(self):
        """沿任意方位前进指定距离后，与出发点的实测大圆距离必须精确等于该距离。

        这里刻意不验证"前进再原路返回能回到原点"——球面上大圆在终点的切线方向
        已经改变，反方位角返回本就会有微小偏差，那是球面几何性质而非缺陷。
        """
        for bearing in (0, 45, 90, 137, 180, 270, 359):
            point = destination_point(*USER, float(bearing), 30.0)
            self.assertAlmostEqual(
                haversine_km(USER[0], USER[1], point.lat, point.lon),
                30.0,
                places=6,
                msg=f"方位 {bearing}° 的落点距离不准确",
            )


class TestProjector(unittest.TestCase):
    """投影齿轮的核心保证：距离等比、方位严格相等。"""

    def setUp(self):
        self.cx, self.cy, self.rpx = 400.0, 350.0, 300.0
        self.proj = Projector(*USER, 50.0, self.cx, self.cy, self.rpx)

    def test_px_per_km(self):
        self.assertAlmostEqual(self.proj.px_per_km, 6.0)

    def test_center_projects_to_center(self):
        x, y = self.proj.project(*USER)
        self.assertAlmostEqual(x, self.cx, places=6)
        self.assertAlmostEqual(y, self.cy, places=6)

    def test_north_is_up(self):
        """正北 50 km 处的点应落在圆心正上方。"""
        target = destination_point(*USER, 0.0, 50.0)
        x, y = self.proj.project(target.lat, target.lon)
        self.assertAlmostEqual(x, self.cx, delta=1e-6)
        self.assertAlmostEqual(y, self.cy - self.rpx, delta=1e-6)

    def test_east_is_right(self):
        """正东 50 km 处的点应落在圆心正右方（左西右东）。"""
        target = destination_point(*USER, 90.0, 50.0)
        x, y = self.proj.project(target.lat, target.lon)
        self.assertAlmostEqual(x, self.cx + self.rpx, delta=1e-6)
        self.assertAlmostEqual(y, self.cy, delta=1e-6)

    def test_west_is_left(self):
        target = destination_point(*USER, 270.0, 50.0)
        x, y = self.proj.project(target.lat, target.lon)
        self.assertAlmostEqual(x, self.cx - self.rpx, delta=1e-6)
        self.assertAlmostEqual(y, self.cy, delta=1e-6)

    def test_pixel_distance_proportional_to_real_distance(self):
        """核心保证之一：像素距离与真实大圆距离严格成正比。"""
        for lat, lon in ((22.6, 114.0), (22.1, 114.5), (22.35, 113.85), (21.95, 114.3)):
            distance, _ = self.proj.distance_bearing(lat, lon)
            x, y = self.proj.project(lat, lon)
            pixel_distance = math.hypot(x - self.cx, y - self.cy)
            self.assertAlmostEqual(
                pixel_distance,
                distance * self.proj.px_per_km,
                delta=1e-6,
                msg=f"({lat},{lon}) 像素距离与真实距离不成比例",
            )

    def test_pixel_bearing_equals_real_bearing(self):
        """核心保证之二：像素方位角与真实初始方位角严格相等。"""
        for lat, lon in ((22.6, 114.0), (22.1, 114.5), (22.35, 113.85), (21.95, 114.3)):
            _, bearing = self.proj.distance_bearing(lat, lon)
            x, y = self.proj.project(lat, lon)
            pixel_bearing = normalize_degrees(
                math.degrees(math.atan2(x - self.cx, self.cy - y))
            )
            self.assertAlmostEqual(
                pixel_bearing, bearing, delta=1e-6,
                msg=f"({lat},{lon}) 像素方位角与真实方位角不一致",
            )

    def test_is_inside(self):
        inside = destination_point(*USER, 45.0, 49.0)
        outside = destination_point(*USER, 45.0, 51.0)
        self.assertTrue(self.proj.is_inside(inside.lat, inside.lon))
        self.assertFalse(self.proj.is_inside(outside.lat, outside.lon))
        # 放宽容差后外部点也应被纳入
        self.assertTrue(self.proj.is_inside(outside.lat, outside.lon, margin_km=2.0))

    def test_unproject_inverts_project(self):
        """投影与反投影必须互为逆运算，往返误差可忽略。"""
        for lat, lon in ((22.6, 114.0), (22.1, 114.5), (22.35, 113.85), (21.95, 114.3)):
            x, y = self.proj.project(lat, lon)
            back = self.proj.unproject(x, y)
            self.assertAlmostEqual(back.lat, lat, places=8)
            self.assertAlmostEqual(back.lon, lon, places=8)

    def test_unproject_center(self):
        point = self.proj.unproject(self.cx, self.cy)
        self.assertAlmostEqual(point.lat, USER[0], places=8)
        self.assertAlmostEqual(point.lon, USER[1], places=8)

    def test_bbox_contains_radius(self):
        """雷达盘边界盒必须真正包住整个半径圆。"""
        lat_min, lat_max, lon_min, lon_max = self.proj.bbox()
        for bearing in range(0, 360, 15):
            edge = destination_point(*USER, float(bearing), 50.0)
            self.assertGreaterEqual(edge.lat, lat_min - 1e-6)
            self.assertLessEqual(edge.lat, lat_max + 1e-6)
            self.assertGreaterEqual(edge.lon, lon_min - 1e-6)
            self.assertLessEqual(edge.lon, lon_max + 1e-6)

    def test_bbox_padding_expands(self):
        tight = self.proj.bbox()
        padded = self.proj.bbox(pad_ratio=0.2)
        self.assertLess(padded[0], tight[0])
        self.assertGreater(padded[1], tight[1])

    def test_rejects_invalid_radius(self):
        with self.assertRaises(ValueError):
            Projector(*USER, 0.0, self.cx, self.cy, self.rpx)
        with self.assertRaises(ValueError):
            Projector(*USER, 50.0, self.cx, self.cy, 0.0)

    def test_reconfigured_keeps_center(self):
        other = self.proj.reconfigured(radius_km=100.0)
        self.assertAlmostEqual(other.center_lat, USER[0])
        self.assertAlmostEqual(other.radius_km, 100.0)
        self.assertAlmostEqual(other.px_per_km, self.rpx / 100.0)


class TestRingScales(unittest.TestCase):
    def test_ring_step_produces_sane_counts(self):
        """任何半径下同心圈数都应落在 4~6 圈，避免过密或过疏。"""
        for radius in (5, 10, 25, 50, 100, 200, 300, 500, 1000, 2000):
            step = ring_step_km(float(radius))
            count = radius / step
            self.assertGreaterEqual(count, 4.0, f"半径 {radius} km 圈数过少")
            self.assertLessEqual(count, 6.0, f"半径 {radius} km 圈数过多")

    def test_ring_step_known_values(self):
        self.assertEqual(ring_step_km(50.0), 10.0)
        self.assertEqual(ring_step_km(100.0), 20.0)
        self.assertEqual(ring_step_km(200.0), 50.0)
        self.assertEqual(ring_step_km(25.0), 5.0)

    def test_ring_values_inside_radius(self):
        values = ring_values_km(50.0)
        self.assertEqual(values, [10.0, 20.0, 30.0, 40.0])
        for value in values:
            self.assertLess(value, 50.0)


class TestCompass(unittest.TestCase):
    def test_cardinals(self):
        self.assertEqual(compass_name(0.0), "N")
        self.assertEqual(compass_name(90.0), "E")
        self.assertEqual(compass_name(180.0), "S")
        self.assertEqual(compass_name(270.0), "W")

    def test_intercardinals(self):
        self.assertEqual(compass_name(45.0), "NE")
        self.assertEqual(compass_name(225.0), "SW")
        self.assertEqual(compass_name(359.0), "N")


class TestEarthRadius(unittest.TestCase):
    def test_radius_value(self):
        self.assertAlmostEqual(EARTH_RADIUS_KM, 6371.0088, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
