"""用户配置的读写、校验与持久化。

配置存放于项目根目录 ``config.json``，首次运行自动以默认值创建。
所有写入路径都经过类型与范围校验：非法值被夹紧或回退到默认值，
因此即使用户手改坏了 JSON，程序也不会因配置异常而崩溃。

写入采用"临时文件 + 原子替换"，避免断电或异常退出导致配置文件半截损坏。
"""

from __future__ import annotations

import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _resolve_data_root() -> Path:
    """确定可写的数据根目录。

    源码方式运行时就用项目目录；打包成 exe 之后不能再这么做：

    * ``--onefile`` 模式下 ``__file__`` 指向临时解包目录，程序一退出就被清空；
    * 装到 ``Program Files`` 之类的位置时，程序目录本身是只读的。

    两种情况都会让配置与缓存写不进去，因此打包后统一改用用户数据目录
    （``%LOCALAPPDATA%\\LocalRadarFlightTracker``）。
    """
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / ".local" / "share"
        target = root / "LocalRadarFlightTracker"
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        return target
    return Path(__file__).resolve().parent.parent


# 项目数据根目录（源码运行时是项目目录，打包后是用户数据目录）
PROJECT_ROOT = _resolve_data_root()
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config.json"

# ---------- 合法取值枚举 ----------
VALID_SOURCES = ("adsbfi", "adsblol", "opensky", "fr24")
VALID_ALTITUDE_UNITS = ("m", "ft")
VALID_FONT_SIZES = ("small", "medium", "large")
VALID_LOCATION_MODES = ("auto", "manual")

# 数据源显示名，供设置面板下拉框使用
SOURCE_LABELS = {
    "adsbfi": "adsb.fi（免费 · 无需 Key · 支持 10 秒刷新）",
    "adsblol": "adsb.lol（免费 · 字段最全 · 约限 3 次/分钟）",
    "opensky": "OpenSky Network（免费 · 数据较稀疏 · 无机型航司）",
    "fr24": "Flightradar24（需企业订阅 API Key）",
}

# ---------- 默认配置 ----------
DEFAULTS: dict[str, Any] = {
    "source": "adsbfi",
    "fr24_api_key": "",
    "radius_km": 50.0,
    "refresh_sec": 10,
    "max_flights": 60,
    "altitude_unit": "m",
    "show_ground": False,
    "stale_limit_sec": 60,
    "font_size": "medium",
    "scan_color": "#3FE08A",
    "bg_level": 0,
    # ---- 底图覆盖层开关 ----
    # 默认全开；数据量大或机器较慢时可以关掉地形与路网减轻负担
    "show_terrain": True,
    "show_roads": True,
    "show_clouds": True,
    "show_places": True,
    "show_airports": True,
    "show_weather": True,
    "location": {
        "mode": "auto",
        "lat": None,
        "lon": None,
    },
}

# 数值型配置的合法区间，越界即夹紧
NUMERIC_LIMITS: dict[str, tuple[float, float]] = {
    # 上限 460 km 而非更大：tar1090 系镜像（adsb.fi / adsb.lol）单次查询
    # 上限为 250 海里 ≈ 463 km，设得更大只会让雷达圈超出实际数据范围
    "radius_km": (1.0, 460.0),
    "refresh_sec": (3.0, 300.0),
    "max_flights": (5.0, 200.0),
    "stale_limit_sec": (10.0, 600.0),
    "bg_level": (0.0, 2.0),
}

_HEX_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _clamp(value: Any, low: float, high: float, fallback: float) -> float:
    """把数值夹到 ``[low, high]``；无法解析时返回 ``fallback``。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(low, min(high, number))


def _clean_color(value: Any, fallback: str) -> str:
    """校验 ``#RRGGBB`` 形式的颜色，非法则回退。"""
    if isinstance(value, str) and _HEX_COLOR_RE.match(value.strip()):
        return value.strip().upper()
    return fallback


def validate(raw: dict[str, Any]) -> dict[str, Any]:
    """把任意来源的原始字典整理成一份合法配置。

    以 :data:`DEFAULTS` 为基准逐项校验，未知键直接丢弃，
    缺失键用默认值补齐——这样配置文件升级时也不会缺项。
    """
    result = copy.deepcopy(DEFAULTS)

    # 枚举型
    if raw.get("source") in VALID_SOURCES:
        result["source"] = raw["source"]
    if raw.get("altitude_unit") in VALID_ALTITUDE_UNITS:
        result["altitude_unit"] = raw["altitude_unit"]
    if raw.get("font_size") in VALID_FONT_SIZES:
        result["font_size"] = raw["font_size"]

    # 布尔型
    for key in ("show_ground", "show_terrain", "show_roads", "show_clouds",
                "show_places", "show_airports", "show_weather"):
        if isinstance(raw.get(key), bool):
            result[key] = raw[key]

    # 数值型（夹紧到合法区间）
    for key, (low, high) in NUMERIC_LIMITS.items():
        default_value = float(DEFAULTS[key])
        cleaned = _clamp(raw.get(key), low, high, default_value)
        if key == "bg_level":
            result[key] = int(cleaned)
        elif key in ("refresh_sec", "max_flights", "stale_limit_sec"):
            result[key] = int(cleaned)
        else:
            result[key] = round(cleaned, 2)

    # 颜色
    result["scan_color"] = _clean_color(raw.get("scan_color"), DEFAULTS["scan_color"])

    # Flightradar24 API Key
    if isinstance(raw.get("fr24_api_key"), str):
        result["fr24_api_key"] = raw["fr24_api_key"].strip()

    # 定位（嵌套结构，单独校验）
    location = raw.get("location")
    if isinstance(location, dict):
        mode = location.get("mode")
        if mode in VALID_LOCATION_MODES:
            result["location"]["mode"] = mode
        lat, lon = location.get("lat"), location.get("lon")
        if isinstance(lat, (int, float)) and -90.0 <= lat <= 90.0:
            result["location"]["lat"] = round(float(lat), 6)
        if isinstance(lon, (int, float)) and -180.0 <= lon <= 180.0:
            result["location"]["lon"] = round(float(lon), 6)

    return result


