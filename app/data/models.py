"""航班数据模型。

一个 :class:`Flight` 由三部分信息合成：

1. **实时遥测**——由数据源提供（位置、高度、地速、航向、垂直速率）；
2. **富化信息**——由 adsbdb / hexdb 补齐（机型、生产商、注册号、航线）；
3. **本地知识**——由 :mod:`app.data.registry` 判定（宽体、特殊机型/涂装）。

距离与方位一律在本地用 :mod:`app.geo` 重算，不采信数据源返回的
``dst`` / ``dir`` 字段：实测 adsb.lol 的 ``dst`` 单位是海里，
直接使用极易造成单位混用，本地重算还能保证与雷达投影完全同源。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.data.registry import airline_from_callsign
from app.data.registry import is_widebody as _is_widebody
from app.data.registry import manufacturer_url as _manufacturer_url
from app.data.registry import special_livery, special_type_note, utc_offset_hours
from app.geo import compass_name, haversine_km, initial_bearing

# 单位换算
M_PER_FOOT = 0.3048
KT_TO_KMH = 1.852

# 低于此速度不估算 ETA（滑行、等待或速度不可信）
_ETA_MIN_SPEED_KT = 50.0


@dataclass
class Route:
    """航线与承运人信息。字段全部来自 adsbdb，缺项留空而非编造。"""

    airline: str = ""
    airline_icao: str = ""
    airline_iata: str = ""
    icao_callsign: str = ""
    iata_callsign: str = ""

    origin_iata: str = ""
    origin_icao: str = ""
    origin_name: str = ""
    origin_country: str = ""
    origin_lat: float | None = None
    origin_lon: float | None = None

    destination_iata: str = ""
    destination_icao: str = ""
    destination_name: str = ""
    destination_country: str = ""
    destination_lat: float | None = None
    destination_lon: float | None = None

    @property
    def available(self) -> bool:
        """是否拿到了任何可用航线信息。"""
        return bool(self.origin_iata or self.destination_iata or self.airline)

    @property
    def label(self) -> str:
        """榜单用短标签，形如 ``PEK → SZX``。缺项以 ``—`` 占位。"""
        return f"{self.origin_iata or '—'} → {self.destination_iata or '—'}"


@dataclass
class AircraftInfo:
    """机身静态信息（变化很慢，适合长缓存）。"""

    registration: str = ""
    type_code: str = ""      # ICAO 机型代码，如 B789
    type_name: str = ""      # 机型全称，如 "787 9"
    manufacturer: str = ""   # 生产商，如 Boeing
    operator: str = ""       # 运营人
    photo_url: str = ""      # 实拍图地址

    @property
    def available(self) -> bool:
        return bool(self.registration or self.type_code or self.type_name)


@dataclass
class Flight:
    """一架航班在某时刻的完整状态。"""

    # ---- 实时遥测（由数据源提供）----
    icao24: str
    callsign: str = ""
    lat: float = 0.0
    lon: float = 0.0
    altitude_ft: float | None = None          # 气压高度
    geo_altitude_ft: float | None = None      # 几何高度
    ground_speed_kt: float | None = None
    track: float | None = None                # 真航向，度
    vertical_rate_fpm: float | None = None    # 英尺/分钟，正为爬升
    on_ground: bool = False
    category: str = ""                        # ADS-B 发射类别
    seen_pos: float = 0.0                     # 位置距今秒数，用于陈旧度判断
    squawk: str = ""
    origin_country: str = ""

    # ---- 富化信息 ----
    info: AircraftInfo = field(default_factory=AircraftInfo)
    route: Route | None = None

    # ---- 相对用户的几何量（本地计算）----
    distance_km: float = 0.0
    bearing: float = 0.0

    # ---- 平滑插值用：上一帧位置 ----
    prev_lat: float | None = None
    prev_lon: float | None = None

    # ------------------------------------------------------------------
    # 几何
    # ------------------------------------------------------------------

    def update_relative(self, center_lat: float, center_lon: float) -> None:
        """按用户位置刷新直线距离与方位角。"""
        self.distance_km = haversine_km(center_lat, center_lon, self.lat, self.lon)
        self.bearing = initial_bearing(center_lat, center_lon, self.lat, self.lon)

    @property
    def is_stale(self) -> bool:
        """位置数据是否已过期（秒）。由调用方与阈值比较。"""
        return self.seen_pos

    # ------------------------------------------------------------------
    # 单位与显示
    # ------------------------------------------------------------------

    @property
    def altitude_m(self) -> float | None:
        return None if self.altitude_ft is None else self.altitude_ft * M_PER_FOOT

    def display_altitude(self, unit: str = "m") -> str:
        """海拔文本，支持米/英尺切换。地面目标直接显示"地面"。"""
        if self.on_ground:
            return "地面"
        if self.altitude_ft is None:
            return "—"
        if unit == "ft":
            return f"{self.altitude_ft:,.0f} ft"
        return f"{self.altitude_m:,.0f} m"

    def display_altitude_short(self, unit: str = "m") -> str:
        """雷达图标签用的紧凑写法，如 ``3.2km`` / ``10500ft``。"""
        if self.on_ground:
            return "地面"
        if self.altitude_ft is None:
            return "—"
        if unit == "ft":
            return f"{self.altitude_ft / 1000:.1f}k ft" if self.altitude_ft >= 1000 else f"{self.altitude_ft:.0f} ft"
        meters = self.altitude_m or 0.0
        if meters >= 1000:
            return f"{meters / 1000:.1f}km"
        return f"{meters:.0f}m"

    def display_speed(self) -> str:
        """地速，单位为节（航空通用）。"""
        if self.ground_speed_kt is None:
            return "—"
        return f"{self.ground_speed_kt:,.0f} kt"

    def display_track(self) -> str:
        if self.track is None:
            return "—"
        return f"{self.track:03.0f}° {compass_name(self.track)}"

    def display_vertical_rate(self) -> str:
        if self.on_ground or self.vertical_rate_fpm is None:
            return "—"
        rate = self.vertical_rate_fpm
        if abs(rate) < 50:
            return "平飞"
        arrow = "↑" if rate > 0 else "↓"
        return f"{arrow} {abs(rate):,.0f} ft/min"

    def display_distance(self) -> str:
        if self.distance_km < 10:
            return f"{self.distance_km:.1f} km"
        return f"{self.distance_km:,.0f} km"

    def display_bearing(self) -> str:
        return f"{self.bearing:.1f}° {compass_name(self.bearing)}"

    # ------------------------------------------------------------------
    # 身份与标识
    # ------------------------------------------------------------------

    @property
    def icao_callsign(self) -> str:
        """ICAO 呼号；缺失时退回 icao24 地址。

        数据源偶尔用全 0 表示"该机没发呼号"（实测 adsb.lol 会返回 ``00000000``）。
        这种值非空，只靠 ``strip()`` 挡不住，会被当成真实航班号显示出来，
        所以这里显式判掉全 0。
        """
        callsign = self.callsign.strip()
        if not callsign or not callsign.strip("0"):
            return self.icao24.upper()
        return callsign

    @property
    def flight_number(self) -> str:
        """优先显示 IATA 航班号（乘客实际看到的），否则退回 ICAO 呼号。"""
        if self.route and self.route.iata_callsign:
            return self.route.iata_callsign
        return self.icao_callsign

    @property
    def registration(self) -> str:
        return self.info.registration or "—"

    @property
    def airline_name(self) -> str:
        """航司名：富化结果优先，缺失时按呼号前缀兜底。"""
        if self.route and self.route.airline:
            return self.route.airline
        if self.info.operator:
            return self.info.operator
        return airline_from_callsign(self.callsign)

    @property
    def model_name(self) -> str:
        """机型全称，如 ``787 9``；缺失时退回 ICAO 代码。"""
        return self.info.type_name or self.info.type_code or "—"

    @property
    def is_widebody(self) -> bool:
        return _is_widebody(self.info.type_code, self.category)

    @property
    def special_note(self) -> str | None:
        """特殊标识说明。特殊涂装优先于特殊机型。"""
        return special_livery(self.info.registration) or special_type_note(self.info.type_code)

    @property
    def manufacturer_url(self) -> str | None:
        """生产商官网；未收录生产商时返回 None（界面据此禁用点击）。"""
        return _manufacturer_url(self.info.manufacturer)

    @property
    def route_label(self) -> str:
        return self.route.label if self.route else "— → —"

    # ------------------------------------------------------------------
    # ETA 估算
    # ------------------------------------------------------------------

    def remaining_km_to_destination(self) -> float | None:
        """到目的地机场的剩余大圆距离。"""
        route = self.route
        if route is None or route.destination_lat is None or route.destination_lon is None:
            return None
        return haversine_km(self.lat, self.lon, route.destination_lat, route.destination_lon)

    def eta(self, now: datetime | None = None) -> tuple[datetime, float] | None:
        """估算到达目的地的时间，返回 ``(目的地当地到达时刻, 剩余公里数)``。

        免费数据源不提供航班时刻表，因此这里的"预计起降时间"只能是**推算值**：
        用当前地速去除剩余大圆距离得到剩余时间，再按目的地时区换算为当地时间。
        界面会明确标注这是估算，不冒充计划时刻。

        目的地坐标缺失、飞机在地面、或地速过低（滑行/等待）时返回 ``None``。
        """
        route = self.route
        if route is None or route.destination_lat is None or route.destination_lon is None:
            return None
        if self.on_ground:
            return None
        if not self.ground_speed_kt or self.ground_speed_kt < _ETA_MIN_SPEED_KT:
            return None

        remaining = haversine_km(
            self.lat, self.lon, route.destination_lat, route.destination_lon
        )
        speed_kmh = self.ground_speed_kt * KT_TO_KMH
        if speed_kmh <= 0:
            return None

        arrival_utc = (now or datetime.now(timezone.utc)) + timedelta(hours=remaining / speed_kmh)
        offset = utc_offset_hours(
            route.destination_country, route.destination_lat, route.destination_lon
        )
        return arrival_utc.astimezone(timezone(timedelta(hours=offset))), remaining

    def eta_text(self, now: datetime | None = None) -> str:
        """ETA 的单行文本，如 ``预计 14:32 到达 · 剩余 1,240 km``。"""
        result = self.eta(now)
        if result is None:
            if self.on_ground:
                return "已落地"
            if self.route is None or self.route.destination_lat is None:
                return "目的地未知"
            return "速度过低，暂不估算"
        arrival, remaining = result
        return f"预计 {arrival:%H:%M} 到达 · 剩余 {remaining:,.0f} km"

    def destination_local_offset_text(self) -> str:
        """目的地时区说明，如 ``UTC+8``。"""
        route = self.route
        if route is None or route.destination_lat is None:
            return ""
        offset = utc_offset_hours(
            route.destination_country, route.destination_lat, route.destination_lon
        )
        sign = "+" if offset >= 0 else "-"
        magnitude = abs(offset)
        hours = int(magnitude)
        minutes = int(round((magnitude - hours) * 60))
        suffix = f":{minutes:02d}" if minutes else ""
        return f"UTC{sign}{hours}{suffix}"


def sort_by_distance(flights: list[Flight]) -> list[Flight]:
    """按直线距离由近到远排序，供排行榜使用。"""
    return sorted(flights, key=lambda f: f.distance_km)
