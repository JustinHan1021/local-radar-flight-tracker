"""航班信息富化：航线、机型全称、生产商与实拍图。

adsb.lol 只提供位置与遥测，航司与航线需要额外查询 adsbdb。
这类信息变化极慢——机型与注册号基本终生不变，航线按天固定——
因此全部落盘缓存，既大幅减少请求量，也避免触发数据源限流。

请求策略：

* 只对**新出现**的 ``icao24`` / ``callsign`` 发起请求，命中缓存直接复用
* 线程池并发 4 路
* 单域请求最小间隔 200 ms，跨线程全局节流
* adsbdb 失败时降级到 hexdb；仍失败则保留遥测自带的原始字段，绝不阻断出图
* 查不到的"负结果"也短期缓存，避免对同一目标反复重试

缓存文件：``assets/cache/enrich.json``
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

from app.config import PROJECT_ROOT
from app.data.models import Flight, Route

CACHE_PATH = PROJECT_ROOT / "assets" / "cache" / "enrich.json"
PHOTO_DIR = PROJECT_ROOT / "assets" / "photos"

ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{key}"
ADSBDB_CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign/{key}"
HEXDB_AIRCRAFT_URL = "https://hexdb.io/api/v1/aircraft/{key}"

# 缓存有效期
_AIRCRAFT_TTL = 7 * 24 * 3600     # 机型、注册号、运营人基本不变
_CALLSIGN_TTL = 24 * 3600         # 航线按天更新
_NEGATIVE_TTL = 6 * 3600          # 查不到的结果短期记住，避免反复重试

_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

_VALID_PHOTO_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")


class _RateLimiter:
    """跨线程共享的请求节流器。"""

    def __init__(self, min_interval: float) -> None:
        self._min_interval = max(0.0, min_interval)
        self._lock = threading.Lock()
        self._next_allowed_at = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self._next_allowed_at - now
            self._next_allowed_at = max(now, self._next_allowed_at) + self._min_interval
        if delay > 0:
            time.sleep(delay)


def _text(value) -> str:
    """把字段转成去空白的字符串，None 安全。"""
    if value is None:
        return ""
    return str(value).strip()


def _optional_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def download_photo(url: str, timeout: float = 15.0) -> Path | None:
    """下载飞机实拍图到本地缓存，返回本地文件路径。

    文件名取 URL 的哈希，天然去重；已存在且非空则直接复用，不再联网。
    """
    if not url:
        return None

    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix not in _VALID_PHOTO_SUFFIXES:
        suffix = ".jpg"

    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:20]
    target = PHOTO_DIR / f"{digest}{suffix}"

    if target.exists() and target.stat().st_size > 0:
        return target

    try:
        response = requests.get(url, timeout=timeout, headers={"User-Agent": _USER_AGENT})
        if response.status_code != 200 or not response.content:
            return None
        PHOTO_DIR.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再替换，避免读到半截图片
        temp = target.with_name(target.name + ".part")
        temp.write_bytes(response.content)
        temp.replace(target)
        return target
    except (requests.RequestException, OSError):
        return None


class Enricher:
    """按需富化航班信息，并把结果长期缓存到磁盘。"""

    def __init__(
        self,
        cache_path: Path | str | None = None,
        timeout: float = 12.0,
        workers: int = 4,
        min_interval: float = 0.2,
        enabled: bool = True,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path else CACHE_PATH
        self.timeout = timeout
        self.workers = max(1, workers)
        self.enabled = enabled

        self._session = requests.Session()
        self._session.headers.update({"User-Agent": _USER_AGENT})
        self._limiter = _RateLimiter(min_interval)
        self._lock = threading.Lock()
        self._dirty = False

        self._aircraft_cache: dict[str, dict] = {}
        self._callsign_cache: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # 缓存读写
    # ------------------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if not isinstance(raw, dict):
            return

        aircraft = raw.get("aircraft")
        if isinstance(aircraft, dict):
            self._aircraft_cache = {k: v for k, v in aircraft.items() if isinstance(v, dict)}

        callsign = raw.get("callsign")
        if isinstance(callsign, dict):
            self._callsign_cache = {k: v for k, v in callsign.items() if isinstance(v, dict)}

    def save(self, force: bool = False) -> None:
        """原子化写回缓存。无改动时默认跳过，避免每轮刷新都写盘。"""
        if not self._dirty and not force:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "aircraft": self._aircraft_cache,
                "callsign": self._callsign_cache,
            }
            temp = self.cache_path.with_name(self.cache_path.name + ".tmp")
            temp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            temp.replace(self.cache_path)
            self._dirty = False
        except OSError:
            # 缓存写不进去不影响本次运行
            pass

    def clear_cache(self) -> None:
        """清空全部富化缓存（设置面板的"清除缓存"用）。"""
        with self._lock:
            self._aircraft_cache.clear()
            self._callsign_cache.clear()
            self._dirty = True
        self.save(force=True)

    # ------------------------------------------------------------------
    # 缓存命中判断
    # ------------------------------------------------------------------

    def _needs_aircraft(self, icao24: str) -> bool:
        entry = self._aircraft_cache.get(icao24)
        if entry is None:
            return True
        ttl = _AIRCRAFT_TTL if entry.get("data") else _NEGATIVE_TTL
        return (time.time() - float(entry.get("ts") or 0)) > ttl

    def _needs_callsign(self, callsign: str) -> bool:
        entry = self._callsign_cache.get(callsign)
        if entry is None:
            return True
        ttl = _CALLSIGN_TTL if entry.get("data") else _NEGATIVE_TTL
        return (time.time() - float(entry.get("ts") or 0)) > ttl

    # ------------------------------------------------------------------
    # 主流程
    # ------------------------------------------------------------------

    def enrich(self, flights: list[Flight]) -> None:
        """就地为航班补全富化信息。网络异常不影响已有数据。"""
        if not self.enabled or not flights:
            return

        aircraft_keys: list[str] = []
        callsign_keys: list[str] = []
        seen_aircraft: set[str] = set()
        seen_callsign: set[str] = set()

        for flight in flights:
            icao24 = flight.icao24
            if icao24 and icao24 not in seen_aircraft and self._needs_aircraft(icao24):
                seen_aircraft.add(icao24)
                aircraft_keys.append(icao24)

            callsign = flight.callsign.strip().upper()
            if callsign and callsign not in seen_callsign and self._needs_callsign(callsign):
                seen_callsign.add(callsign)
                callsign_keys.append(callsign)

        if aircraft_keys or callsign_keys:
            self._fetch_many(aircraft_keys, callsign_keys)

        for flight in flights:
            self._apply(flight)

    def _fetch_many(self, aircraft_keys: list[str], callsign_keys: list[str]) -> None:
        """并发拉取缺失项。单个键失败被吞掉，不影响其余键。"""
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = [pool.submit(self._fetch_aircraft, key) for key in aircraft_keys]
            futures += [pool.submit(self._fetch_callsign, key) for key in callsign_keys]
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    # 富化失败是常态（目标未收录），静默跳过即可
                    pass
        self.save()

    def _apply(self, flight: Flight) -> None:
        """把缓存中的富化结果填入航班对象。"""
        entry = self._aircraft_cache.get(flight.icao24)
        data = entry.get("data") if entry else None
        if isinstance(data, dict):
            # 富化结果比遥测自带的更完整；但遥测值非空时不覆盖成空串
            flight.info.registration = data.get("registration") or flight.info.registration
            flight.info.type_code = data.get("type_code") or flight.info.type_code
            flight.info.type_name = data.get("type_name") or ""
            flight.info.manufacturer = data.get("manufacturer") or ""
            flight.info.operator = data.get("operator") or ""
            flight.info.photo_url = data.get("photo_url") or ""

        callsign = flight.callsign.strip().upper()
        entry = self._callsign_cache.get(callsign)
        route_data = entry.get("data") if entry else None
        if isinstance(route_data, dict):
            try:
                flight.route = Route(**route_data)
            except TypeError:
                flight.route = None

    # ------------------------------------------------------------------
    # 网络请求
    # ------------------------------------------------------------------

    def _fetch_aircraft(self, icao24: str) -> None:
        self._limiter.wait()
        info: dict | None = None

        try:
            response = self._session.get(
                ADSBDB_AIRCRAFT_URL.format(key=icao24), timeout=self.timeout
            )
            if response.status_code == 200:
                info = self._parse_adsbdb_aircraft(response.json())
        except (requests.RequestException, ValueError):
            info = None

        # adsbdb 未收录时退回 hexdb
        if info is None:
            info = self._fetch_hexdb_aircraft(icao24)

        self._store(self._aircraft_cache, icao24, info)

    def _fetch_hexdb_aircraft(self, icao24: str) -> dict | None:
        self._limiter.wait()
        try:
            response = self._session.get(
                HEXDB_AIRCRAFT_URL.format(key=icao24), timeout=self.timeout
            )
            if response.status_code != 200:
                return None
            payload = response.json()
        except (requests.RequestException, ValueError):
            return None

        if not isinstance(payload, dict):
            return None
        return {
            "registration": _text(payload.get("Registration")).upper(),
            "type_code": _text(payload.get("ICAOTypeCode")).upper(),
            "type_name": _text(payload.get("Type")),
            "manufacturer": _text(payload.get("Manufacturer")),
            "operator": _text(payload.get("RegisteredOwners")),
            "photo_url": "",  # hexdb 不提供实拍图
        }

    def _fetch_callsign(self, callsign: str) -> None:
        self._limiter.wait()
        route: dict | None = None
        try:
            response = self._session.get(
                ADSBDB_CALLSIGN_URL.format(key=callsign), timeout=self.timeout
            )
            if response.status_code == 200:
                route = self._parse_adsbdb_callsign(response.json())
        except (requests.RequestException, ValueError):
            route = None

        self._store(self._callsign_cache, callsign, route)

    def _store(self, cache: dict[str, dict], key: str, data: dict | None) -> None:
        with self._lock:
            cache[key] = {"data": data, "ts": time.time()}
            self._dirty = True

    # ------------------------------------------------------------------
    # 响应解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_adsbdb_aircraft(payload) -> dict | None:
        """解析 adsbdb ``/v0/aircraft/{hex}``。

        实测响应形如::

            {"response": {"aircraft": {
                "type": "787 9", "icao_type": "B789", "manufacturer": "Boeing",
                "mode_s": "781001", "registration": "B-7835",
                "registered_owner": "Hainan Airlines",
                "url_photo": "https://image.airport-data.com/aircraft/001411389.jpg"}}}
        """
        if not isinstance(payload, dict):
            return None
        response = payload.get("response")
        if not isinstance(response, dict):
            return None
        aircraft = response.get("aircraft")
        if not isinstance(aircraft, dict):
            return None

        return {
            "registration": _text(aircraft.get("registration")).upper(),
            "type_code": _text(aircraft.get("icao_type")).upper(),
            "type_name": _text(aircraft.get("type")),
            "manufacturer": _text(aircraft.get("manufacturer")),
            "operator": _text(aircraft.get("registered_owner")),
            "photo_url": _text(aircraft.get("url_photo")),
        }

    @staticmethod
    def _parse_adsbdb_callsign(payload) -> dict | None:
        """解析 adsbdb ``/v0/callsign/{callsign}``。

        实测响应形如::

            {"response": {"flightroute": {
                "callsign": "CHH7713", "callsign_icao": "CHH7713",
                "callsign_iata": "HU7713",
                "airline": {"name": "Hainan Airlines", "icao": "CHH", "iata": "HU"},
                "origin": {"iata_code": "PEK", "icao_code": "ZBAA",
                           "name": "Beijing Capital International Airport",
                           "country_iso_name": "CN", "municipality": "Beijing",
                           "latitude": 40.08, "longitude": 116.58},
                "destination": {...}}}}
        """
        if not isinstance(payload, dict):
            return None
        response = payload.get("response")
        if not isinstance(response, dict):
            return None
        route = response.get("flightroute")
        if not isinstance(route, dict):
            return None

        airline = route.get("airline") if isinstance(route.get("airline"), dict) else {}
        origin = route.get("origin") if isinstance(route.get("origin"), dict) else {}
        destination = (
            route.get("destination") if isinstance(route.get("destination"), dict) else {}
        )

        parsed = {
            "airline": _text(airline.get("name")),
            "airline_icao": _text(airline.get("icao")).upper(),
            "airline_iata": _text(airline.get("iata")).upper(),
            "icao_callsign": _text(route.get("callsign_icao")).upper(),
            "iata_callsign": _text(route.get("callsign_iata")).upper(),

            "origin_iata": _text(origin.get("iata_code")).upper(),
            "origin_icao": _text(origin.get("icao_code")).upper(),
            "origin_name": _text(origin.get("name")),
            "origin_country": _text(origin.get("country_iso_name")).upper(),
            "origin_lat": _optional_float(origin.get("latitude")),
            "origin_lon": _optional_float(origin.get("longitude")),

            "destination_iata": _text(destination.get("iata_code")).upper(),
            "destination_icao": _text(destination.get("icao_code")).upper(),
            "destination_name": _text(destination.get("name")),
            "destination_country": _text(destination.get("country_iso_name")).upper(),
            "destination_lat": _optional_float(destination.get("latitude")),
            "destination_lon": _optional_float(destination.get("longitude")),
        }

        # 三者全空说明这条记录没有实际内容，按未命中处理
        if not (parsed["airline"] or parsed["origin_iata"] or parsed["destination_iata"]):
            return None
        return parsed

    def close(self) -> None:
        self.save(force=True)
        self._session.close()
