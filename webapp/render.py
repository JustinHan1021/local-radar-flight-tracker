"""服务端底图预渲染：用 PIL 把矢量底图与覆盖层画成一张方形 PNG。

桌面版把同样的图层画在 ``app/ui/radar_overlays.py`` 里，走的是 QPainter。
服务端跑在 Linux 上、不装 PySide6，因此这里用 PIL 按同一套配色与透明度重画一遍，
绘制顺序也照抄桌面版：盘底 → 海洋 → 陆地 → 海岸线 → 地形 → 路网 → 云图 →
城市名 → 跑道。

**只画静态地理要素。** 量程环、罗盘、十字线、扫描线、航班图标、时钟一律留给客户端：
它们要么每帧都在变，要么必须和扫描线对齐，烤进底图既浪费带宽也做不到。

合成一律走 ``Image.alpha_composite``，不用 ``paste(mask=...)``：
后者把 alpha 通道也当普通通道做加权平均，半透明层叠几次就会把画布自己的
alpha 一起磨掉（0.5 叠一次就变成 0.75 而不是 1.0），而这张图整体是靠
"底色不透明 + 各层半透明"叠出来的，磨掉就全乱了。

PIL 的绘图原语没有抗锯齿，线宽也只能取整数，所以亚像素宽度（如海岸线 0.8、
高速 1.15、主干 0.80）会被取整到 1 像素：这三档道路因此在像素上一样粗，
只剩颜色能区分。这是与 Qt 渲染可见的差别。
"""

from __future__ import annotations

import colorsys
import io
import math

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from shapely.geometry import GeometryCollection, MultiPolygon, Point, Polygon, box

from app.airports import AirportDatabase
from app.basemap import DATA_DIR, DATA_FILES, parse_coastline, parse_land
from app.geo import Projector
from app.places import PlaceDatabase
from app.roads import fetch_roads
from app.terrain import fetch_terrain
from app.weather import fetch_cloud_field

#: 可选覆盖层白名单。基础地理（海洋 + 陆地 + 海岸线）不在此列，始终绘制。
LAYERS = ("terrain", "roads", "clouds", "places", "airports")

# ---------- 配色：地理要素色取自 app/theme.py，务必与桌面版保持一致 ----------
_SEA = "#0F1B2A"
_LAND = "#16241C"
_COASTLINE = "#6C7B7F"
_RADAR_DISH = "#0D1512"
_WINDOW_BG = "#0B0E0F"
_HIGHLIGHT = "#5AFFC2"
_TEXT_SECONDARY = "#6E7F7A"

#: 背景档位 → 提亮百分比。与 app/theme.py 的 _BG_LIFT 一致。
_BG_LIFT = {0: 100, 1: 170, 2: 240}

# ---------- 各层不透明度：与桌面版 radar_overlays 的实参一一对应 ----------
_SEA_ALPHA = 0.55
_LAND_ALPHA = 0.75
_COASTLINE_ALPHA = 0.45
_COASTLINE_WIDTH = 0.8
_TERRAIN_ALPHA = 0.34
_ROADS_ALPHA = 0.55
_CLOUDS_MAX_ALPHA = 0.20
_CLOUDS_MIN_COVER = 8.0
_PLACES_ALPHA = 0.80
_RUNWAY_SHOULDER = "#11161A"
_RUNWAY_SURFACE = "#9AA7A2"
_RUNWAY_SHOULDER_ALPHA = 0.75
_RUNWAY_SURFACE_ALPHA = 0.80

#: 道路等级 → (颜色, 线宽)。与 radar_overlays 的 _ROAD_STYLE 一致。
_ROAD_STYLE: dict[str, tuple[str, float]] = {
    "motorway": ("#4A8FD4", 1.15),
    "trunk": ("#3E7BB8", 0.95),
    "primary": ("#3A6EA0", 0.80),
}

#: 城市名字号。取主题的 medium 档，与桌面版同档；不随 size 缩放——
#: 底图是位图，客户端放大显示时字号本来就跟着放大。
_PLACE_FONT_PX = 13

#: 包围盒外扩比例，与 app/basemap.py 一致：留出余量，海岸线不会在盘边生硬断头
_BBOX_PADDING = 0.2

