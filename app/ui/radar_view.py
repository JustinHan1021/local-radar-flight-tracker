"""雷达主视图。

绘制分层（从下到上）：

1. **静态层**——底图 + 等距刻度圈 + 方位刻度 + 圆心站位，缓存为 ``QPixmap``。
   这些内容只在尺寸 / 半径 / 圆心变化时改变，每帧重绘纯属浪费。
2. **扫描层**——锥形渐变余晖 + 高亮前沿线，6 秒匀速一圈。
3. **航班层**——按真实航向旋转的矢量飞机图标，宽体机用琥珀黄区分，
   被扫描线掠过的航班短暂发光，模拟真实雷达回波。
4. **标注层**——航班号与海拔标签、左上日期 / 右上时间角标、底部状态行。

地理正确性由 :class:`app.geo.Projector` 保证：任意点到圆心的像素距离与真实
大圆距离成正比，像素方位角与真实方位角严格相等（见 ``tests/test_geo.py``）。
因此距离刻度圈与方位刻度是数学上成立的，而非视觉近似。

本控件严格上北下南、左西右东：正北永远是屏幕上方，正东永远是屏幕右方。
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QConicalGradient,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import QWidget

from app.airports import Airport
from app.basemap import Basemap
from app.config import Config
from app.data.models import Flight
from app.data.registry import aircraft_kind
from app.geo import Projector, ring_values_km
from app.places import Place
from app.roads import RoadLayer
from app.terrain import TerrainLayer
from app.theme import Theme, make_font
from app.ui import radar_overlays as overlays
from app.weather import CloudField, Weather

#: 扫描一圈的时长（毫秒）
SWEEP_PERIOD_MS = 6000.0

#: 余晖拖尾的角宽（度）：从扫描前沿向已扫过的方向渐隐
SWEEP_TAIL_DEG = 72.0

#: 被扫过后的回波高亮持续时长（秒）
ECHO_DECAY_SEC = 1.6

#: 帧间隔（毫秒）。30 fps 足够顺滑，同时把 CPU 占用压得很低。
FRAME_INTERVAL_MS = 33

#: 雷达盘四周留白。这个值同时要容纳三样东西：
#: 角标（约 30px）、方位刻度线与标签（约 24 + 14px）、底部状态两行（约 34px），
#: 因此不能取太小，否则正南方向的方位标签会被状态行压住。
DISH_MARGIN = 80

#: 滚轮缩放档位（km）。按档位跳变而非连续缩放：半径值更整齐，
#: 静态层与投影路径的缓存命中率也更高。
RADIUS_STOPS = (
    2.0, 5.0, 10.0, 15.0, 20.0, 25.0, 30.0, 50.0, 75.0,
    100.0, 150.0, 200.0, 250.0, 300.0, 400.0, 460.0,
)

#: 点击命中半径（像素）
HIT_RADIUS_PX = 22.0

#: 0/90/180/270 用方位字母标注，其余每 30° 标度数
_CARDINAL = {0: "N", 90: "E", 180: "S", 270: "W"}

_WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")


def _qc(hex_color: str, alpha: float) -> QColor:
    """由十六进制色与透明度构造 QColor。雷达盘大量使用半透明叠色。"""
    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


def _blend(a: QColor, b: QColor, t: float) -> QColor:
    """按比例混合两个颜色，用于回波高亮染色。"""
    t = max(0.0, min(1.0, t))
    return QColor(
        int(a.red() + (b.red() - a.red()) * t),
        int(a.green() + (b.green() - a.green()) * t),
        int(a.blue() + (b.blue() - a.blue()) * t),
    )


def _ease(t: float) -> float:
    """smoothstep 缓动，消除插值起止处的突跳感。"""
    t = max(0.0, min(1.0, t))
    return t * t * (3.0 - 2.0 * t)


def _angle_in_sweep(bearing: float, start: float, end: float) -> bool:
    """判断方位角是否落在从 ``start`` 顺时针扫到 ``end`` 的区间内。"""
    if start == end:
        return False
    if start < end:
        return start <= bearing <= end
    # 区间跨越 0°（正北）
    return bearing >= start or bearing <= end


@dataclass(frozen=True)
class _PlaneShape:
    """一类机型的俯视轮廓比例（全部相对图标半长 ``size``）。"""

    body: float        #: 机身半宽
    span: float        #: 翼展（半展）
    sweep: float       #: 机翼后掠量（翼尖前缘相对翼根前缘的后移）
    tail_span: float   #: 水平尾翼半展
    wing_pos: float    #: 机翼前缘距机头的距离
    root_chord: float  #: 翼根弦长
    tip_chord: float   #: 翼尖弦长
    tail_len: float    #: 机尾距中心的距离（机身总长为 ``size * (1 + tail_len)``）


#: 各类别的机身比例。
#:
#: 这套数值是按**真实客机的俯视比例**换算的，而不是凭感觉调的。
#: 以 A320 为例：机身长 37.6 m、翼展 35.8 m、翼根弦长 6 m、翼尖弦长 1.5 m、
#: 机翼前缘距机头 12 m（占机身长 32%）、后掠角约 25°。
#:
#: 早先的版本把机翼放在距机头 19% 的位置，机翼过于靠前、后掠又过陡，
#: 整体轮廓看起来像飞蛾而不是飞机——机翼位置是最关键的参数。
#:
#: 涡桨的后掠量只有 0.10，画出近乎平直的机翼，与喷气机一眼区分。
_PLANE_SHAPES: dict[str, _PlaneShape] = {
    "narrow": _PlaneShape(0.100, 0.95, 0.52, 0.30, 0.60, 0.30, 0.09, 0.84),
    "wide": _PlaneShape(0.135, 1.00, 0.50, 0.34, 0.60, 0.31, 0.10, 0.86),
    "regional": _PlaneShape(0.092, 0.84, 0.56, 0.27, 0.58, 0.28, 0.09, 0.82),
    "turboprop": _PlaneShape(0.115, 1.02, 0.10, 0.31, 0.58, 0.32, 0.12, 0.84),
    "business": _PlaneShape(0.084, 0.86, 0.68, 0.25, 0.56, 0.26, 0.08, 0.84),
    "military": _PlaneShape(0.150, 1.02, 0.42, 0.38, 0.58, 0.34, 0.12, 0.88),
    "light": _PlaneShape(0.082, 0.84, 0.46, 0.26, 0.58, 0.26, 0.09, 0.82),
}

_DEFAULT_SHAPE = _PLANE_SHAPES["narrow"]

#: 各类别图标的基准尺寸（半长，像素）。宽体与军用运输更大，
#: 与"看起来更重"的直觉一致；轻型机最小。
_KIND_SIZES: dict[str, float] = {
    "narrow": 9.0,
    "wide": 11.0,
    "regional": 8.4,
    "turboprop": 9.0,
    "business": 8.6,
    "military": 11.6,
    "light": 8.0,
    "helicopter": 8.8,
}


def _engine_nacelles(
    size: float,
    kind: str,
    engines: int,
    span: float,
    nose: float,
    wing_y: float,
) -> list[tuple[float, float, float, float]]:
    """返回发动机短舱的 ``(中心x, 中心y, 半径x, 半径y)`` 列表。

    涡桨机画成更大的螺旋桨盘，喷气机画成细长的短舱——
    这样"几发"和"涡桨还是喷气"都能从轮廓上看出来。
    ``wing_y`` 由调用方按机翼的实际位置给出，短舱才会恰好挂在翼下。
    """
    if kind == "turboprop":
        disc = size * 0.20
        if engines == 1:
            return [(0.0, nose + size * 0.22, disc, disc)]
        return [
            (-span * size * 0.50, wing_y, disc, disc),
            (span * size * 0.50, wing_y, disc, disc),
        ]

    radius_x, radius_y = size * 0.075, size * 0.125

    if engines == 1:
        return [(0.0, nose + size * 0.30, size * 0.095, size * 0.150)]
    if engines == 2:
        return [
            (-span * size * 0.50, wing_y, radius_x, radius_y),
            (span * size * 0.50, wing_y, radius_x, radius_y),
        ]
    if engines == 3:
        return [
            (-span * size * 0.54, wing_y, radius_x, radius_y),
            (span * size * 0.54, wing_y, radius_x, radius_y),
            (0.0, size * 0.62, radius_x, radius_y),      # 尾部中央发动机
        ]
    if engines == 4:
        return [
            (-span * size * 0.40, wing_y, radius_x, radius_y),
            (-span * size * 0.68, wing_y, radius_x, radius_y),
            (span * size * 0.40, wing_y, radius_x, radius_y),
            (span * size * 0.68, wing_y, radius_x, radius_y),
        ]
    if engines >= 6:
        # 六发及以上（An-225 之类），沿翼展均匀排布
        step = span * 0.80 / max(1, engines // 2 - 1)
        result = []
        for index in range(engines // 2):
            offset = span * size * (0.42 + index * step * 0.5)
            for side in (1.0, -1.0):
                result.append((side * offset, wing_y, radius_x, radius_y))
        return result
    return []


def _helicopter_path(size: float) -> QPainterPath:
    """直升机俯视轮廓：主旋翼盘 + 机身 + 尾梁 + 尾旋翼。

    形态与固定翼完全不同，因此单独构造而不是参数化复用。
    """
    path = QPainterPath()
    # 主旋翼盘：横向长椭圆，表示旋转中的桨盘
    path.addEllipse(QPointF(0.0, -size * 0.18), size * 1.02, size * 0.17)
    # 机身
    path.addEllipse(QPointF(0.0, -size * 0.20), size * 0.30, size * 0.58)
    # 尾梁
    path.addRect(-size * 0.075, size * 0.20, size * 0.15, size * 0.62)
    # 尾旋翼
    path.addEllipse(QPointF(0.0, size * 0.84), size * 0.17, size * 0.17)
    return path


@lru_cache(maxsize=128)
def _plane_path(size: float, kind: str, engines: int) -> QPainterPath:
    """按机型类别构造俯视轮廓，机头指向 ``-Y``（即正北）。

    由机身、主翼、水平尾翼与发动机短舱四部分组合而成。
    不同类别在**机身宽度、翼展、后掠角、发动机数量**四个维度上拉开差异，
    因此雷达图上既能看出宽窄，也能数出发动机个数。
    """
    if kind == "helicopter":
        return _helicopter_path(size)

    shape = _PLANE_SHAPES.get(kind, _DEFAULT_SHAPE)
    body_half = shape.body * size
    nose = -size
    tail = size * shape.tail_len

    path = QPainterPath()

    # ---- 机身：尖机头 → 细长平行段 → 略收窄的尾锥 ----
    path.moveTo(0.0, nose)
    path.lineTo(body_half * 0.45, nose + size * 0.20)
    path.lineTo(body_half, nose + size * 0.46)
    path.lineTo(body_half, tail - size * 0.30)
    path.lineTo(body_half * 0.55, tail)
    path.lineTo(-body_half * 0.55, tail)
    path.lineTo(-body_half, tail - size * 0.30)
    path.lineTo(-body_half, nose + size * 0.46)
    path.lineTo(-body_half * 0.45, nose + size * 0.20)
    path.closeSubpath()

    # ---- 主翼：前缘距机头 wing_pos、翼尖后掠 sweep、弦长由根到尖收窄 ----
    wing_root_front = nose + size * shape.wing_pos
    wing_root_back = wing_root_front + size * shape.root_chord
    wing_tip_front = wing_root_front + size * shape.sweep
    wing_tip_back = wing_tip_front + size * shape.tip_chord
    for side in (1.0, -1.0):
        wing = QPainterPath()
        wing.moveTo(side * body_half, wing_root_front)
        wing.lineTo(side * shape.span * size, wing_tip_front)
        wing.lineTo(side * shape.span * size, wing_tip_back)
        wing.lineTo(side * body_half, wing_root_back)
        wing.closeSubpath()
        path.addPath(wing)

    # ---- 水平尾翼：小得多，避免轮廓显得笨重 ----
    tail_root_front = tail - size * 0.22
    tail_tip_front = tail - size * 0.10
    tail_tip_back = tail - size * 0.005
    tail_root_back = tail - size * 0.12
    for side in (1.0, -1.0):
        fin = QPainterPath()
        fin.moveTo(side * body_half, tail_root_front)
        fin.lineTo(side * shape.tail_span * size, tail_tip_front)
        fin.lineTo(side * shape.tail_span * size, tail_tip_back)
        fin.lineTo(side * body_half, tail_root_back)
        fin.closeSubpath()
        path.addPath(fin)

    # ---- 发动机短舱 / 螺旋桨盘 ----
    wing_y = wing_root_front + size * shape.root_chord * 0.42
    for cx, cy, radius_x, radius_y in _engine_nacelles(
        size, kind, engines, shape.span, nose, wing_y
    ):
        path.addEllipse(QPointF(cx, cy), radius_x, radius_y)

    return path


class RadarView(QWidget):
    """圆形雷达主视图。"""

    #: 选中航班变化，携带 icao24；空串表示取消选中
    flight_selected = Signal(str)
    #: 用户滚轮调整了雷达半径（km）
    radius_changed = Signal(float)
    #: 用户点了左下角"回到当前位置"按钮
    home_requested = Signal()

    def __init__(
        self,
        config: Config,
        theme: Theme,
        basemap: Basemap,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._config = config
        self._theme = theme
        self._basemap = basemap

        # 圆心（用户位置）与半径
        self._lat = 0.0
        self._lon = 0.0
        self._radius_km = float(config.radius_km)
        self._projector: Projector | None = None

        # 数据
        self._flights: list[Flight] = []
        self._payload: dict | None = None
        self._payload_at = 0.0
        self._status_text = "正在启动…"

        # 交互状态
        self._selected = ""
        self._hovered = ""

        # 覆盖层数据。全部画在静态层上，因此只在数据或视口变化时重建
        self._terrain: TerrainLayer | None = None
        self._roads: RoadLayer | None = None
        self._cloud: CloudField | None = None
        self._places: list[Place] = []
        self._airports: list[Airport] = []
        self._weather: Weather | None = None
        self._overlay_revision = 0
        self._overlays_enabled = {
            "terrain": True,
            "roads": True,
            "clouds": True,
            "places": True,
            "airports": True,
            "weather": True,
        }
        #: 用户真实位置。雷达中心可能被"跳转到机场"移走，用它判断能否回位
        self._home_lat: float | None = None
        self._home_lon: float | None = None

        # 扫描动画
        self._sweep_angle = 0.0
        self._last_tick = time.monotonic()
        self._echo_at: dict[str, float] = {}

        # 静态层缓存
        self._static_layer: QPixmap | None = None
        self._static_key: tuple | None = None
        #: 静态层对应的雷达半径，用于缩放预览时按比例拉伸
        self._static_base_radius = 0.0

        # 缩放预览：滚轮连续滚动时，静态层每一档都要重建（实测约 240 ms），
        # 直接重绘会明显卡顿。这里先用旧层按比例拉伸顶着，停稳 250 ms
        # 后再精确重建——期间画面略有模糊但跟手，松手后立刻变清晰。
        self._preview_active = False
        self._preview_scale = 1.0
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(250)
        self._rebuild_timer.timeout.connect(self._end_preview)

        self.setMinimumSize(560, 560)
        self.setMouseTracking(True)
        self.setCursor(Qt.CursorShape.CrossCursor)
        self.setAutoFillBackground(False)

        self._timer = QTimer(self)
        self._timer.setInterval(FRAME_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

        self._rebuild_projector()

    # ==================================================================
    # 对外接口
    # ==================================================================

    def set_location(self, lat: float, lon: float) -> None:
        """移动雷达圆心到新的用户位置。"""
        if abs(lat - self._lat) < 1e-9 and abs(lon - self._lon) < 1e-9:
            return
        self._lat, self._lon = float(lat), float(lon)
        self._rebuild_projector()
        self.update()

    def set_radius(self, radius_km: float) -> None:
        """设置雷达半径（km）。"""
        radius_km = float(radius_km)
        if abs(radius_km - self._radius_km) < 1e-9:
            return

        # 已有静态层时进入预览模式：先拉伸旧层顶着，避免每滚一档就重建一次
        if self._static_layer is not None and self._static_base_radius > 0:
            self._preview_active = True
            self._preview_scale = self._static_base_radius / radius_km
            self._rebuild_timer.start()

        self._radius_km = radius_km
        self._rebuild_projector()
        self.update()

    def _end_preview(self) -> None:
        """结束预览，触发一次精确重建。"""
        if not self._preview_active:
            return
        self._preview_active = False
        self._preview_scale = 1.0
        self.update()

    def set_theme(self, theme: Theme) -> None:
        """切换主题（配色 / 字号 / 背景档位）。"""
        self._theme = theme
        self.update()

    def set_status(self, text: str) -> None:
        """设置底部状态文本（用于启动、定位、错误等阶段提示）。"""
        if text != self._status_text:
            self._status_text = text
            self.update()

    # ==================================================================
    # 覆盖层数据
    # ==================================================================
    #
    # 这些数据都随视口变化，因此统一画在静态层上（QPixmap 缓存）。
    # 每次更新只需递增一个版本号，静态层的缓存键就会失效并重建。

    def _touch_overlays(self) -> None:
        """标记覆盖层数据已变化，使静态层缓存失效。"""
        self._overlay_revision += 1
        self.update()

    def set_terrain(self, terrain: TerrainLayer | None) -> None:
        """设置地形高程数据（海拔着色）。"""
        self._terrain = terrain
        self._touch_overlays()

    def set_roads(self, roads: RoadLayer | None) -> None:
        """设置路网数据。"""
        self._roads = roads
        self._touch_overlays()

    def set_cloud_field(self, cloud: CloudField | None) -> None:
        """设置云量场（云图叠加）。"""
        self._cloud = cloud
        self._touch_overlays()

    def set_places(self, places: list[Place]) -> None:
        """设置要标注的城市（调用方应按人口降序传入）。"""
        self._places = list(places or [])
        self._touch_overlays()

    def set_airports(self, airports: list[Airport]) -> None:
        """设置视野内的机场（含跑道）。"""
        self._airports = list(airports or [])
        self._touch_overlays()

    def set_weather(self, weather: Weather | None) -> None:
        """设置当前位置的天气。"""
        self._weather = weather
        self._touch_overlays()

    def set_overlays_enabled(self, **flags: bool) -> None:
        """开关各覆盖层，例如 ``set_overlays_enabled(roads=False)``。"""
        changed = False
        for key, value in flags.items():
            if key in self._overlays_enabled and self._overlays_enabled[key] != bool(value):
                self._overlays_enabled[key] = bool(value)
                changed = True
        if changed:
            self._touch_overlays()

    def set_home_location(self, lat: float, lon: float) -> None:
        """记录用户真实位置，供"回到当前位置"使用。"""
        self._home_lat, self._home_lon = float(lat), float(lon)
        self.update()

    @property
    def is_home(self) -> bool:
        """雷达中心当前是否就是用户位置。"""
        if self._home_lat is None or self._home_lon is None:
            return True
        return abs(self._lat - self._home_lat) < 1e-6 and abs(self._lon - self._home_lon) < 1e-6

    def home_location(self) -> tuple[float, float] | None:
        if self._home_lat is None or self._home_lon is None:
            return None
        return self._home_lat, self._home_lon

    def set_flights(self, payload: dict) -> None:
        """接收一轮新数据。

        ``payload`` 由 :class:`app.data.manager.FlightManager` 发出，
        含 ``flights`` / ``center`` / ``radius_km`` / ``source`` / ``stale`` 等键。
        """
        center = payload.get("center")
        if center:
            self._lat, self._lon = float(center[0]), float(center[1])

        radius = payload.get("radius_km")
        if radius:
            self._radius_km = float(radius)

        self._rebuild_projector()

        self._flights = list(payload.get("flights") or [])
        self._payload = payload
        self._payload_at = time.monotonic()

        # 清理已消失航班的回波记录，避免字典无限增长
        alive = {flight.icao24 for flight in self._flights}
        if len(self._echo_at) > len(alive):
            self._echo_at = {k: v for k, v in self._echo_at.items() if k in alive}

        self.update()

    def set_selected(self, icao24: str) -> None:
        """高亮指定航班（传空串取消）。"""
        icao24 = (icao24 or "").lower()
        if icao24 != self._selected:
            self._selected = icao24
            self.update()

    def flight_by_id(self, icao24: str) -> Flight | None:
        icao24 = (icao24 or "").lower()
        for flight in self._flights:
            if flight.icao24 == icao24:
                return flight
        return None

    @property
    def flights(self) -> list[Flight]:
        return list(self._flights)

    @property
    def projector(self) -> Projector | None:
        return self._projector

    # ==================================================================
    # 几何
    # ==================================================================

    def _rebuild_projector(self) -> None:
        """按当前控件尺寸重建投影仪，圆心固定在控件中心。"""
        width = max(1, self.width())
        height = max(1, self.height())
        dish_radius = max(1.0, min(width, height) / 2.0 - DISH_MARGIN)
        self._projector = Projector(
            self._lat,
            self._lon,
            self._radius_km,
            width / 2.0,
            height / 2.0,
            dish_radius,
        )

    @property
    def dish_radius(self) -> float:
        return self._projector.radius_px if self._projector else 1.0

    def _flight_screen_pos(self, flight: Flight, now: float) -> tuple[float, float]:
        """返回航班当前应绘制的屏幕坐标。

        数据每 10 秒才刷新一次，直接跳到新位置会显得生硬；
        这里在新旧位置之间按时间做 smoothstep 插值，让图标平滑滑行。
        """
        projector = self._projector
        if projector is None:
            return 0.0, 0.0

        target = projector.project(flight.lat, flight.lon)
        if flight.prev_lat is None or flight.prev_lon is None:
            # 首次出现的航班直接落位，不做插值
            return target

        previous = projector.project(flight.prev_lat, flight.prev_lon)
        interval = max(3.0, float(self._config.refresh_sec))
        progress = _ease((now - self._payload_at) / interval)
        return (
            previous[0] + (target[0] - previous[0]) * progress,
            previous[1] + (target[1] - previous[1]) * progress,
        )

    # ==================================================================
    # 静态层
    # ==================================================================

    def _static_pixmap(self) -> QPixmap:
        """取得静态层位图，必要时重建。

        静态层含底图、各类覆盖层与刻度。每帧重绘这些内容会白白吃掉 CPU，
        缓存成位图后每帧只需一次 ``drawPixmap``。
        """
        # 预览期间沿用旧层：此时半径已经变了，缓存键必然失效，
        # 若照常重建就等于每滚一档都付一次 240 ms，正是卡顿的来源
        if self._preview_active and self._static_layer is not None:
            return self._static_layer

        theme = self._theme
        key = (
            self.width(),
            self.height(),
            round(self._lat, 6),
            round(self._lon, 6),
            round(self._radius_km, 3),
            theme.font_size,
            theme.bg_level,
            theme.scan,
            # 底图与覆盖层都是后台加载的，状态一变缓存键就失效并自动重建
            self._basemap.is_loaded,
            self._overlay_revision,
        )
        if self._static_layer is not None and self._static_key == key:
            return self._static_layer

        ratio = self.devicePixelRatioF() or 1.0
        pixmap = QPixmap(int(self.width() * ratio), int(self.height() * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.GlobalColor.transparent)

        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._draw_static(painter)
        painter.end()

        self._static_layer = pixmap
        self._static_key = key
        self._static_base_radius = self._radius_km
        return pixmap

    def _draw_static(self, painter: QPainter) -> None:
        projector = self._projector
        if projector is None:
            return

        theme = self._theme
        cx, cy = projector.center_x, projector.center_y
        dish = projector.radius_px

        # 盘底：底图未就绪时这就是雷达盘的底色
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(theme.radar_dish))
        painter.drawEllipse(QPointF(cx, cy), dish, dish)

        # 底图（内部已自行裁剪到圆内）
        if self._basemap.is_loaded:
            self._basemap.paint(painter, projector, theme, dish)

        # ---- 覆盖层 ----
        # 顺序有讲究：海拔压在陆地之上、路网又在海拔之上，
        # 云图与城市名最后画，否则会被下面的图层盖住。
        #
        # 所有覆盖层都必须裁剪到雷达盘内：地形与云图是用 drawImage 画的
        # **矩形**（范围是整个数据栅格，比盘大得多），不裁剪就会在盘外
        # 留下一大片半透明色块。
        enabled = self._overlays_enabled
        dish_clip = QPainterPath()
        dish_clip.addEllipse(QPointF(cx, cy), dish, dish)

        painter.save()
        painter.setClipPath(dish_clip)
        if enabled["terrain"] and self._terrain is not None:
            overlays.draw_terrain(painter, self._terrain, projector, dish)
        if enabled["roads"] and self._roads is not None:
            overlays.draw_roads(painter, self._roads, projector, dish)
        if enabled["clouds"] and self._cloud is not None:
            overlays.draw_clouds(painter, self._cloud, projector, dish)
        if enabled["airports"] and self._airports:
            overlays.draw_airports(painter, self._airports, projector, theme)
        if enabled["places"] and self._places:
            overlays.draw_places(painter, self._places, projector, theme, dish)
        painter.restore()

        # 海岸线画在最后，保证轮廓始终清晰
        if self._basemap.is_loaded:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            pen = QPen(_qc(theme.coastline, 0.45))
            pen.setWidthF(0.8)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawPath(self._basemap.paths(projector).coastline)

        if enabled["weather"] and self._weather is not None:
            overlays.draw_weather_arrow(painter, self._weather, theme,
                                        projector.center_x, projector.center_y)

        self._draw_rings(painter)
        self._draw_compass(painter)
        self._draw_center_marker(painter)
        self._draw_home_button(painter)

        if enabled["weather"] and self._weather is not None:
            overlays.draw_weather_badge(painter, self._weather, theme, self.width())

    def _draw_rings(self, painter: QPainter) -> None:
        """等距同心圈与距离刻度标签。

        圈半径由 :func:`app.geo.ring_values_km` 给出"整数好看"的步进
        （如 50 km 半径取 10 / 20 / 30 / 40），避免出现 7.3 km 这种读数。
        """
        projector = self._projector
        theme = self._theme
        cx, cy = projector.center_x, projector.center_y

        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setFont(make_font(max(9, theme.font_minor - 1), mono=True))

        for value in ring_values_km(self._radius_km):
            radius = projector.px_radius_for(value)
            if radius < 10.0:
                continue

            pen = QPen(_qc(theme.scan, 0.11))
            pen.setWidthF(1.0)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)

            # 距离标签压在正北方向的圈线上，稍向上偏移避免与圈线重叠
            painter.setPen(_qc(theme.scan, 0.42))
            painter.drawText(QPointF(cx + 6.0, cy - radius - 5.0), f"{value:.0f} km")

        # 最外圈用更亮的描边界定雷达盘边界
        pen = QPen(_qc(theme.scan, 0.30))
        pen.setWidthF(1.2)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawEllipse(QPointF(cx, cy), projector.radius_px, projector.radius_px)

    def _draw_compass(self, painter: QPainter) -> None:
        """外圈方位刻度：细密刻度线 + 0/90/180/270 正方位标注。

        刻度画在雷达盘**外侧**，既不占用盘内空间，也不会与航班图标打架。
        """
        projector = self._projector
        theme = self._theme
        cx, cy = projector.center_x, projector.center_y
        dish = projector.radius_px

        # 细密刻度线：每 5° 一条，长度按重要程度分三档
        for bearing in range(0, 360, 5):
            if bearing % 90 == 0:
                length, alpha, width = 13.0, 0.75, 1.4
            elif bearing % 30 == 0:
                length, alpha, width = 9.0, 0.45, 1.0
            else:
                length, alpha, width = 4.5, 0.20, 1.0

            radians = math.radians(bearing)
            sin_b, cos_b = math.sin(radians), math.cos(radians)
            inner = dish + 5.0
            outer = inner + length

            pen = QPen(_qc(theme.scan, alpha))
            pen.setWidthF(width)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawLine(
                QPointF(cx + inner * sin_b, cy - inner * cos_b),
                QPointF(cx + outer * sin_b, cy - outer * cos_b),
            )

        # 度数标签：每 30° 一个，正方位换成 N/E/S/W 并加粗。
        # 半径取 dish + 24 而非更大：底部还要给两行状态文本留位置，
        # 正南方向的标签一旦下探就会被状态行压住。
        label_radius = dish + 24.0
        for bearing in range(0, 360, 30):
            radians = math.radians(bearing)
            x = cx + label_radius * math.sin(radians)
            y = cy - label_radius * math.cos(radians)

            if bearing in _CARDINAL:
                font = make_font(theme.font_major, bold=True, mono=True)
                color = _qc(theme.scan, 0.95)
                text = _CARDINAL[bearing]
            else:
                font = make_font(max(9, theme.font_minor - 1), mono=True)
                color = _qc(theme.scan, 0.40)
                text = f"{bearing:03d}"

            painter.setFont(font)
            painter.setPen(color)
            metrics = QFontMetrics(font)
            width = metrics.horizontalAdvance(text)
            height = metrics.height()
            painter.drawText(
                QRectF(x - width / 2.0, y - height / 2.0, width, height),
                Qt.AlignmentFlag.AlignCenter,
                text,
            )

    def _draw_center_marker(self, painter: QPainter) -> None:
        """圆心站位标记：双圈 + 四向短线，标出用户真实位置。"""
        projector = self._projector
        theme = self._theme
        cx, cy = projector.center_x, projector.center_y

        painter.setBrush(Qt.BrushStyle.NoBrush)

        pen = QPen(_qc(theme.highlight, 0.30))
        pen.setWidthF(1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawEllipse(QPointF(cx, cy), 10.5, 10.5)

        pen = QPen(_qc(theme.highlight, 0.85))
        pen.setWidthF(1.3)
        pen.setCosmetic(True)
        painter.setPen(pen)
        painter.drawEllipse(QPointF(cx, cy), 5.0, 5.0)

        pen = QPen(_qc(theme.highlight, 0.45))
        pen.setWidthF(1.0)
        pen.setCosmetic(True)
        painter.setPen(pen)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            painter.drawLine(
                QPointF(cx + dx * 7.0, cy + dy * 7.0),
                QPointF(cx + dx * 14.0, cy + dy * 14.0),
            )

    # 左下角"回到当前位置"按钮的尺寸与边距
    HOME_BUTTON_SIZE = 34.0
    HOME_BUTTON_MARGIN = 18.0

    def _home_button_rect(self) -> QRectF:
        """左下角按钮的位置。"""
        size = self.HOME_BUTTON_SIZE
        return QRectF(
            self.HOME_BUTTON_MARGIN,
            self.height() - self.HOME_BUTTON_MARGIN - size,
            size,
            size,
        )

    def _draw_home_button(self, painter: QPainter) -> None:
        """左下角的"回到当前位置"按钮。

        中心已经回到用户位置时淡化显示并禁用点击——
        让用户一眼看出"现在按了也没用"。
        """
        theme = self._theme
        rect = self._home_button_rect()
        at_home = self.is_home

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_qc(theme.panel_bg, 0.80 if not at_home else 0.45))
        painter.drawRoundedRect(rect, 7.0, 7.0)

        pen = QPen(_qc(theme.border, 0.9 if not at_home else 0.45))
        pen.setWidthF(1.0)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawRoundedRect(rect, 7.0, 7.0)

        # 准星 + 中心点，比"房子"图标更贴合雷达语境
        cx = rect.center().x()
        cy = rect.center().y()
        accent = _qc(theme.highlight, 0.90 if not at_home else 0.42)
        pen = QPen(accent)
        pen.setWidthF(1.4)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.drawEllipse(QPointF(cx, cy), 7.0, 7.0)
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            painter.drawLine(QPointF(cx + dx * 7.0, cy + dy * 7.0),
                             QPointF(cx + dx * 10.5, cy + dy * 10.5))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(accent)
        painter.drawEllipse(QPointF(cx, cy), 2.2, 2.2)

        painter.restore()

    # ==================================================================
    # 绘制：动态层
    # ==================================================================

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        theme = self._theme
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
        painter.fillRect(self.rect(), QColor(theme.window_bg))

        if self._projector is None:
            painter.end()
            return

        layer = self._static_pixmap()
        if self._preview_active and abs(self._preview_scale - 1.0) > 1e-4:
            # 以圆心为中心按比例拉伸旧层，让缩放即时跟手
            cx, cy = self._projector.center_x, self._projector.center_y
            painter.save()
            painter.translate(cx, cy)
            painter.scale(self._preview_scale, self._preview_scale)
            painter.translate(-cx, -cy)
            painter.drawPixmap(0, 0, layer)
            painter.restore()
        else:
            painter.drawPixmap(0, 0, layer)

        self._draw_sweep(painter)
        self._draw_flights(painter)
        self._draw_corners(painter)
        self._draw_status(painter)
        painter.end()

    def _draw_sweep(self, painter: QPainter) -> None:
        """扫描扇形与余晖拖尾。

        用锥形渐变实现：扫描前沿最亮，向已扫过的方向渐隐约 72°。
        Qt 的锥形渐变以 3 点钟方向为 0°、逆时针为正，
        而航向角以正北为 0°、顺时针为正，故换算关系为 ``90 - θ``。
        """
        projector = self._projector
        theme = self._theme
        cx, cy = projector.center_x, projector.center_y
        dish = projector.radius_px
        theta = self._sweep_angle

        tail = SWEEP_TAIL_DEG / 360.0
        gradient = QConicalGradient(QPointF(cx, cy), 90.0 - theta)
        gradient.setColorAt(0.0, _qc(theme.scan, 0.32))
        gradient.setColorAt(tail * 0.30, _qc(theme.scan, 0.16))
        gradient.setColorAt(tail * 0.65, _qc(theme.scan, 0.055))
        gradient.setColorAt(tail, _qc(theme.scan, 0.0))
        gradient.setColorAt(1.0, _qc(theme.scan, 0.0))

        painter.save()
        dish_path = QPainterPath()
        dish_path.addEllipse(QPointF(cx, cy), dish, dish)
        painter.setClipPath(dish_path)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(gradient)
        painter.drawEllipse(QPointF(cx, cy), dish, dish)
        painter.restore()

        # 前沿亮线：三层描边叠出光晕，越细越亮
        radians = math.radians(theta)
        tip_x = cx + dish * math.sin(radians)
        tip_y = cy - dish * math.cos(radians)

        painter.save()
        painter.setClipPath(dish_path)
        for width, alpha in ((3.6, 0.14), (1.8, 0.42), (0.9, 0.95)):
            pen = QPen(_qc(theme.scan, alpha))
            pen.setWidthF(width)
            pen.setCosmetic(True)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(cx, cy), QPointF(tip_x, tip_y))
        painter.restore()

    def _draw_flights(self, painter: QPainter) -> None:
        """绘制全部航班。

        绘制顺序按「普通 → 悬停 → 选中」安排，保证高亮目标压在最上层，
        不会被后续绘制覆盖。
        """
        if not self._flights:
            return

        projector = self._projector
        center_x, center_y = projector.center_x, projector.center_y
        # 允许图标略微探出盘边（贴边的航班这样更自然），但不能飘到很远
        limit = projector.radius_px + 16.0

        now = time.monotonic()
        ordered = sorted(
            self._flights,
            key=lambda f: (f.icao24 == self._selected, f.icao24 == self._hovered),
        )
        for flight in ordered:
            x, y = self._flight_screen_pos(flight, now)
            # 半径刚从大缩小时，手里仍是旧半径的数据，其中较远的航班会落到盘外。
            # 不过滤的话会出现"漂浮在雷达盘之外"的图标。
            if math.hypot(x - center_x, y - center_y) > limit:
                continue
            self._draw_one_flight(painter, flight, x, y, now)

    def _draw_one_flight(
        self, painter: QPainter, flight: Flight, x: float, y: float, now: float
    ) -> None:
        theme = self._theme
        selected = flight.icao24 == self._selected
        hovered = flight.icao24 == self._hovered

        # 被扫描线掠过后的回波强度，1.6 秒内衰减到 0
        swept_at = self._echo_at.get(flight.icao24)
        echo = 0.0 if swept_at is None else max(0.0, 1.0 - (now - swept_at) / ECHO_DECAY_SEC)

        # 机型形态：类别决定轮廓形状，宽窄与发动机数量体现为可识别的细节
        kind = aircraft_kind(flight.info.type_code, flight.category)
        base_size = _KIND_SIZES.get(kind.key, 8.6)

        # 宽体机用琥珀黄区分；选中 / 悬停改用高亮青绿。
        # 形状与颜色双重区分：颜色表达"是不是宽体"，形状表达"几发、什么类型"。
        if selected:
            color, size = QColor(theme.highlight), base_size + 2.6
        elif hovered:
            color, size = QColor(theme.highlight), base_size + 1.6
        elif flight.is_widebody:
            color, size = QColor(theme.widebody), base_size
        else:
            color, size = QColor(theme.aircraft), base_size

        painter.save()
        painter.translate(x, y)

        if selected:
            # 选中态加指示环，便于在密集图标中一眼定位
            painter.setBrush(Qt.BrushStyle.NoBrush)
            pen = QPen(_qc(theme.highlight, 0.62))
            pen.setWidthF(1.2)
            pen.setCosmetic(True)
            painter.setPen(pen)
            painter.drawEllipse(QPointF(0, 0), 16.5, 16.5)

        if echo > 0.02:
            # 回波柔光：一圈向外扩散的径向渐变
            glow = QRadialGradient(QPointF(0, 0), 16.0)
            glow.setColorAt(0.0, _qc(theme.scan, 0.44 * echo))
            glow.setColorAt(1.0, _qc(theme.scan, 0.0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(glow)
            painter.drawEllipse(QPointF(0, 0), 16.0, 16.0)

        # 旋转到真实航向：0° = 正北 = 屏幕上方，顺时针为正
        if flight.track is not None:
            painter.rotate(flight.track)

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(color if echo <= 0.02 else _blend(color, QColor(theme.scan), 0.55 * echo))
        painter.drawPath(_plane_path(size, kind.key, kind.engines))
        painter.restore()

        self._draw_flight_label(painter, flight, x, y, selected or hovered)

    def _draw_flight_label(
        self, painter: QPainter, flight: Flight, x: float, y: float, emphasized: bool
    ) -> None:
        """在图标旁标注航班号与当前海拔。

        标签默认放图标右侧；若会溢出雷达盘则翻到左侧——
        否则贴着盘边的航班标签会被裁掉一半。
        """
        theme = self._theme
        projector = self._projector
        unit = self._config.altitude_unit

        text = f"{flight.flight_number} {flight.display_altitude_short(unit)}"
        font = make_font(max(9, theme.font_minor - 1), mono=True, bold=emphasized)
        painter.setFont(font)

        metrics = QFontMetrics(font)
        text_width = metrics.horizontalAdvance(text)
        text_height = metrics.height()
        pad_x, pad_y = 5.0, 1.5
        box_width = text_width + pad_x * 2.0
        box_height = text_height + pad_y * 2.0

        # 标签优先放图标右侧；右侧放不下就翻到左侧。
        # 关键：夹紧边界必须按**圆**算，而不是用 center_x ± radius_px 的矩形边框——
        # 雷达盘是圆的，在同一高度 y 处圆的半宽远小于半径，
        # 用矩形边界会导致贴边航班的标签看似在框内、实际已经出圆外。
        gap = 13.0
        dy = y - projector.center_y
        radius = projector.radius_px
        if abs(dy) >= radius:
            half_width = 0.0
        else:
            half_width = math.sqrt(radius * radius - dy * dy)

        right_edge = projector.center_x + half_width - 6.0
        left_edge = projector.center_x - half_width + 6.0

        left = x + gap
        if left + box_width > right_edge:
            left = x - gap - box_width
        left = max(left_edge, min(left, right_edge - box_width))

        rect = QRectF(left, y - box_height / 2.0, box_width, box_height)

        # 半透明底衬：保证文字在底图与扫描余晖上都能看清
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_qc(theme.window_bg, 0.68 if emphasized else 0.46))
        painter.drawRoundedRect(rect, 2.5, 2.5)

        painter.setPen(QColor(theme.highlight) if emphasized else _qc(theme.text_primary, 0.88))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)

    def _draw_corners(self, painter: QPainter) -> None:
        """左上角日期、右上角时间，与系统时间同步走秒。

        用等宽字体，秒数跳动时文字不会左右抖动。
        """
        theme = self._theme
        now = datetime.now()

        painter.setFont(make_font(max(10, theme.font_minor), mono=True))
        painter.setPen(_qc(theme.text_secondary, 0.90))
        painter.drawText(QPointF(18.0, 27.0), f"{now:%Y-%m-%d}  {_WEEKDAYS[now.weekday()]}")

        font = make_font(theme.font_major + 4, bold=True, mono=True)
        painter.setFont(font)
        painter.setPen(_qc(theme.scan, 0.92))
        text = f"{now:%H:%M:%S}"
        width = QFontMetrics(font).horizontalAdvance(text)
        painter.drawText(QPointF(self.width() - 18.0 - width, 29.0), text)

    def _draw_status(self, painter: QPainter) -> None:
        """雷达盘下方的状态行：数据源、航班数、半径、更新时间。"""
        theme = self._theme
        payload = self._payload

        if payload is None:
            parts = [self._status_text]
        else:
            parts = []
            source = payload.get("source")
            if source:
                parts.append(f"数据源 {source}")
            parts.append(f"{len(self._flights)} 架")
            parts.append(f"半径 {self._radius_km:.0f} km")
            fetched = payload.get("fetched_at")
            if fetched is not None:
                parts.append(f"更新 {fetched.astimezone():%H:%M:%S}")
            if payload.get("stale"):
                parts.append("数据延迟")
            if len(parts) == 1 and self._status_text:
                parts = [self._status_text]

        font = make_font(max(10, theme.font_minor), mono=True)
        painter.setFont(font)
        painter.setPen(_qc(theme.text_dim, 0.95))
        metrics = QFontMetrics(font)
        text = "  ·  ".join(parts)

        # 窗口较窄时退化为简版，避免文字溢出控件边界
        available = self.width() - 40.0
        if metrics.horizontalAdvance(text) > available and len(parts) > 2:
            text = "  ·  ".join([parts[0], parts[1]])

        width = metrics.horizontalAdvance(text)
        painter.drawText(
            QPointF((self.width() - width) / 2.0, self.height() - 32.0), text
        )

        # 降级 / 限流提示单独一行，用琥珀色以引起注意
        notice = (payload or {}).get("notice")
        if notice:
            painter.setPen(_qc(theme.widebody, 0.85))
            short = notice
            while short and metrics.horizontalAdvance(short) > available:
                short = short[:-2]
            if short != notice:
                short = (short[:-1] + "…") if short else ""
            if short:
                width = metrics.horizontalAdvance(short)
                painter.drawText(
                    QPointF((self.width() - width) / 2.0, self.height() - 14.0), short
                )

    # ==================================================================
    # 动画
    # ==================================================================

    def _on_tick(self) -> None:
        """动画帧：推进扫描角、记录回波、请求重绘。"""
        now = time.monotonic()
        # 限制单帧步进，避免系统休眠唤醒后扫描线突然跳过大半圈
        elapsed = max(0.0, min(1.0, now - self._last_tick))
        self._last_tick = now

        previous = self._sweep_angle
        self._sweep_angle = (previous + elapsed / (SWEEP_PERIOD_MS / 1000.0) * 360.0) % 360.0

        # 标记本帧被扫描线掠过的航班，供回波高亮使用
        if self._flights and previous != self._sweep_angle:
            for flight in self._flights:
                if _angle_in_sweep(flight.bearing, previous, self._sweep_angle):
                    self._echo_at[flight.icao24] = now

        self.update()

    # ==================================================================
    # 交互
    # ==================================================================

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        super().resizeEvent(event)
        self._rebuild_projector()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        if event.button() != Qt.MouseButton.LeftButton:
            return

        # 左下角按钮优先响应；已经在当前位置时不响应
        if self._home_button_rect().contains(event.position()):
            if not self.is_home:
                self.home_requested.emit()
            return

        hit = self._flight_at(event.position())
        # 空串表示点到了空白处，界面据此取消选中
        self.flight_selected.emit(hit.icao24 if hit else "")

    def mouseMoveEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        hit = self._flight_at(event.position())
        icao24 = hit.icao24 if hit else ""
        if icao24 != self._hovered:
            self._hovered = icao24
            self.setCursor(
                Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.CrossCursor
            )

    def leaveEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        if self._hovered:
            self._hovered = ""
            self.setCursor(Qt.CursorShape.CrossCursor)

    def wheelEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        """滚轮缩放雷达半径。

        按档位跳变而非连续缩放：半径值更整齐好看，
        静态层与投影路径的缓存命中率也显著更高。
        """
        delta = event.angleDelta().y()
        if delta == 0:
            return

        index = self._nearest_stop_index(self._radius_km)
        index += 1 if delta > 0 else -1
        index = max(0, min(len(RADIUS_STOPS) - 1, index))
        new_radius = RADIUS_STOPS[index]

        if abs(new_radius - self._radius_km) > 1e-9:
            self.radius_changed.emit(new_radius)
        event.accept()

    @staticmethod
    def _nearest_stop_index(radius_km: float) -> int:
        """找到与当前半径最接近的档位下标。"""
        best_index, best_delta = 0, float("inf")
        for index, stop in enumerate(RADIUS_STOPS):
            delta = abs(stop - radius_km)
            if delta < best_delta:
                best_index, best_delta = index, delta
        return best_index

    def _flight_at(self, point) -> Flight | None:
        """命中测试：返回点击位置附近最近的一架航班。"""
        projector = self._projector
        if projector is None:
            return None

        center_x, center_y = projector.center_x, projector.center_y
        limit = projector.radius_px + 16.0
        now = time.monotonic()
        best: Flight | None = None
        best_distance = HIT_RADIUS_PX * HIT_RADIUS_PX

        for flight in self._flights:
            x, y = self._flight_screen_pos(flight, now)
            # 与绘制保持一致：盘外的航班没有画出来，自然也不该被点到
            if math.hypot(x - center_x, y - center_y) > limit:
                continue
            distance = (x - point.x()) ** 2 + (y - point.y()) ** 2
            if distance < best_distance:
                best, best_distance = flight, distance
        return best
