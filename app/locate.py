"""用户定位：Windows 系统定位 → IP 定位 → 手动校准。

三级降级策略：

1. **Windows 系统定位**（``winsdk`` 的 ``Geolocator``）——实测本机精度约 86 米，
   是唯一能真正回答"用户此刻在哪"的方式；
2. **IP 定位**——多源轮询，精度为城市级（数公里到数十公里），
   且在有代理的网络环境下可能落在代理出口，只能作为兜底；
3. **手动校准**——前两者都失败时，使用配置文件中保存的坐标。

系统定位是阻塞操作（要等 GPS / WiFi 定位回传），必须放在后台线程调用，
否则启动时界面会白屏数秒。
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from dataclasses import dataclass

_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

# IP 定位备用源。返回字段名不同，逐个适配；顺带互为交叉验证。
_IP_ENDPOINTS: tuple[tuple[str, str], ...] = (
    ("https://api.ip.sb/geoip", "lat_lon"),
    ("https://ipwho.is/", "lat_lon"),
    ("https://ipinfo.io/json", "loc"),
)


class LocationError(RuntimeError):
    """定位失败。"""


@dataclass(frozen=True)
class LocationResult:
    """一次成功的定位结果。"""

    lat: float
    lon: float
    #: 定位方式：``windows`` / ``ip`` / ``manual``
    source: str
    #: 精度（米），仅系统定位会提供
    accuracy_m: float | None = None
    #: 面向用户的说明文本
    detail: str = ""

    @property
    def is_precise(self) -> bool:
        return self.source == "windows"


# ---------------------------------------------------------------------------
# Windows 系统定位
# ---------------------------------------------------------------------------

def locate_windows(timeout: float = 12.0) -> LocationResult:
    """调用 Windows 高精度定位 API。

    需要系统定位服务（``lfsvc``）已启动且应用有定位权限；
    任一条件不满足都会抛出 :class:`LocationError`，由调用方降级到 IP 定位。
    """
    try:
        from winsdk.windows.devices.geolocation import (
            GeolocationAccessStatus,
            Geolocator,
            PositionAccuracy,
        )
    except ImportError as exc:
        raise LocationError("未安装 winsdk，无法使用系统定位") from exc

    async def _read() -> tuple[float, float, float | None]:
        locator = Geolocator()
        locator.desired_accuracy = PositionAccuracy.HIGH
        status = await locator.request_access_async()
        if status != GeolocationAccessStatus.ALLOWED:
            raise LocationError("系统定位权限被拒绝（请在 Windows 设置中允许定位）")
        position = await locator.get_geoposition_async()
        coordinate = position.coordinate
        return coordinate.latitude, coordinate.longitude, coordinate.accuracy

    try:
        lat, lon, accuracy = asyncio.run(asyncio.wait_for(_read(), timeout))
    except LocationError:
        raise
    except asyncio.TimeoutError as exc:
        raise LocationError(f"系统定位超时（{timeout:.0f} 秒）") from exc
    except Exception as exc:  # noqa: BLE001 - winsdk 会抛各类 WinRT 异常
        raise LocationError(f"系统定位失败：{type(exc).__name__}") from exc

    accuracy_text = f"，精度约 {accuracy:.0f} 米" if accuracy else ""
    return LocationResult(
        lat=float(lat),
        lon=float(lon),
        source="windows",
        accuracy_m=float(accuracy) if accuracy else None,
        detail=f"Windows 系统定位{accuracy_text}",
    )


# ---------------------------------------------------------------------------
# IP 定位
# ---------------------------------------------------------------------------

def _fetch_json(url: str, timeout: float) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _parse_ip_payload(payload: dict, style: str) -> tuple[float, float] | None:
    if not isinstance(payload, dict):
        return None

    if style == "loc":
        # ipinfo 把坐标放在 "lat,lon" 形式的字符串里
        text = payload.get("loc")
        if isinstance(text, str) and "," in text:
            left, right = text.split(",", 1)
            try:
                return float(left), float(right)
            except ValueError:
                return None
        return None

    lat = payload.get("latitude", payload.get("lat"))
    lon = payload.get("longitude", payload.get("lon"))
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return float(lat), float(lon)
    return None


def locate_ip(timeout: float = 6.0) -> LocationResult:
    """按顺序尝试多个 IP 定位源。

    全部失败时抛 :class:`LocationError`。刻意轮询多个源：
    单一源在国内网络环境下容易被拒或返回错误归属地。
    """
    errors: list[str] = []
    for url, style in _IP_ENDPOINTS:
        try:
            payload = _fetch_json(url, timeout)
        except Exception as exc:  # noqa: BLE001 - 网络异常种类繁多，统一降级
            errors.append(f"{url.split('/')[2]}({type(exc).__name__})")
            continue

        parsed = _parse_ip_payload(payload, style)
        if parsed is None:
            errors.append(f"{url.split('/')[2]}(无坐标字段)")
            continue

        lat, lon = parsed
        region = payload.get("city") or payload.get("region") or payload.get("country") or ""
        return LocationResult(
            lat=lat,
            lon=lon,
            source="ip",
            accuracy_m=None,
            detail=f"IP 定位（{region}，精度为城市级）",
        )

    raise LocationError("IP 定位失败：" + "、".join(errors))


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def locate(prefer_windows: bool = True, fallback: tuple[float, float] | None = None) -> LocationResult:
    """依次尝试系统定位与 IP 定位。

    ``fallback`` 为配置文件中保存的手动校准坐标，通常等价于上一次成功定位的位置，
    因此比猜测一个默认值可靠。
    """
    errors: list[str] = []

    if prefer_windows:
        try:
            return locate_windows()
        except LocationError as exc:
            errors.append(str(exc))

    try:
        return locate_ip()
    except LocationError as exc:
        errors.append(str(exc))

    if fallback is not None:
        return LocationResult(
            lat=fallback[0],
            lon=fallback[1],
            source="manual",
            accuracy_m=None,
            detail="使用上次保存的坐标（系统定位与 IP 定位均失败）",
        )

    raise LocationError("；".join(errors) if errors else "定位失败")