#: 栅格贴图的分块数。桌面版也取 5：块内用双线性近似真实投影，
#: 再多收益很小，而投影次数按块数平方增长。
_GEO_IMAGE_TILES = 5

#: 估算"经纬度矩形投影外包框"时每条边的采样点数
_EDGE_SAMPLES = 5

#: 高程色带上限：再高也归到最高一档
_ELEVATION_MAX = 8000

#: 高程色带（米, RGB）。**逐字照抄** app/terrain.py 的 _ELEVATION_STOPS：
#: 服务端没有 Qt，桌面版返回 QColor 的 elevation_color() 用不了，只能重写一份。
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

#: Pillow 9.1 起把常量收进了枚举；这里做兼容取值，两个版本都能跑。
_QUAD = getattr(Image, "Transform", Image).QUAD
_BILINEAR = getattr(Image, "Resampling", Image).BILINEAR

#: 字体候选：先微软雅黑（与桌面版一致），服务端退回 Noto / DejaVu。
#: 只有前两个带中文字形；全都找不到时退回内置位图字体，中文会缺字。
_FONT_CANDIDATES = ("msyh.ttc", "NotoSansCJK-Regular.ttc", "DejaVuSans.ttf", "Arial.ttf")


# ---------------------------------------------------------------------------
# 颜色工具
# ---------------------------------------------------------------------------

def _rgb(hex_color: str) -> tuple[int, int, int]:
    """``#RRGGBB`` → ``(r, g, b)``。"""
    text = hex_color.lstrip("#")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _lift_hex(hex_color: str, level: int) -> tuple[int, int, int]:
    """按背景档位提亮颜色。

    等价于桌面版的 ``QColor.lighter()``：乘的是 **HSL 亮度**而不是 RGB 分量。
    照抄这一点，Web 端的盘底才会和桌面版同一档位下的观感一致。
    """
    red, green, blue = _rgb(hex_color)
    level = max(0, min(2, int(level)))
    if level == 0:
        return red, green, blue

    hue, lightness, saturation = colorsys.rgb_to_hls(
        red / 255.0, green / 255.0, blue / 255.0
    )
    lightness = min(1.0, lightness * _BG_LIFT[level] / 100.0)
    lifted = colorsys.hls_to_rgb(hue, lightness, saturation)
    return tuple(round(value * 255.0) for value in lifted)  # type: ignore[return-value]


def _alpha_lut(fraction: float) -> list[int]:
    """整层不透明度的查表：把一层的 alpha 统一乘上 ``fraction``。

    返回 list 而不是 bytes：``Image.point()`` 对"序列"的判定只认 list / tuple。
    """
    scale = max(0.0, min(1.0, fraction))
    return [min(255, int(value * scale)) for value in range(256)]


def _scale_alpha(layer: Image.Image, fraction: float) -> Image.Image:
    """就地缩放整层的 alpha（原地修改并返回，便于链式调用）。"""
    layer.putalpha(layer.getchannel("A").point(_alpha_lut(fraction)))
    return layer


def _stroke_width(width: float) -> int:
    """PIL 的线宽只能是整数像素，所以亚像素宽度会取整。"""
    return max(1, int(round(width)))


def _overlaps(a: tuple[float, float, float, float],
              b: tuple[float, float, float, float]) -> bool:
    """两个 ``(x, y, 宽, 高)`` 矩形是否相交。"""
    return not (
        a[0] + a[2] < b[0] or b[0] + b[2] < a[0]
        or a[1] + a[3] < b[1] or b[1] + b[3] < a[1]
    )


# ---------------------------------------------------------------------------
# 数据（进程内单例）
# ---------------------------------------------------------------------------

_VECTORS: tuple[list, list] | None = None
_PLACE_DB: PlaceDatabase | None = None
_AIRPORT_DB: AirportDatabase | None = None
_FONT_CACHE: dict[int, ImageFont.ImageFont] = {}
_ELEVATION_LUT: np.ndarray | None = None


