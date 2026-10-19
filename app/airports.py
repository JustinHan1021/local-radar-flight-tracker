"""机场：跑道形态绘制与机场快速跳转。

数据来自 **OurAirports**（公共领域，免注册直接下载）：

* ``airports.csv``——8.6 万条机场，含城市、IATA/ICAO 代码、类型、是否定期航班
* ``runways.csv``——4.8 万条跑道，含**两端经纬度**、磁方位、长度与宽度

有了跑道两端的真实坐标，就能把机场画成它本来的样子——
例如香港国际机场是三条平行跑道（07C / 07L / 07R，各长 12467 ft），
实测可以从数据里精确取到六组端点坐标与方位。

机场列表用于"快速跳转到某个城市上空"：筛选出在运营的民航机场
（``scheduled_service = yes`` 且类型为大型/中型），按城市名排序，
选中即把雷达圆心移到该机场。
"""

from __future__ import annotations

import csv
import io
import math
import pickle
from dataclasses import dataclass, field
from pathlib import Path

import requests

from app.config import PROJECT_ROOT

DATA_DIR = PROJECT_ROOT / "assets" / "data"
CACHE_DIR = PROJECT_ROOT / "assets" / "cache"

_AIRPORTS_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
_RUNWAYS_URL = "https://davidmegginson.github.io/ourairports-data/runways.csv"

_AIRPORTS_FILE = "ourairports_airports.csv"
_RUNWAYS_FILE = "ourairports_runways.csv"

_USER_AGENT = "LocalRadarFlightTracker/1.0 (Windows)"

#: pickle 结构版本
_CACHE_VERSION = 1

#: 下载文件的最小合理体积，避免把错误页面当成数据
_MIN_AIRPORTS_BYTES = 1_000_000
_MIN_RUNWAYS_BYTES = 200_000

#: 视为"全国"的地区代码。港澳台在 OurAirports 里是不同的 iso_country，
#: 但都属于中国民航机场的范畴，跳转列表一并收录。
CHINA_REGIONS = ("CN", "HK", "MO", "TW")

#: 视为"民航机场"的类型。small_airport 数量极多且多为通用航空，
#: 不纳入跳转列表，但跑道仍会绘制。
_SCHEDULED_TYPES = ("large_airport", "medium_airport")


def base_city(value: str) -> str:
    """去掉 OurAirports 城市名里的括号补充。

    实测该字段常带后缀：``Shanghai (Pudong)``、``Guangzhou (Huadu)``、
    ``Chengdu (Shuangliu)``、``Shenzhen (Nanshan)``。不剥离的话
    中文对照表一个都匹配不上。
    """
    text = (value or "").strip()
    if "(" in text:
        text = text.split("(", 1)[0].strip()
    return text

#: 常见中国城市的中文名。OurAirports 只提供英文地名，
#: 这里补一份对照表让界面保持中文；未收录的城市回退到英文原名。
_CITY_ZH: dict[str, str] = {
    "Beijing": "北京", "Shanghai": "上海", "Guangzhou": "广州", "Shenzhen": "深圳",
    "Chengdu": "成都", "Chongqing": "重庆", "Hangzhou": "杭州", "Xi'an": "西安",
    "Xian": "西安", "Wuhan": "武汉", "Nanjing": "南京", "Tianjin": "天津",
    "Zhengzhou": "郑州", "Changsha": "长沙", "Qingdao": "青岛", "Shenyang": "沈阳",
    "Dalian": "大连", "Xiamen": "厦门", "Fuzhou": "福州", "Jinan": "济南",
    "Harbin": "哈尔滨", "Kunming": "昆明", "Guiyang": "贵阳", "Nanning": "南宁",
    "Hefei": "合肥", "Nanchang": "南昌", "Taiyuan": "太原", "Shijiazhuang": "石家庄",
    "Changchun": "长春", "Urumqi": "乌鲁木齐", "Lanzhou": "兰州", "Hohhot": "呼和浩特",
    "Yinchuan": "银川", "Xining": "西宁", "Lhasa": "拉萨", "Haikou": "海口",
    "Sanya": "三亚", "Ningbo": "宁波", "Wenzhou": "温州", "Wuxi": "无锡",
    "Changzhou": "常州", "Nantong": "南通", "Yangzhou": "扬州", "Xuzhou": "徐州",
    "Yantai": "烟台", "Weihai": "威海", "Linyi": "临沂", "Jining": "济宁",
    "Zhuhai": "珠海", "Shantou": "汕头", "Zhanjiang": "湛江", "Meizhou": "梅州",
    "Jieyang": "揭阳", "Quanzhou": "泉州", "Wuyishan": "武夷山", "Guilin": "桂林",
    "Liuzhou": "柳州", "Beihai": "北海", "Zunyi": "遵义", "Mianyang": "绵阳",
    "Yibin": "宜宾", "Luzhou": "泸州", "Nanchong": "南充", "Wanzhou": "万州",
    "Dazhou": "达州", "Xichang": "西昌", "Lijiang": "丽江", "Dali": "大理",
    "Jinghong": "西双版纳", "Tengchong": "腾冲", "Baoshan": "保山", "Diqing": "迪庆",
    "Yiwu": "义乌", "Huangyan": "台州", "Zhoushan": "舟山", "Quzhou": "衢州",
    "Yancheng": "盐城", "Yangzhou": "扬州", "Huai'an": "淮安", "Lianyungang": "连云港",
    "Tunxi": "黄山", "Anqing": "安庆", "Fuyang": "阜阳", "Huangshan": "黄山",
    "Luoyang": "洛阳", "Nanyang": "南阳", "Xiangyang": "襄阳", "Yichang": "宜昌",
    "Enshi": "恩施", "Shiyan": "十堰", "Changde": "常德", "Hengyang": "衡阳",
    "Zhangjiajie": "张家界", "Huaihua": "怀化", "Yongzhou": "永州",
    "Ganzhou": "赣州", "Jingdezhen": "景德镇", "Jiujiang": "九江", "Yichun": "宜春",
    "Hong Kong": "香港", "Macau": "澳门", "Macao": "澳门", "Taipei": "台北",
    "Kaohsiung": "高雄", "Taichung": "台中", "Tainan": "台南",
    "Dongguan": "东莞", "Foshan": "佛山", "Huizhou": "惠州", "Zhongshan": "中山",
    "Suzhou": "苏州", "Wuhan": "武汉", "Datong": "大同", "Baotou": "包头",
    "Ordos": "鄂尔多斯", "Chifeng": "赤峰", "Tongliao": "通辽", "Hailar": "海拉尔",
    "Mudanjiang": "牡丹江", "Jiamusi": "佳木斯", "Qiqihar": "齐齐哈尔",
    "Yanji": "延吉", "Dandong": "丹东", "Jinzhou": "锦州", "Anshan": "鞍山",
    "Weifang": "潍坊", "Rizhao": "日照", "Dongying": "东营", "Heze": "菏泽",
}


