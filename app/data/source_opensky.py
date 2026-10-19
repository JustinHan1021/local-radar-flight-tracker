"""OpenSky Network 数据源适配器（备选源）。

**实测结论：不适合作为主源。** 同一位置同一时刻，OpenSky 仅返回 3 架航班，
且 17 个字段中不含机型、航司、航线——而排行榜要展示的正是这些字段。

保留它的两个理由：一是作为 adsb.lol 不可用时的降级兜底，
二是满足"默认使用 OpenSky"的原始需求，用户可在设置面板中手动切换。
缺字段的项在界面上显示 ``—``，而不是编造填补。

单位差异（易错点，已在此统一换算）：
OpenSky 高度用**米**、速度用**米/秒**、垂直速率用**米/秒**，
而模型内部统一使用英尺与节，转换全部集中在本模块。
"""

from __future__ import annotations

import time
import urllib.parse

import requests

from app.data.base import FlightSource, SourceError, SourceRateLimited
from app.data.models import Flight
from app.geo import radius_bbox

BASE_URL = "https://opensky-network.org/api/states/all"
AUTH_URL = "https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token"

# states 向量的字段下标，依据 OpenSky 官方文档定义
_IDX_ICAO24 = 0
_IDX_CALLSIGN = 1
_IDX_ORIGIN_COUNTRY = 2
_IDX_TIME_POSITION = 3
_IDX_LONGITUDE = 5
_IDX_LATITUDE = 6
_IDX_BARO_ALTITUDE = 7
_IDX_ON_GROUND = 8
_IDX_VELOCITY = 9
_IDX_TRUE_TRACK = 10
_IDX_VERTICAL_RATE = 11
_IDX_GEO_ALTITUDE = 13
_IDX_SQUAWK = 14
_IDX_CATEGORY = 17

_METERS_TO_FEET = 1.0 / 0.3048
_MS_TO_KNOTS = 1.9438445
_MS_TO_FPM = 196.85039

_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

# 位置数据缺失时的保守陈旧度（秒），避免被误判为"刚刚更新"
_UNKNOWN_STALENESS = 120.0


def _to_float(value) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_category(value) -> str:
    """把 OpenSky 的整数类别码转成 adsb.lol 风格的 ``A0``–``A7`` 字符串。

    两者本质是同一套 ADS-B 发射类别编码（0–7 对应 A0–A7），
    归一化后宽体判定逻辑才能对所有数据源通用。
    """
    number = _to_float(value)
    if number is None:
        return ""
    code = int(number)
    if 0 <= code <= 7:
        return f"A{code}"
    return ""


def parse_state(row: list) -> Flight | None:
    """把一条 OpenSky states 向量解析成 :class:`Flight`。"""
    if not isinstance(row, (list, tuple)) or len(row) <= _IDX_CATEGORY:
        return None

    icao24 = str(row[_IDX_ICAO24] or "").strip().lower()
    lat = _to_float(row[_IDX_LATITUDE])
    lon = _to_float(row[_IDX_LONGITUDE])
    if not icao24 or lat is None or lon is None:
        return None

    baro_altitude_m = _to_float(row[_IDX_BARO_ALTITUDE])
    geo_altitude_m = _to_float(row[_IDX_GEO_ALTITUDE])
    velocity_ms = _to_float(row[_IDX_VELOCITY])
    vertical_rate_ms = _to_float(row[_IDX_VERTICAL_RATE])
    time_position = _to_float(row[_IDX_TIME_POSITION])

    # 位置更新距今多久：OpenSky 给的是 Unix 时间戳，需要自己减
    if time_position is None:
        seen_pos = _UNKNOWN_STALENESS
    else:
        seen_pos = max(0.0, time.time() - time_position)

    return Flight(
        icao24=icao24,
        callsign=str(row[_IDX_CALLSIGN] or "").strip(),
        lat=lat,
        lon=lon,
        altitude_ft=None if baro_altitude_m is None else baro_altitude_m * _METERS_TO_FEET,
        geo_altitude_ft=None if geo_altitude_m is None else geo_altitude_m * _METERS_TO_FEET,
        ground_speed_kt=None if velocity_ms is None else velocity_ms * _MS_TO_KNOTS,
        track=_to_float(row[_IDX_TRUE_TRACK]),
        vertical_rate_fpm=None if vertical_rate_ms is None else vertical_rate_ms * _MS_TO_FPM,
        on_ground=bool(row[_IDX_ON_GROUND]),
        category=_format_category(row[_IDX_CATEGORY]),
        seen_pos=seen_pos,
        squawk=str(row[_IDX_SQUAWK] or "").strip(),
        origin_country=str(row[_IDX_ORIGIN_COUNTRY] or "").strip(),
    )


class OpenSkySource(FlightSource):
    """OpenSky Network 数据源。匿名访问即可，无需 Key。"""

    name = "opensky"
    label = "OpenSky Network"

    def __init__(self, client_id: str = "", client_secret: str = "") -> None:
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        self._client_id = client_id.strip()
        self._client_secret = client_secret.strip()
        self._token: str | None = None

    def _auth_headers(self) -> dict[str, str]:
        """配置了 OAuth2 凭据时取访问令牌，可显著提高额度上限。"""
        if not (self._client_id and self._client_secret):
            return {}
        if self._token is None:
            try:
                response = self._session.post(
                    AUTH_URL,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                    },
                    timeout=15,
                )
                if response.status_code == 200:
                    self._token = response.json().get("access_token")
            except requests.RequestException:
                # 取令牌失败就退回匿名访问，不阻断主流程
                self._token = None
        return {"Authorization": f"Bearer {self._token}"} if self._token else {}

    def fetch(
        self, lat: float, lon: float, radius_km: float, timeout: float = 20.0
    ) -> list[Flight]:
        # OpenSky 只支持矩形范围查询，用雷达圆的包围盒，多余部分由调度层按半径过滤
        lat_min, lat_max, lon_min, lon_max = radius_bbox(lat, lon, radius_km)
        query = urllib.parse.urlencode({
            "lamin": f"{lat_min:.6f}",
            "lomin": f"{lon_min:.6f}",
            "lamax": f"{lat_max:.6f}",
            "lomax": f"{lon_max:.6f}",
        })

        try:
            response = self._session.get(
                f"{BASE_URL}?{query}", timeout=timeout, headers=self._auth_headers()
            )
        except requests.RequestException as exc:
            raise SourceError(f"OpenSky 请求失败：{exc}") from exc

        if response.status_code == 429:
            raise SourceRateLimited("OpenSky 已限流（HTTP 429）")
        if response.status_code != 200:
            raise SourceError(f"OpenSky 返回 HTTP {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(f"OpenSky 响应不是合法 JSON：{exc}") from exc

        flights: list[Flight] = []
        for row in payload.get("states") or []:
            parsed = parse_state(row)
            if parsed is not None:
                flights.append(parsed)
        return flights

    def close(self) -> None:
        self._session.close()