def _vectors() -> tuple[list, list]:
    """陆地与海岸线矢量，返回 ``(陆地面, 海岸线环)``。

    复用 ``app.basemap`` 的 ``parse_land`` / ``parse_coastline``：它们与 Qt 无关，
    可以直接拿来用；桌面版那份把结果投影成 QPainterPath 的缓存这里用不上。
    数据没下载时两个解析函数都返回空列表，图层静默跳过即可。
    """
    global _VECTORS
    if _VECTORS is None:
        _VECTORS = (
            parse_land(DATA_DIR / DATA_FILES["land"]),
            parse_coastline(DATA_DIR / DATA_FILES["coastline"]),
        )
    return _VECTORS


def _place_database() -> PlaceDatabase:
    """地名库单例。七千个城点加 pickle 缓存，每次请求重建纯属浪费。"""
    global _PLACE_DB
    if _PLACE_DB is None:
        database = PlaceDatabase()
        database.load()
        _PLACE_DB = database
    return _PLACE_DB


def _airport_database() -> AirportDatabase:
    """机场库单例（含跑道）。理由同上：八万多个机场，不可能每个请求解析一次。"""
    global _AIRPORT_DB
    if _AIRPORT_DB is None:
        database = AirportDatabase()
        database.load()
        _AIRPORT_DB = database
    return _AIRPORT_DB


def _place_font() -> ImageFont.ImageFont:
    """取一个能画中文的字体；找不到就退回内置位图字体。"""
    cached = _FONT_CACHE.get(_PLACE_FONT_PX)
    if cached is not None:
        return cached

    font: ImageFont.ImageFont | None = None
    for name in _FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(name, _PLACE_FONT_PX)
            break
        except OSError:
            continue
    if font is None:
        try:
            font = ImageFont.load_default(size=_PLACE_FONT_PX)
        except TypeError:  # Pillow < 10.1 的 load_default() 不接受字号
            font = ImageFont.load_default()

    _FONT_CACHE[_PLACE_FONT_PX] = font
    return font


# ---------------------------------------------------------------------------
# 高程色带
# ---------------------------------------------------------------------------

def _elevation_rgb(elevation_m: float) -> tuple[int, int, int]:
    """高程 → RGB（低绿 → 高红），线性插值，与桌面版色带完全一致。"""
    stops = _ELEVATION_STOPS
    if elevation_m <= stops[0][0]:
        return stops[0][1]
    if elevation_m >= stops[-1][0]:
        return stops[-1][1]

    for index in range(len(stops) - 1):
        low_alt, low_color = stops[index]
        high_alt, high_color = stops[index + 1]
        if low_alt <= elevation_m <= high_alt:
            ratio = (elevation_m - low_alt) / max(1e-9, high_alt - low_alt)
            return tuple(  # type: ignore[return-value]
                int(low + (high - low) * ratio)
                for low, high in zip(low_color, high_color)
            )
    return stops[-1][1]


def _elevation_lut() -> np.ndarray:
    """高程 → RGBA 的查找表（0–8000 m，每米一项）。

    逐像素调 Python 函数上色太慢——一张 400×400 的栅格就是 16 万次调用。
    先查表算出整块像素，再交给 PIL 一次性构造图像。
    """
    global _ELEVATION_LUT
    if _ELEVATION_LUT is None:
        table = np.zeros((_ELEVATION_MAX + 1, 4), dtype=np.uint8)
        for altitude in range(_ELEVATION_MAX + 1):
            red, green, blue = _elevation_rgb(float(altitude))
            table[altitude] = (red, green, blue, 255)
        _ELEVATION_LUT = table
    return _ELEVATION_LUT


# ---------------------------------------------------------------------------
# 几何工具
# ---------------------------------------------------------------------------

def _iter_polygons(geometry):
    """把 shapely 几何展开成一个个 Polygon。

    ``intersection`` 的结果可能是 GeometryCollection（含退化成的线段与点），
    这里只取真正的多边形部分。内环（洞）必须跟着外环一起保留——
    拆散之后洞会被当成实心陆地填上。
    """
    if geometry is None or geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
    elif isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for part in geometry.geoms:
            yield from _iter_polygons(part)