def _to_float(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


@dataclass(frozen=True)
class Runway:
    """一条跑道。用两端经纬度描述，因此能反映真实布局（平行 / 交叉 / 方位）。"""

    le_ident: str
    he_ident: str
    le_lat: float
    le_lon: float
    he_lat: float
    he_lon: float
    length_ft: float
    width_ft: float
    le_heading: float

    @property
    def length_m(self) -> float:
        return self.length_ft * 0.3048


@dataclass
class Airport:
    """一个机场及其跑道。"""

    ident: str                      #: ICAO 代码（OurAirports 的 ident）
    name: str
    city: str
    country: str
    lat: float
    lon: float
    kind: str                       #: large_airport / medium_airport / ...
    iata: str = ""
    scheduled: bool = False
    runways: list[Runway] = field(default_factory=list)

    @property
    def city_zh(self) -> str:
        """城市中文名；未收录时回退英文原名（已剥离括号补充）。"""
        base = base_city(self.city)
        return _CITY_ZH.get(base, base) or base

    @property
    def codes(self) -> str:
        """``PEK / ZBAA`` 形式的代码串，缺一项时只显示有值的那个。"""
        parts = [code for code in (self.iata, self.ident) if code]
        return " / ".join(parts)

    @property
    def label(self) -> str:
        """列表显示用：``北京 · 北京首都国际机场 · PEK/ZBAA``。"""
        city = self.city_zh or "—"
        return f"{city} · {self.name} · {self.codes}"

    @property
    def sort_key(self) -> tuple[str, str]:
        """按城市名（英文，等价于拼音首字母）排序，同城再按机场名。"""
        return (base_city(self.city) or self.name, self.name)


class AirportDatabase:
    """机场与跑道数据的加载、查询。"""

    def __init__(self, data_dir: Path | str | None = None) -> None:
        self.data_dir = Path(data_dir) if data_dir else DATA_DIR
        self._airports: list[Airport] = []
        self._loaded = False

    # ------------------------------------------------------------------
    # 数据准备
    # ------------------------------------------------------------------

    def data_ready(self) -> bool:
        return (
            (self.data_dir / _AIRPORTS_FILE).exists()
            and (self.data_dir / _RUNWAYS_FILE).exists()
        )

    def download_missing(self, timeout: float = 240.0) -> tuple[bool, str]:
        """下载机场与跑道数据（合计约 16 MB）。"""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        failures: list[str] = []

        for name, url, minimum in (
            (_AIRPORTS_FILE, _AIRPORTS_URL, _MIN_AIRPORTS_BYTES),
            (_RUNWAYS_FILE, _RUNWAYS_URL, _MIN_RUNWAYS_BYTES),
        ):
            target = self.data_dir / name
            if target.exists() and target.stat().st_size >= minimum:
                continue
            try:
                response = requests.get(
                    url, timeout=timeout, headers={"User-Agent": _USER_AGENT}
                )
                if response.status_code != 200 or len(response.content) < minimum:
                    failures.append(f"{name}(HTTP {response.status_code})")
                    continue
                temp = target.with_name(target.name + ".part")
                temp.write_bytes(response.content)
                temp.replace(target)
            except (requests.RequestException, OSError) as exc:
                failures.append(f"{name}({type(exc).__name__})")

        if failures:
            return False, "机场数据下载失败：" + "、".join(failures)
        return True, "机场数据就绪"

    def load(self) -> bool:
        """载入机场数据（优先走 pickle 缓存）。"""
        if self._loaded:
            return bool(self._airports)

        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache = CACHE_DIR / f"airports_v{_CACHE_VERSION}.pickle"
        source_newest = 0.0
        for name in (_AIRPORTS_FILE, _RUNWAYS_FILE):
            path = self.data_dir / name
            if path.exists():
                source_newest = max(source_newest, path.stat().st_mtime)

        if cache.exists() and source_newest and cache.stat().st_mtime >= source_newest:
            try:
                with cache.open("rb") as handle:
                    airports = pickle.load(handle)
                if isinstance(airports, list):
                    self._airports = airports
                    self._loaded = True
                    return bool(airports)
            except (OSError, pickle.PickleError, EOFError, AttributeError):
                pass

        self._airports = self._parse()
        self._loaded = True
        try:
            with cache.open("wb") as handle:
                pickle.dump(self._airports, handle, protocol=pickle.HIGHEST_PROTOCOL)
        except OSError:
            pass
        return bool(self._airports)

    def _parse(self) -> list[Airport]:
        """解析 CSV，把跑道挂到对应机场上。"""
        airports_path = self.data_dir / _AIRPORTS_FILE
        runways_path = self.data_dir / _RUNWAYS_FILE
        if not airports_path.exists():
            return []

        airports: dict[str, Airport] = {}
        with airports_path.open(encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                lat = _to_float(row.get("latitude_deg"))
                lon = _to_float(row.get("longitude_deg"))
                ident = (row.get("ident") or "").strip()
                if not ident or lat is None or lon is None:
                    continue
                airports[ident] = Airport(
                    ident=ident,
                    name=(row.get("name") or "").strip(),
                    city=(row.get("municipality") or "").strip(),
                    country=(row.get("iso_country") or "").strip(),
                    lat=lat,
                    lon=lon,
                    kind=(row.get("type") or "").strip(),
                    iata=(row.get("iata_code") or "").strip().upper(),
                    scheduled=(row.get("scheduled_service") or "").strip() == "yes",
                )

        if runways_path.exists():
            with runways_path.open(encoding="utf-8", newline="") as handle:
                for row in csv.DictReader(handle):
                    airport = airports.get((row.get("airport_ident") or "").strip())
                    if airport is None:
                        continue
                    le_lat = _to_float(row.get("le_latitude_deg"))
                    le_lon = _to_float(row.get("le_longitude_deg"))
                    he_lat = _to_float(row.get("he_latitude_deg"))
                    he_lon = _to_float(row.get("he_longitude_deg"))
                    if None in (le_lat, le_lon, he_lat, he_lon):
                        continue
                    airport.runways.append(Runway(
                        le_ident=(row.get("le_ident") or "").strip(),
                        he_ident=(row.get("he_ident") or "").strip(),
                        le_lat=le_lat, le_lon=le_lon,
                        he_lat=he_lat, he_lon=he_lon,
                        length_ft=_to_float(row.get("length_ft")) or 0.0,
                        width_ft=_to_float(row.get("width_ft")) or 0.0,
                        le_heading=_to_float(row.get("le_heading_degT")) or 0.0,
                    ))

        return list(airports.values())

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def count(self) -> int:
        return len(self._airports)

    def with_runways(self) -> list[Airport]:
        """所有带跑道数据的机场。"""
        return [airport for airport in self._airports if airport.runways]

    def scheduled_airports(self, countries: tuple[str, ...] = CHINA_REGIONS) -> list[Airport]:
        """在运营的民航机场，按城市名排序。

        这是"快速跳转"列表的数据源：只看定期航班机场（``scheduled_service``），
        类型限定为大型与中型。这样能自然排除掉军用机场——
        实测被排除的 109 个里绝大多数名字就带 ``Air Base``。
        """
        result = [
            airport for airport in self._airports
            if airport.country in countries
            and airport.kind in _SCHEDULED_TYPES
            and airport.scheduled
        ]
        result.sort(key=lambda airport: airport.sort_key)
        return result

    def nearby(self, lat: float, lon: float, radius_km: float) -> list[Airport]:
        """雷达视野内的机场（含跑道），按距离升序。"""
        from app.geo import haversine_km

        result = []
        for airport in self.with_runways():
            distance = haversine_km(lat, lon, airport.lat, airport.lon)
            if distance <= radius_km:
                result.append(airport)
        result.sort(key=lambda airport: haversine_km(lat, lon, airport.lat, airport.lon))
        return result
