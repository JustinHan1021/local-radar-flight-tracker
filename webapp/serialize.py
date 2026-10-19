"""``Flight`` / ``Config`` → JSON。

**格式化刻意放在服务端。** ``Flight`` 上已有整套 ``display_*`` 方法与
``flight_number`` / ``model_name`` / ``airline_name`` 等属性（桌面版详情页与雷达标签
用的就是它们）。若把这些规则搬到 JS 再维护一份，两边文案迟早不一致——
所以 payload 同时给**原始量**与 **display 子对象**：要画几何用原始量，
要显示文案用 display。
"""

from __future__ import annotations

from datetime import datetime

from app.data.models import Flight

#: 回给前端前必须抹掉的配置键。前端只需要知道"有没有配"。
SECRET_CONFIG_KEYS = ("fr24_api_key",)

#: 垂直速度超过这个值才算爬升/下降（英尺/分钟）。
#: 取得比桌面版 display_vertical_rate 的 50 大：那个是"是否平飞"的显示阈值，
#: 而这里回答的是"处于什么飞行阶段"，噪声容限应当更宽。
PHASE_RATE_FPM = 300.0

#: 低于这个地速视为滑行/等待，而非巡航（节）。
PHASE_LOW_SPEED_KT = 120.0


def flight_phase(flight: Flight) -> str:
    """飞行阶段。桌面版详情页用的就是这几个词。"""
    if flight.on_ground:
        return "地面"
    rate = flight.vertical_rate_fpm
    if rate is not None:
        if rate > PHASE_RATE_FPM:
            return "爬升中"
        if rate < -PHASE_RATE_FPM:
            return "下降中"
    speed = flight.ground_speed_kt
    if speed is not None and speed < PHASE_LOW_SPEED_KT:
        return "低速"
    return "巡航"


def _route_to_dict(flight: Flight) -> dict | None:
    route = flight.route
    if route is None:
        return None
    return {
        "origin_iata": route.origin_iata,
        "destination_iata": route.destination_iata,
        "airline": route.airline,
        "iata_callsign": route.iata_callsign,
        "label": route.label,
        "destination_lat": route.destination_lat,
        "destination_lon": route.destination_lon,
        "destination_country": route.destination_country,
    }


def flight_to_dict(
    flight: Flight, unit: str = "m", now: datetime | None = None
) -> dict:
    """一架航班 → JSON。

    ``unit`` 只影响 display 文案，原始量一律保持英尺/节，
    这样客户端切换单位不必重新请求。
    """
    note = flight.special_note
    return {
        # ---- 遥测原始量：客户端画几何用 ----
        "icao24": flight.icao24,
        "callsign": flight.callsign,
        "lat": flight.lat,
        "lon": flight.lon,
        "prev_lat": flight.prev_lat,
        "prev_lon": flight.prev_lon,
        "altitude_ft": flight.altitude_ft,
        "geo_altitude_ft": flight.geo_altitude_ft,
        "ground_speed_kt": flight.ground_speed_kt,
        "track": flight.track,
        "vertical_rate_fpm": flight.vertical_rate_fpm,
        "on_ground": flight.on_ground,
        "category": flight.category,
        "seen_pos": flight.seen_pos,
        "squawk": flight.squawk,
        "origin_country": flight.origin_country,
        "distance_km": round(flight.distance_km, 3),
        "bearing": round(flight.bearing, 2),
        "info": {
            "registration": flight.info.registration,
            "type_code": flight.info.type_code,
            "type_name": flight.info.type_name,
            "manufacturer": flight.info.manufacturer,
            "operator": flight.info.operator,
            "photo_url": flight.info.photo_url,
        },
        "route": _route_to_dict(flight),
        "flags": {
            "widebody": flight.is_widebody,
            "special": bool(note),
            "special_note": note or "",
            "manufacturer_url": flight.manufacturer_url or "",
        },
        # ---- 展示文案：客户端直接用，不要在 JS 里重算 ----
        "display": {
            "flight_number": flight.flight_number,
            "icao_callsign": flight.icao_callsign,
            "registration": flight.registration,
            "airline": flight.airline_name,
            "model": flight.model_name,
            "altitude": flight.display_altitude(unit),
            "altitude_short": flight.display_altitude_short(unit),
            "speed": flight.display_speed(),
            "heading": flight.display_track(),
            "vertical": flight.display_vertical_rate(),
            "distance": flight.display_distance(),
            "bearing": flight.display_bearing(),
            "route_label": flight.route_label,
            "phase": flight_phase(flight),
            "eta": flight.eta_text(now),
            "destination_offset": flight.destination_local_offset_text(),
        },
    }


def frame_to_dict(
    payload: dict, unit: str = "m", cooling: dict[str, int] | None = None
) -> dict:
    """把 pipeline 的帧转成 JSON 安全的字典。"""
    now = payload.get("fetched_at")
    flights = payload.get("flights") or []
    center = payload.get("center") or (0.0, 0.0)
    return {
        "flights": [flight_to_dict(f, unit, now) for f in flights],
        "source": payload.get("source", ""),
        "notice": payload.get("notice", ""),
        "stale": bool(payload.get("stale", False)),
        "center": [center[0], center[1]],
        "radius_km": payload.get("radius_km", 0.0),
        "fetched_at": now.isoformat() if now is not None else None,
        "cooling": cooling or {},
    }


def empty_frame(lat: float, lon: float, radius_km: float, notice: str = "") -> dict:
    """无数据可用时的空帧。字段与正常帧一致，前端不必写两条分支。"""
    return {
        "flights": [],
        "source": "",
        "notice": notice,
        "stale": False,
        "center": [lat, lon],
        "radius_km": radius_km,
        "fetched_at": None,
        "cooling": {},
    }


def config_to_dict(config) -> dict:
    """配置回前端：抹掉密钥明文，只留"是否已配"。"""
    data = dict(config.as_dict())
    for key in SECRET_CONFIG_KEYS:
        raw = str(data.get(key, "") or "")
        data.pop(key, None)
        data[f"has_{key}"] = bool(raw)
    return data