def _project_ring(coords, project) -> list[tuple[float, float]]:
    """把经纬度坐标序列投影成像素点列。GeoJSON 的坐标是 ``(经度, 纬度)``。"""
    return [project(lat, lon) for lon, lat in coords]


# ---------------------------------------------------------------------------
# 各图层的绘制：每个函数接一张画布，返回叠好之后的画布
# ---------------------------------------------------------------------------

def _base_plate(side: int, bg_level: int) -> Image.Image:
    """盘底：雷达盘底色按背景档位提亮后的实色。

    服务端出一张**不透明**的方形图，客户端按圆裁完直接铺在盘下就行，
    不必再操心它底下是什么颜色——bg_level 因此跟着请求走。
    方形四角在圆外，裁掉即可。
    """
    return Image.new("RGBA", (side, side),
                     _lift_hex(_RADAR_DISH, bg_level) + (255,))


def _draw_sea(canvas: Image.Image) -> Image.Image:
    """铺海洋底色。整块方形都铺，圆外的部分由客户端裁掉。"""
    layer = Image.new("RGBA", canvas.size, _rgb(_SEA) + (int(round(_SEA_ALPHA * 255)),))
    return Image.alpha_composite(canvas, layer)


def _rasterize_land(mask: Image.Image, geometries: list, projector: Projector) -> None:
    """把陆地画进掩膜：外环填 255，内环（洞）填回 0。

    用掩膜而不是直接填多边形，是因为 GeoJSON 的陆地自带内环——
    直接填会把湖泊、内海一起涂成陆地色。

    裁剪交给 shapely 的 ``intersection(box)``：它会保留多边形的闭合性与内环。
    改用"只取视口内可见的点段"是不行的：多边形一被截断就不再代表原来的区域，
    填充会塌成一条薄带，大片陆地漏成海洋色。
    """
    draw = ImageDraw.Draw(mask)
    lat_min, lat_max, lon_min, lon_max = projector.bbox(_BBOX_PADDING)
    clip = box(lon_min, lat_min, lon_max, lat_max)
    project = projector.project
    touched = False

    for geometry in geometries:
        # 先用包围盒快速排除，避免对每个大洲都做一次昂贵的 intersection
        gx_min, gy_min, gx_max, gy_max = geometry.bounds
        if gx_max < lon_min or gx_min > lon_max or gy_max < lat_min or gy_min > lat_max:
            continue

        clipped = geometry.intersection(clip)
        if clipped.is_empty:
            continue
        touched = True

        for polygon in _iter_polygons(clipped):
            exterior = _project_ring(polygon.exterior.coords, project)
            if len(exterior) < 3:
                continue
            draw.polygon(exterior, fill=255)
            for interior in polygon.interiors:
                hole = _project_ring(interior.coords, project)
                if len(hole) >= 3:
                    draw.polygon(hole, fill=0)

    if touched:
        return

    # 视口内一条陆地边界都没有：可能整块都落在陆地内部（例如在北京看 50 km），
    # 这时整张图都要填成陆地色
    center = Point(projector.center_lon, projector.center_lat)
    if any(geometry.contains(center) for geometry in geometries):
        draw.rectangle((0, 0, mask.width, mask.height), fill=255)


def _draw_land(canvas: Image.Image, projector: Projector) -> Image.Image:
    """填陆地。数据没下载时这一层直接跳过，海洋照画。"""
    land, _ = _vectors()
    mask = Image.new("L", canvas.size, 0)
    if land:
        _rasterize_land(mask, land, projector)

    layer = Image.new("RGBA", canvas.size, _rgb(_LAND) + (255,))
    layer.putalpha(mask.point(_alpha_lut(_LAND_ALPHA)))
    return Image.alpha_composite(canvas, layer)


