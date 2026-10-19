"""雷达视图上的各类底图覆盖层绘制。

把绘制逻辑从 ``radar_view`` 里拆出来，是因为覆盖层已经有五类
（地形着色、路网、云图、机场跑道、城市名标签），全塞进一个类会难以维护。

这些覆盖层都画在**静态层**上（``QPixmap`` 缓存），只在视口 / 半径 / 图层数据
变化时重建，因此不必担心每帧重绘的开销。

绘制顺序由 ``radar_view`` 决定：海洋底色 → 地形 → 陆地 → 路网 → 海岸线
→ 云图 → 机场跑道 → 城市名 → 刻度圈。海拔和路网必须在陆地之上，
云图与城市名又在它们之上，否则会被盖住。
"""

from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
)

from app.airports import Airport
from app.geo import Projector
from app.places import Place
from app.roads import RoadLayer
from app.terrain import TerrainLayer, elevation_color
from app.theme import Theme, make_font, rgba
from app.weather import CloudField, Weather


def _qc(hex_color: str, alpha: float) -> QColor:
    color = QColor(hex_color)
    color.setAlphaF(max(0.0, min(1.0, alpha)))
    return color


#: 经纬度图像投影时的分块数。每块用四边形映射，块数越多越精确，
#: 但 5×5 已经能把误差压到 0.05% 以内，而总投影次数只有 100 次。
_GEO_IMAGE_TILES = 5


def draw_geo_image(
    painter: QPainter,
    image: QImage,
    lat_min: float,
    lat_max: float,
    lon_min: float,
    lon_max: float,
    projector: Projector,
    tiles: int = _GEO_IMAGE_TILES,
) -> None:
    """把一张按经纬度铺开的图像投影绘制到雷达盘上。

    **不能**像早先那样"投影两个对角点、构造屏幕矩形再 drawImage"：
    极坐标等距投影下，一个经纬度矩形投影出来是**弯曲的梯形**（高纬度的
    经线间距收缩），用矩形画会把内容整体拉伸错位——460 km 视野下台湾岛
    能偏出二十多像素，海拔色块与海岸线明显对不上。

    这里的做法是把图像切成 ``tiles × tiles`` 小块，每块用
    ``QTransform.quadToQuad`` 做四边形映射。块内以双线性近似真实投影，
    误差可忽略；而投影次数只有 ``tiles² × 4`` 次，比逐像素反投影快几个数量级。
    """
    if image.isNull() or image.width() < 1 or image.height() < 1:
        return

    src_w = float(image.width())
    src_h = float(image.height())
    lon_span = lon_max - lon_min
    lat_span = lat_max - lat_min

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    for row in range(tiles):
        for col in range(tiles):
            # 该块对应的经纬度四角。图像第 0 行是 lat_max（北），
            # 第 0 列是 lon_min（西）。
            frac_l = col / tiles
            frac_r = (col + 1) / tiles
            frac_t = row / tiles
            frac_b = (row + 1) / tiles

            lon_l = lon_min + lon_span * frac_l
            lon_r = lon_min + lon_span * frac_r
            lat_t = lat_max - lat_span * frac_t
            lat_b = lat_max - lat_span * frac_b

            # 源块在图像里的像素矩形
            sx0, sx1 = src_w * frac_l, src_w * frac_r
            sy0, sy1 = src_h * frac_t, src_h * frac_b

            project = projector.project
            x_tl, y_tl = project(lat_t, lon_l)
            x_tr, y_tr = project(lat_t, lon_r)
            x_br, y_br = project(lat_b, lon_r)
            x_bl, y_bl = project(lat_b, lon_l)

            source_quad = QPolygonF([
                QPointF(sx0, sy0), QPointF(sx1, sy0),
                QPointF(sx1, sy1), QPointF(sx0, sy1),
            ])
            dest_quad = QPolygonF([
                QPointF(x_tl, y_tl), QPointF(x_tr, y_tr),
                QPointF(x_br, y_br), QPointF(x_bl, y_bl),
            ])

            transform = QTransform()
            if not QTransform.quadToQuad(source_quad, dest_quad, transform):
                continue

            painter.save()
            painter.setTransform(transform, True)
            source_rect = QRectF(sx0, sy0, sx1 - sx0, sy1 - sy0)
            painter.drawImage(source_rect, image, source_rect)
            painter.restore()

    painter.restore()


