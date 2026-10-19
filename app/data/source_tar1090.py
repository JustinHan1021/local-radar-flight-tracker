"""tar1090 系公开镜像适配器（adsb.fi / adsb.lol）。

adsb.fi 与 adsb.lol 都基于 tar1090，**接口与字段完全一致**，
差别只在 URL 路径与响应里列表的键名，因此共用同一份解析逻辑。

数据源选型完全基于本机实测，结论如下：

======================  ==========================  ==============  ==================
镜像                    10 秒刷新是否可持续          香港 50 km 航班   字段完整度
======================  ==========================  ==============  ==================
**adsb.fi**             ✅ 30/30 连续成功，约 214ms   26–37 架        注册号/机型/类别齐全
adsb.lol                ❌ 配额仅 3 次/分钟            34–48 架        同上
OpenSky                 ✅ 宽松                       26 架           无机型/航司/类别码
======================  ==========================  ==============  ==================

因此**默认主源取 adsb.fi**——它是唯一能支撑 10 秒刷新频率的免费源；
adsb.lol 数据略多但配额严格（实测间隔 1 秒连发，第 4 次即 HTTP 429；
间隔 25 秒可持续），适合作为备选；OpenSky 数据字段贫瘠，仅作兜底。

两处**已实测确认**的单位陷阱：

* URL 中 ``radius`` 单位是**海里**（两个镜像都是），上限 250 nm ≈ 463 km
* 响应中 ``dst`` 也是海里、``dir`` 为度

本适配器把半径从公里换算成海里发出，并且**距离与方位一律本地重算**，
不采信 ``dst`` / ``dir``，以保证全链路单位统一且与雷达投影同源。
"""

from __future__ import annotations

import requests

from app.data.base import FlightSource, SourceError, SourceRateLimited
from app.data.models import Flight

#: 服务端半径上限（海里），换算约 463 km。两个镜像都是这个上限。
MAX_RADIUS_NM = 250.0
KM_PER_NM = 1.852
MAX_RADIUS_KM = MAX_RADIUS_NM * KM_PER_NM

_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

# alt_baro 为这些字符串时表示目标在地面
_GROUND_TOKENS = {"ground", "grnd"}


def _to_float(value) -> float | None:
    """宽容地把字段转成 float；空值、布尔值与非数字返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_altitude(value) -> float | None:
    """解析海拔（英尺）。字符串 ``"ground"`` 表示在地面，与 0 英尺不同。"""
    if isinstance(value, str):
        if value.strip().lower() in _GROUND_TOKENS:
            return None
        return _to_float(value)
    return _to_float(value)


def _is_ground_token(value) -> bool:
    return isinstance(value, str) and value.strip().lower() in _GROUND_TOKENS


def parse_aircraft(raw: dict) -> Flight | None:
    """把一条 tar1090 记录解析成 :class:`Flight`。

    没有位置的目标直接丢弃——雷达图无法绘制它们。
    遥测自带的注册号与机型代码先填进 ``info`` 占位，
    随后由富化层用更完整的 adsbdb 结果覆盖。
    """
    icao24 = str(raw.get("hex") or "").strip().lower()
    lat = _to_float(raw.get("lat"))
    lon = _to_float(raw.get("lon"))
    if not icao24 or lat is None or lon is None:
        return None

    altitude_raw = raw.get("alt_baro")
    on_ground = _is_ground_token(altitude_raw) or _is_ground_token(raw.get("alt_geom"))

    # 垂直速率优先取气压变化率，为 None 时才退回几何变化率。
    # 注意不能写成 `a or b`——0 是"平飞"这一有效值，会被 or 误判成缺失。
    vertical_rate = _to_float(raw.get("baro_rate"))
    if vertical_rate is None:
        vertical_rate = _to_float(raw.get("geom_rate"))

    seen_pos = _to_float(raw.get("seen_pos"))
    if seen_pos is None:
        seen_pos = 0.0

    flight = Flight(
        icao24=icao24,
        callsign=str(raw.get("flight") or "").strip(),
        lat=lat,
        lon=lon,
        altitude_ft=_parse_altitude(altitude_raw),
        geo_altitude_ft=_parse_altitude(raw.get("alt_geom")),
        ground_speed_kt=_to_float(raw.get("gs")),
        track=_to_float(raw.get("track")),
        vertical_rate_fpm=vertical_rate,
        on_ground=on_ground,
        category=str(raw.get("category") or "").strip().upper(),
        seen_pos=seen_pos,
        squawk=str(raw.get("squawk") or "").strip(),
    )

    flight.info.registration = str(raw.get("r") or "").strip().upper()
    flight.info.type_code = str(raw.get("t") or "").strip().upper()
    return flight


class Tar1090Source(FlightSource):
    """tar1090 系镜像的公共实现。子类只需给出 URL 模板与列表键名。"""

    #: URL 模板，可含 ``{lat}`` ``{lon}`` ``{radius}`` 占位符
    url_template: str = ""
    #: 响应中航班列表可能的键名，按顺序探测
    list_keys: tuple[str, ...] = ("ac", "aircraft")
    max_radius_km = MAX_RADIUS_KM

    def __init__(self) -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})

    def _extract_list(self, payload) -> list:
        """从响应里取出航班列表，兼容不同镜像的键名。"""
        if not isinstance(payload, dict):
            return []
        for key in self.list_keys:
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []

    def fetch(
        self, lat: float, lon: float, radius_km: float, timeout: float = 15.0
    ) -> list[Flight]:
        # 服务端只接受海里且上限 250 nm；超出会被静默截断，故在此夹紧
        radius_nm = min(max(1.0, radius_km / KM_PER_NM), MAX_RADIUS_NM)
        url = self.url_template.format(lat=f"{lat:.6f}", lon=f"{lon:.6f}", radius=f"{radius_nm:.1f}")

        try:
            response = self._session.get(url, timeout=timeout)
        except requests.RequestException as exc:
            raise SourceError(f"{self.label} 请求失败：{exc}") from exc

        if response.status_code == 429:
            # 实测 adsb.lol 的配额是"约 60 秒内 3 次"，触发后需等待窗口滑出
            raise SourceRateLimited(f"{self.label} 请求过于频繁（HTTP 429）")
        if response.status_code != 200:
            raise SourceError(f"{self.label} 返回 HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(f"{self.label} 响应不是合法 JSON：{exc}") from exc

        flights: list[Flight] = []
        for raw in self._extract_list(payload):
            if not isinstance(raw, dict):
                continue
            parsed = parse_aircraft(raw)
            if parsed is not None:
                flights.append(parsed)
        return flights

    def close(self) -> None:
        self._session.close()


class AdsbfiSource(Tar1090Source):
    """adsb.fi 镜像。**默认主源**——实测可支撑 10 秒刷新且不限流。

    注意它的路径格式与 adsb.lol 不同（``/lat/../lon/../dist/..``），
    响应列表键名是 ``aircraft``。
    """

    name = "adsbfi"
    label = "adsb.fi"
    url_template = "https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{radius}"
    list_keys = ("aircraft", "ac")


class AdsblolSource(Tar1090Source):
    """adsb.lol 镜像。数据量略多，但配额严格（约 3 次/分钟），适合做备选。"""

    name = "adsblol"
    label = "adsb.lol"
    url_template = "https://api.adsb.lol/v2/point/{lat}/{lon}/{radius}"
    list_keys = ("ac", "aircraft")