def _draw_coastline(canvas: Image.Image, projector: Projector) -> Image.Image:
    """描海岸线。

    折线整体投影——先用经纬度包围盒把整条都与视口无关的环剔掉；海岸线的
    每条 LineString 都不长（1:10m 大约八千条共四十多万点），剔除后剩下的很少。
    """
    _, coastline = _vectors()
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))

    if coastline:
        draw = ImageDraw.Draw(layer)
        fill = _rgb(_COASTLINE) + (255,)
        bounds = projector.bbox(_BBOX_PADDING)
        project = projector.project
        for ring in coastline:
            if (ring.lat_max < bounds[0] or ring.lat_min > bounds[1]
                    or ring.lon_max < bounds[2] or ring.lon_min > bounds[3]):
                continue
            points = [project(float(lat), float(lon))
                      for lat, lon in zip(ring.lats, ring.lons)]
            if len(points) >= 2:
                draw.line(points, fill=fill, width=_stroke_width(_COASTLINE_WIDTH))

    _scale_alpha(layer, _COASTLINE_ALPHA)
    return Image.alpha_composite(canvas, layer)


def _terrain_raster(terrain) -> Image.Image:
    """高程栅格 → RGBA 小图。海平面以下完全透明，海洋因此不会被着色。"""
    elevations = terrain.elevations
    indices = np.clip(elevations, 0, _ELEVATION_MAX).astype(np.int32)
    pixels = _elevation_lut()[indices]
    alpha = np.where(elevations > 0.0,
                     int(round(_TERRAIN_ALPHA * 255)), 0).astype(np.uint8)
    pixels[:, :, 3] = alpha
    return Image.fromarray(pixels, "RGBA")


def _draw_terrain(canvas: Image.Image, projector: Projector,
                  lat: float, lon: float, radius_km: float) -> Image.Image:
    """给陆地上海拔色。瓦片取不到（网络失败 / 该区域没数据）就直接不画。"""
    terrain = fetch_terrain(lat, lon, radius_km)
    if terrain is None or terrain.rows < 2 or terrain.cols < 2 or not terrain.has_land:
        return canvas
    return _project_geo_image(canvas, _terrain_raster(terrain),
                              terrain.lat_min, terrain.lat_max,
                              terrain.lon_min, terrain.lon_max, projector)


def _draw_roads(canvas: Image.Image, projector: Projector,
                lat: float, lon: float, radius_km: float) -> Image.Image:
    """画主要路网。

    按等级分三组、由细到粗地叠：高速压在主干之上，交汇处才看得出等级。
    视野太大时 fetch_roads 返回空层（那种尺度下路网只是噪声）。
    """
    roads = fetch_roads(lat, lon, radius_km)
    if roads is None or roads.is_empty:
        return canvas

    grouped: dict[str, list] = {}
    for line, road_class in zip(roads.lines, roads.classes):
        grouped.setdefault(road_class, []).append(line)

    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    project = projector.project

    for road_class in ("primary", "trunk", "motorway"):
        lines = grouped.get(road_class)
        if not lines:
            continue
        hex_color, width = _ROAD_STYLE[road_class]
        fill = _rgb(hex_color) + (255,)
        for line in lines:
            points = [project(line_lat, line_lon) for line_lon, line_lat in line]
            if len(points) >= 2:
                draw.line(points, fill=fill, width=_stroke_width(width))

    _scale_alpha(layer, _ROADS_ALPHA)
    return Image.alpha_composite(canvas, layer)


def _cloud_raster(cloud) -> Image.Image:
    """云量场 → 半透明白色小图。

    **要上下翻一次**：``CloudField`` 的第 0 行是南边（``lat_min``），
    而栅格贴图约定第 0 行是北边。桌面版漏了这一步（``radar_overlays.draw_clouds``），
    云图在那边是上下颠倒的；这里按正确方向出图。
    """
    pixels = bytearray(cloud.cols * cloud.rows * 4)
    for row in range(cloud.rows):
        for col in range(cloud.cols):
            cover = max(0.0, min(100.0, cloud.values[row][col]))
            if cover < _CLOUDS_MIN_COVER:
                continue
            # 用 1.4 次幂压一下薄云，让"晴 / 多云"的差别不至于太刺眼
            alpha = int(round(255 * _CLOUDS_MAX_ALPHA * (cover / 100.0) ** 1.4))
            offset = ((cloud.rows - 1 - row) * cloud.cols + col) * 4
            pixels[offset:offset + 4] = bytes((255, 255, 255, alpha))
    return Image.frombytes("RGBA", (cloud.cols, cloud.rows), bytes(pixels))