class Config:
    """配置文件的内存视图，支持取值、设值与持久化。"""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_CONFIG_PATH
        self._data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # ---------- 持久化 ----------

    def load(self) -> None:
        """从磁盘读取配置。文件不存在则写出默认配置；内容损坏则退回默认值。"""
        if not self.path.exists():
            self.save()
            return

        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # 配置损坏不应阻断启动，静默退回默认值
            self._data = copy.deepcopy(DEFAULTS)
            return

        self._data = validate(raw) if isinstance(raw, dict) else copy.deepcopy(DEFAULTS)

    def save(self) -> None:
        """原子化写回磁盘：先写临时文件，再整体替换。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(self.path.name + ".tmp")
            temp_path.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.path)
        except OSError:
            # 磁盘不可写时不影响运行，仅本次设置不落盘
            pass

    # ---------- 通用访问 ----------

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """设置单个键。值会经 :func:`validate` 整理，写入后需调用 :meth:`save`。"""
        candidate = copy.deepcopy(self._data)
        candidate[key] = value
        self._data = validate(candidate)

    def update(self, **kwargs: Any) -> None:
        """批量设置并立即落盘。"""
        candidate = copy.deepcopy(self._data)
        candidate.update(kwargs)
        self._data = validate(candidate)
        self.save()

    def reset(self) -> None:
        """恢复全部默认值并落盘（定位与 API Key 一并重置）。"""
        self._data = copy.deepcopy(DEFAULTS)
        self.save()

    def as_dict(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    # ---------- 常用项快捷访问 ----------

    @property
    def source(self) -> str:
        return self._data["source"]

    @property
    def radius_km(self) -> float:
        return float(self._data["radius_km"])

    @property
    def refresh_sec(self) -> int:
        return int(self._data["refresh_sec"])

    @property
    def max_flights(self) -> int:
        return int(self._data["max_flights"])

    @property
    def altitude_unit(self) -> str:
        return self._data["altitude_unit"]

    @property
    def show_ground(self) -> bool:
        return bool(self._data["show_ground"])

    @property
    def stale_limit_sec(self) -> int:
        return int(self._data["stale_limit_sec"])

    @property
    def font_size(self) -> str:
        return self._data["font_size"]

    @property
    def scan_color(self) -> str:
        return self._data["scan_color"]

    @property
    def bg_level(self) -> int:
        return int(self._data["bg_level"])

    @property
    def fr24_api_key(self) -> str:
        return self._data["fr24_api_key"]

    @property
    def location_mode(self) -> str:
        return self._data["location"]["mode"]

    def manual_location(self) -> tuple[float, float] | None:
        """返回手动校准的经纬度；未设置则返回 ``None``。"""
        location = self._data["location"]
        lat, lon = location.get("lat"), location.get("lon")
        if lat is None or lon is None:
            return None
        return float(lat), float(lon)

    def set_manual_location(self, lat: float, lon: float) -> None:
        """写入手动校准坐标，并把定位模式切到 ``manual``。"""
        candidate = copy.deepcopy(self._data)
        candidate["location"] = {"mode": "manual", "lat": lat, "lon": lon}
        self._data = validate(candidate)
        self.save()

    def set_last_location(self, lat: float, lon: float) -> None:
        """记录最近一次成功定位的坐标，但**不改变定位模式**。

        用途：自动定位成功时把坐标留存下来，作为下次定位全部失败时的兜底——
        记住上次的位置远比猜一个默认坐标靠谱。
        """
        candidate = copy.deepcopy(self._data)
        candidate["location"]["lat"] = lat
        candidate["location"]["lon"] = lon
        self._data = validate(candidate)
        self.save()

    def use_auto_location(self) -> None:
        """把定位模式切回自动（保留已记录的坐标作为兜底）。"""
        candidate = copy.deepcopy(self._data)
        candidate["location"]["mode"] = "auto"
        self._data = validate(candidate)
        self.save()