# ---------------------------------------------------------------------------
# 地形高程
# ---------------------------------------------------------------------------

def _elevation_lut() -> np.ndarray:
    """高程 → BGRA 颜色的查找表（0–8000 m，每米一项）。

    逐像素调用 ``QImage.setPixelColor`` 会慢得离谱：426×426 的栅格有 18 万次
    调用，实测单次静态层重建要 240 ms，滚轮缩放时肉眼可见地卡。
    改成先用 numpy 查表算出整块像素，再一次性构造 QImage，可以压到几毫秒。

    注意 ``Format_ARGB32`` 在内存里的字节序是 **BGRA**（小端），
    查表的最后一位才是 alpha。
    """
    lut = np.zeros((8001, 4), dtype=np.uint8)
    for altitude in range(8001):
        color = elevation_color(float(altitude))
        lut[altitude] = (color.blue(), color.green(), color.red(), color.alpha())
    return lut


_ELEVATION_LUT: np.ndarray | None = None


def draw_terrain(
    painter: QPainter,
    terrain: TerrainLayer,
    projector: Projector,
    dish_radius_px: float,
    opacity: float = 0.34,
) -> None:
    """把高程栅格按色带着色后铺在陆地上。

    做法是把栅格转成一张小图，再平滑放大——比逐个像素投影快得多。
    由于雷达视野尺度下经纬度到屏幕的映射近似线性，直接用图像缩放即可，
    无须逐像素反向投影。

    海平面以下（高程为负）留透明，海洋因此不会被着色。
    """
    global _ELEVATION_LUT

    if terrain.rows < 2 or terrain.cols < 2 or not terrain.has_land:
        return

    elevations = terrain.elevations
    rows, cols = terrain.rows, terrain.cols

    if _ELEVATION_LUT is None:
        _ELEVATION_LUT = _elevation_lut()

    # 查表上色：先夹到 0–8000，再整体取色
    indices = np.clip(elevations, 0, 8000).astype(np.int32)
    pixels = _ELEVATION_LUT[indices].copy()

    # 海平面以下透明；陆地上的 alpha 按 opacity 缩放
    above_sea = elevations > 0.0
    alpha = np.where(above_sea, int(255 * max(0.0, min(1.0, opacity))), 0).astype(np.uint8)
    pixels[:, :, 3] = alpha

    # QImage 不复制传入的缓冲区，必须 copy() 一份，否则 numpy 数组被回收后图像失效
    image = QImage(
        pixels.tobytes(), cols, rows, cols * 4, QImage.Format.Format_ARGB32
    ).copy()

    draw_geo_image(painter, image, terrain.lat_min, terrain.lat_max,
                   terrain.lon_min, terrain.lon_max, projector)


# ---------------------------------------------------------------------------
# 路网
# ---------------------------------------------------------------------------

#: 道路等级 → (颜色, 线宽)。用蓝色系，与"深墨绿陆地 + 藏蓝海洋"协调
_ROAD_STYLE: dict[str, tuple[str, float]] = {
    "motorway": ("#4A8FD4", 1.15),
    "trunk": ("#3E7BB8", 0.95),
    "primary": ("#3A6EA0", 0.80),
}


def draw_roads(
    painter: QPainter,
    roads: RoadLayer,
    projector: Projector,
    dish_radius_px: float,
) -> None:
    """绘制主要路网。

    按等级分三组画（高速最粗最亮），每组一条 ``QPainterPath``——
    比逐条 ``drawPolyline`` 少很多次调用，四千条折线也能一次画完。
    """
    if roads.is_empty:
        return

    paths: dict[str, QPainterPath] = {}
    project = projector.project

    for line, road_class in zip(roads.lines, roads.classes):
        path = paths.get(road_class)
        if path is None:
            path = QPainterPath()
            paths[road_class] = path
        first = True
        for lon, lat in line:
            x, y = project(lat, lon)
            if first:
                path.moveTo(x, y)
                first = False
            else:
                path.lineTo(x, y)

    painter.save()
    painter.setBrush(Qt.BrushStyle.NoBrush)
    for road_class in ("primary", "trunk", "motorway"):
        path = paths.get(road_class)
        if path is None or path.isEmpty():
            continue
        hex_color, width = _ROAD_STYLE[road_class]
        pen = QPen(_qc(hex_color, 0.55))
        pen.setWidthF(width)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        painter.drawPath(path)
    painter.restore()