def _draw_clouds(canvas: Image.Image, projector: Projector,
                 lat: float, lon: float, radius_km: float) -> Image.Image:
    """叠一层淡淡的云图。取不到云量（网络失败）就不画。"""
    cloud = fetch_cloud_field(lat, lon, radius_km)
    if cloud is None or cloud.is_empty:
        return canvas
    return _project_geo_image(canvas, _cloud_raster(cloud),
                              cloud.lat_min, cloud.lat_max,
                              cloud.lon_min, cloud.lon_max, projector)


def _draw_places(canvas: Image.Image, projector: Projector,
                 lat: float, lon: float, radius_km: float) -> Image.Image:
    """标注城市名。

    ``visible()`` 已按人口降序排好并限了总数（26 个）；这里逐个尝试放置，
    与已放下的标签重叠就跳过，保证人口更大的城市优先出现。
    """
    database = _place_database()
    places = database.visible(lat, lon, radius_km) if database.is_loaded else []
    if not places:
        return canvas

    font = _place_font()
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    highlight = _rgb(_HIGHLIGHT) + (255,)
    secondary = _rgb(_TEXT_SECONDARY) + (255,)
    outline = _rgb(_WINDOW_BG) + (255,)
    occupied: list[tuple[float, float, float, float]] = []
    center_x, center_y = projector.center_x, projector.center_y
    radius_px = projector.radius_px

    for place in places:
        x, y = projector.project(place.lat, place.lon)
        # 贴着盘边的名字会被圆裁掉一半，宁可不画
        if math.hypot(x - center_x, y - center_y) > radius_px - 6.0:
            continue

        text = place.label
        if not text.strip():
            continue
        box_left, box_top, box_right, box_bottom = draw.textbbox((0, 0), text, font=font)
        text_width = box_right - box_left
        text_height = box_bottom - box_top
        if text_width <= 0 or text_height <= 0:
            continue

        rect = (x + 5.0, y - text_height / 2.0, text_width + 4.0, text_height)
        if any(_overlaps(rect, other) for other in occupied):
            continue
        occupied.append(rect)

        # 大城市用亮青色，其余用次要文字色；点也大一圈
        colour = highlight if place.is_major else secondary
        dot = 2.0 if place.is_major else 1.4
        draw.ellipse((x - dot, y - dot, x + dot, y + dot), fill=colour)

        # 底图深浅不一，纯文字会看不清：先描一圈深色再叠字
        text_x = x + 5.0 - box_left
        text_y = y - text_height / 2.0 - box_top
        for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            draw.text((text_x + dx, text_y + dy), text, font=font, fill=outline)
        draw.text((text_x, text_y), text, font=font, fill=colour)

    _scale_alpha(layer, _PLACES_ALPHA)
    return Image.alpha_composite(canvas, layer)


def _draw_airports(canvas: Image.Image, projector: Projector,
                   lat: float, lon: float, radius_km: float) -> Image.Image:
    """把机场画成它本来的样子：跑道按真实方位与长度绘制。

    香港国际机场因此会显示为三条平行跑道——数据里就是三条（07C / 07L / 07R）。
    """
    database = _airport_database()
    airports = database.nearby(lat, lon, radius_km) if database.is_loaded else []
    if not airports:
        return canvas

    shoulder = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    surface = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    draw_shoulder = ImageDraw.Draw(shoulder)
    draw_surface = ImageDraw.Draw(surface)
    shoulder_fill = _rgb(_RUNWAY_SHOULDER) + (255,)
    surface_fill = _rgb(_RUNWAY_SURFACE) + (255,)

    for airport in airports:
        for runway in airport.runways:
            start = projector.project(runway.le_lat, runway.le_lon)
            end = projector.project(runway.he_lat, runway.he_lon)

            # 英尺 → 米 → 公里 → 像素。这里必须逐级换算：把米当成公里用会把宽度
            # 放大 1000 倍（0.57 px 变成 573 px），整块雷达盘都被糊住。
            width = runway.width_ft * 0.3048 / 1000.0 * projector.px_per_km
            width = max(1.6, min(12.0, width))

            # 先描一条更粗的深色道肩，再叠亮色道面：
            # 深色底图上只有这样跑道边界才清晰
            draw_shoulder.line([start, end], fill=shoulder_fill,
                               width=_stroke_width(width + 1.6))
            draw_surface.line([start, end], fill=surface_fill,
                              width=_stroke_width(width))

    _scale_alpha(shoulder, _RUNWAY_SHOULDER_ALPHA)
    _scale_alpha(surface, _RUNWAY_SURFACE_ALPHA)
    return Image.alpha_composite(Image.alpha_composite(canvas, shoulder), surface)


# ---------------------------------------------------------------------------
# 栅格贴图
# ---------------------------------------------------------------------------

def _source_quad(image: Image.Image,
                 rect: tuple[int, int, int, int],
                 lat_min: float, lat_max: float,
                 lon_min: float, lon_max: float,
                 projector: Projector) -> tuple[float, ...]:
    """算出目的矩形四角对应的**源像素坐标**（PIL 的 QUAD 变换要的就是这个）。

    顺序必须是左上、左下、右下、右上——顺序错了图像要么翻过来要么拧起来。
    """
    left, top, width, height = rect
    span_lat = max(1e-9, lat_max - lat_min)
    span_lon = max(1e-9, lon_max - lon_min)
    quad: list[float] = []

    for x, y in ((left, top), (left, top + height),
                 (left + width, top + height), (left + width, top)):
        point = projector.unproject(float(x), float(y))
        # unproject 的经度归一化到了 [0, 360)，换算回 ±180 才能和栅格范围比较
        point_lon = ((point.lon + 180.0) % 360.0) - 180.0
        frac_x = max(0.0, min(1.0, (point_lon - lon_min) / span_lon))
        frac_y = max(0.0, min(1.0, (lat_max - point.lat) / span_lat))
        quad.append(frac_x * (image.width - 1))
        quad.append(frac_y * (image.height - 1))

    return tuple(quad)


def _overlay(canvas: Image.Image, image: Image.Image,
             origin: tuple[int, int]) -> Image.Image:
    """把一张 RGBA 小图叠到画布上（正确的 over 合成）。

    先裁到画布范围内再贴：越界矩形交给 PIL 的 paste 不保险，自己裁更稳。
    """
    left = max(0, origin[0])
    top = max(0, origin[1])
    right = min(canvas.width, origin[0] + image.width)
    bottom = min(canvas.height, origin[1] + image.height)
    if right <= left or bottom <= top:
        return canvas

    cropped = image.crop((left - origin[0], top - origin[1],
                          right - origin[0], bottom - origin[1]))
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    layer.paste(cropped, (left, top))
    return Image.alpha_composite(canvas, layer)


def _projected_bbox(lat_min: float, lat_max: float,
                    lon_min: float, lon_max: float,
                    projector: Projector) -> tuple[int, int, int, int]:
    """经纬度矩形投影后的外包框，返回 ``(左, 上, 宽, 高)``。

    只看四个角会低估范围——矩形投影出来边是**弯的**，极值点落在边的中段，
    因此沿四条边各采样几个点取极值。
    """
    xs: list[float] = []
    ys: list[float] = []
    for index in range(_EDGE_SAMPLES):
        frac = index / (_EDGE_SAMPLES - 1)
        for lat, lon in (
            (lat_min + (lat_max - lat_min) * frac, lon_min),
            (lat_min + (lat_max - lat_min) * frac, lon_max),
            (lat_min, lon_min + (lon_max - lon_min) * frac),
            (lat_max, lon_min + (lon_max - lon_min) * frac),
        ):
            x, y = projector.project(lat, lon)
            xs.append(x)
            ys.append(y)

    left = int(round(min(xs)))
    top = int(round(min(ys)))
    return left, top, int(round(max(xs))) - left, int(round(max(ys))) - top