# ---------------------------------------------------------------------------
# 云图
# ---------------------------------------------------------------------------

def draw_clouds(
    painter: QPainter,
    cloud: CloudField,
    projector: Projector,
    dish_radius_px: float,
    max_alpha: float = 0.20,
) -> None:
    """把云量场叠加成淡淡的白色云图。

    与地形同样的思路：云量场是一张 ``rows × cols`` 的小图，
    每个像素的白色透明度正比于云量，再平滑放大到雷达盘上。

    透明度刻意压得较低（云量满值也只有 0.20），因为它只是背景提示；
    真实天空满是云时雷达盘会整体泛白，那是正确的表现，但不该盖住航班。
    """
    if cloud.is_empty:
        return

    image = QImage(cloud.cols, cloud.rows, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    for row in range(cloud.rows):
        for col in range(cloud.cols):
            cover = max(0.0, min(100.0, cloud.values[row][col]))
            if cover < 8.0:
                continue
            # 用 1.4 次幂压一下薄云，让"晴/多云"的差别不至于太刺眼
            alpha = max_alpha * (cover / 100.0) ** 1.4
            image.setPixelColor(col, row, QColor(255, 255, 255, int(alpha * 255)))

    # 与海拔用同一套四边形映射，否则大视野下云团同样会与海岸线错位
    draw_geo_image(painter, image, cloud.lat_min, cloud.lat_max,
                   cloud.lon_min, cloud.lon_max, projector)


# ---------------------------------------------------------------------------
# 机场跑道
# ---------------------------------------------------------------------------

def draw_airports(
    painter: QPainter,
    airports: list[Airport],
    projector: Projector,
    theme: Theme,
) -> None:
    """把机场画成它本来的样子：跑道按真实方位与长度绘制。

    香港国际机场因此会显示为三条平行跑道——数据里就是三条
    （07C / 07L / 07R），端点坐标精确到小数点后六位。
    """
    if not airports:
        return

    painter.save()
    painter.setBrush(Qt.BrushStyle.NoBrush)

    for airport in airports:
        for runway in airport.runways:
            start = projector.project(runway.le_lat, runway.le_lon)
            end = projector.project(runway.he_lat, runway.he_lon)

            # 英尺 → 米 → 公里 → 像素。这里必须逐级换算：
            # 早期版本写成 width_ft * 0.3048 * px_per_km，把米当成公里用，
            # 宽度被放大 1000 倍（0.57 px 变成 573 px），整块雷达盘都被糊住。
            width_m = runway.width_ft * 0.3048
            width = width_m / 1000.0 * projector.px_per_km
            # 上下限都收一下：太细看不见，太粗显然是数据异常
            width = max(1.6, min(12.0, width))

            # 先画一条更粗的深色描边作为跑道路肩，再叠亮色道面，
            # 这样在深色底图上跑道边界清晰
            shoulder = QPen(_qc("#11161A", 0.75))
            shoulder.setWidthF(width + 1.6)
            shoulder.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(shoulder)
            painter.drawLine(QPointF(*start), QPointF(*end))

            surface = QPen(_qc("#9AA7A2", 0.80))
            surface.setWidthF(width)
            surface.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(surface)
            painter.drawLine(QPointF(*start), QPointF(*end))

    painter.restore()


# ---------------------------------------------------------------------------
# 城市名
# ---------------------------------------------------------------------------

def draw_places(
    painter: QPainter,
    places: list[Place],
    projector: Projector,
    theme: Theme,
    dish_radius_px: float,
) -> None:
    """标注城市名。

    调用方已按人口降序排好；这里逐个尝试放置，与已放下的标签重叠就跳过，
    保证人口更大的城市优先出现、雷达盘不会被文字糊满。
    """
    if not places:
        return

    center_x, center_y = projector.center_x, projector.center_y
    radius = projector.radius_px
    occupied: list[tuple[float, float, float, float]] = []

    painter.save()
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)

    for place in places:
        x, y = projector.project(place.lat, place.lon)
        distance = math.hypot(x - center_x, y - center_y)
        if distance > radius - 6.0:
            continue

        font = make_font(theme.font_minor, bold=place.is_major, mono=False)
        painter.setFont(font)
        metrics = QFontMetrics(font)
        text = place.label
        width = metrics.horizontalAdvance(text)
        height = metrics.height()

        box = (x + 5.0, y - height / 2.0, width + 4.0, height)
        if any(_overlaps(box, other) for other in occupied):
            continue
        occupied.append(box)

        rect = QRectF(box[0], box[1], box[2], box[3])

        # 大城市加一个小方点，便于在底图上定位
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(_qc(theme.highlight if place.is_major else theme.text_secondary, 0.75))
        painter.drawEllipse(QPointF(x, y), 2.0 if place.is_major else 1.4,
                            2.0 if place.is_major else 1.4)

        # 文字描边：底图深浅不一，纯文字会看不清
        painter.setPen(_qc(theme.window_bg, 0.72))
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            painter.drawText(rect.translated(dx, dy),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

        painter.setPen(_qc(theme.text_primary, 0.80) if not place.is_major
                      else _qc(theme.highlight, 0.88))
        painter.drawText(rect, Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, text)

    painter.restore()


def _overlaps(a: tuple[float, float, float, float],
              b: tuple[float, float, float, float]) -> bool:
    return not (
        a[0] + a[2] < b[0] or b[0] + b[2] < a[0]
        or a[1] + a[3] < b[1] or b[1] + b[3] < a[1]
    )


# ---------------------------------------------------------------------------
# 天气角标
# ---------------------------------------------------------------------------

def draw_weather_badge(
    painter: QPainter,
    weather: Weather,
    theme: Theme,
    width: int,
) -> None:
    """雷达盘下方的天气摘要：温度、风、云量。"""
    font = make_font(max(10, theme.font_minor), mono=True)
    painter.save()
    painter.setFont(font)

    text = weather.summary()
    metrics = QFontMetrics(font)
    text_width = metrics.horizontalAdvance(text)
    height = metrics.height()
    pad = 8.0

    left = (width - text_width) / 2.0 - pad
    top = 42.0
    rect = QRectF(left, top, text_width + pad * 2, height + pad * 0.6)

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(_qc(theme.panel_bg, 0.72))
    painter.drawRoundedRect(rect, 4.0, 4.0)

    pen = QPen(_qc(theme.border, 0.85))
    pen.setWidthF(1.0)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawRoundedRect(rect, 4.0, 4.0)

    painter.setPen(_qc(theme.text_primary, 0.92))
    painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, text)
    painter.restore()


def draw_weather_arrow(
    painter: QPainter,
    weather: Weather,
    theme: Theme,
    cx: float,
    cy: float,
) -> None:
    """盘心位置的风向箭头。

    箭头指向风的**去向**（气象上风向指风的来向，这里刻意画去向，
    与"风吹向哪"的直觉一致），长度随风速变化。
    """
    painter.save()
    painter.translate(cx, cy)

    length = min(26.0, 10.0 + weather.wind_speed_kmh * 0.35)
    # 风向为来向，加 180° 得到去向；屏幕 0° 在正上方
    angle = math.radians(weather.wind_direction_deg + 180.0)

    tip_x = length * math.sin(angle)
    tip_y = -length * math.cos(angle)

    pen = QPen(_qc(theme.highlight, 0.62))
    pen.setWidthF(1.2)
    pen.setCosmetic(True)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawLine(QPointF(0, 0), QPointF(tip_x, tip_y))

    # 箭头
    head = 4.5
    for offset in (150.0, -150.0):
        radians = angle + math.radians(offset)
        painter.drawLine(
            QPointF(tip_x, tip_y),
            QPointF(tip_x + head * math.sin(radians), tip_y - head * math.cos(radians)),
        )
    painter.restore()