def _project_geo_image(canvas: Image.Image, image: Image.Image,
                       lat_min: float, lat_max: float,
                       lon_min: float, lon_max: float,
                       projector: Projector) -> Image.Image:
    """把一张按经纬度铺开的栅格贴到画布上（第 0 行是北、第 0 列是西）。

    极坐标等距投影下，一个经纬度矩形投影出来是**弯曲的梯形**。整块按矩形缩放
    会整体错位（460 km 视野下能偏出二十多像素，海拔色块与海岸线明显对不上），
    所以桌面版把图像切成 5×5 块、逐块做四边形映射；这里照做同样的分块，
    只是方向相反：桌面版把源块映射到投影出的四边形，PIL 的 QUAD 只能把
    "目的矩形 → 源四边形"，于是反过来算——每块的目的矩形四角反投影回经纬度，
    换成源像素坐标当四边形，块内仍是双线性近似，误差量与桌面版同阶。
    """
    if image.width < 2 or image.height < 2:
        return canvas

    left, top, width, height = _projected_bbox(lat_min, lat_max, lon_min, lon_max,
                                               projector)
    if width < _GEO_IMAGE_TILES or height < _GEO_IMAGE_TILES:
        return canvas

    tiles = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    for row in range(_GEO_IMAGE_TILES):
        cell_top = height * row // _GEO_IMAGE_TILES
        cell_bottom = height * (row + 1) // _GEO_IMAGE_TILES
        for col in range(_GEO_IMAGE_TILES):
            cell_left = width * col // _GEO_IMAGE_TILES
            cell_right = width * (col + 1) // _GEO_IMAGE_TILES
            cell_width = cell_right - cell_left
            cell_height = cell_bottom - cell_top
            if cell_width < 1 or cell_height < 1:
                continue
            rect = (left + cell_left, top + cell_top, cell_width, cell_height)
            quad = _source_quad(image, rect, lat_min, lat_max, lon_min, lon_max,
                                projector)
            warped = image.transform((cell_width, cell_height), _QUAD, quad, _BILINEAR)
            tiles.paste(warped, (cell_left, cell_top))

    return _overlay(canvas, tiles, (left, top))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _draw_optional(draw, canvas: Image.Image, *args) -> Image.Image:
    """调用一个可选图层，任何异常都按"这一层没有"处理。

    数据还没下载、网络不通、瓦片损坏——这些都只该让那一层消失，不该让整个
    底图请求失败：底图只是方位参照，缺一层雷达照样能用。
    """
    try:
        return draw(canvas, *args)
    except Exception:  # noqa: BLE001
        return canvas


def render_basemap(lat: float, lon: float, radius_km: float, size: int,
                   layers: frozenset[str], *, bg_level: int = 1) -> bytes:
    """渲染 ``(lat, lon)`` 半径 ``radius_km`` 投影方框的 ``size × size`` PNG。

    ``layers`` 只控制可选覆盖层，白名单（:data:`LAYERS`）之外的值一律忽略；
    基础地理（海洋 + 陆地 + 海岸线）始终绘制，不受它影响。
    调用方应已校验过参数并对非法值回 400，这里仍会再交集一次白名单。
    """
    side = max(1, int(size))
    projector = Projector(
        center_lat=lat,
        center_lon=lon,
        radius_km=radius_km,
        center_x=side / 2.0,
        center_y=side / 2.0,
        radius_px=side / 2.0,
    )
    wanted = frozenset(layers or ()) & frozenset(LAYERS)

    canvas = _base_plate(side, bg_level)
    canvas = _draw_sea(canvas)
    canvas = _draw_land(canvas, projector)
    canvas = _draw_coastline(canvas, projector)

    if "terrain" in wanted:
        canvas = _draw_optional(_draw_terrain, canvas, projector, lat, lon, radius_km)
    if "roads" in wanted:
        canvas = _draw_optional(_draw_roads, canvas, projector, lat, lon, radius_km)
    if "clouds" in wanted:
        canvas = _draw_optional(_draw_clouds, canvas, projector, lat, lon, radius_km)
    if "places" in wanted:
        canvas = _draw_optional(_draw_places, canvas, projector, lat, lon, radius_km)
    if "airports" in wanted:
        canvas = _draw_optional(_draw_airports, canvas, projector, lat, lon, radius_km)

    buffer = io.BytesIO()
    canvas.save(buffer, format="PNG")
    return buffer.getvalue()
