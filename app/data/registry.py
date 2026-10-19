"""本地注册表：宽体机型、生产商官网、特殊机型/涂装、航司兜底名。

这些是"静态知识"，不属于任何数据源，因此内置在本地。

关于宽体判定：实测 adsb.lol 返回的 ``category`` 字段**不可靠**
（实例：``CPA459 / B773 / category=A0``，而 B773 是典型宽体机），
因此宽体判定一律以 ICAO 机型代码为准，``category`` 只作辅助补充。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from app.config import PROJECT_ROOT

# ---------------------------------------------------------------------------
# 宽体机型（ICAO Type Designator）
# ---------------------------------------------------------------------------
# 判定标准：双通道客机或大型货机/运输机。窄体机（737/A320/E190/CRJ 等）
# 刻意不在表内，它们才是绝大多数。
WIDEBODY_TYPES: frozenset[str] = frozenset({
    # ---- Boeing ----
    "B701", "B703", "B720",                     # 707 系列
    "B741", "B742", "B743", "B744", "B748",     # 747 系列
    "BLCF",                                     # 747 Dreamlifter 特种运输机
    "B762", "B763", "B764",                     # 767
    "B772", "B773", "B77L", "B77W",             # 777-200/300/200LR/300ER
    "B778", "B779",                             # 777-8 / 777-9
    "B788", "B789", "B78X",                     # 787-8 / -9 / -10
    # ---- Airbus ----
    "A306", "A30B", "A310",                     # A300 / A310
    "A332", "A333", "A337", "A338", "A339",     # A330 全系
    "A342", "A343", "A345", "A346",             # A340 全系
    "A359", "A35K",                             # A350-900 / -1000
    "A388",                                     # A380
    "A3ST",                                     # A300-600ST 大白鲸
    "A400",                                     # A400M 军用运输机
    # ---- 其他制造商 ----
    "IL86", "IL96",                             # 伊尔-86 / 伊尔-96
    "AN22", "AN124", "AN225",                   # 安托诺夫重型运输机
    "DC10", "MD11", "L1011",                    # 老式三发宽体
    "C5M", "C17",                               # 军用战略/战术运输机
})

# ADS-B 发射类别码的辅助判据，仅在机型代码缺失时兜底使用
_WIDEBODY_CATEGORY = "A5"


def is_widebody(type_code: str | None, category: str | None = None) -> bool:
    """判断是否宽体机。

    优先采信 ICAO 机型代码；代码缺失时退回 ADS-B 类别码 ``A5``（Large）。
    """
    if type_code:
        if type_code.strip().upper() in WIDEBODY_TYPES:
            return True
        # 机型代码明确给出了，就以它为准，不再看 category
        return False
    return (category or "").strip().upper() == _WIDEBODY_CATEGORY


# ---------------------------------------------------------------------------
# 机型形态分类（决定雷达图上的图标形状）
# ---------------------------------------------------------------------------
# 需求要求"按机型分类图标，比如几发、宽窄"，因此这里把机型归纳成
# 「机身类别 + 发动机数量」两组可绘制的属性。

#: 机身类别 → 中文名
KIND_LABELS: dict[str, str] = {
    "narrow": "窄体",
    "wide": "宽体",
    "regional": "支线",
    "turboprop": "涡桨",
    "helicopter": "直升机",
    "business": "公务机",
    "military": "军用运输",
    "light": "轻型",
}

_ENGINE_LABELS = {0: "", 1: "单发", 2: "双发", 3: "三发", 4: "四发", 6: "六发", 8: "八发"}


@dataclass(frozen=True)
class AircraftKind:
    """机型的图标形态：机身类别 + 发动机数量。"""

    key: str
    engines: int

    @property
    def is_widebody(self) -> bool:
        return self.key in ("wide", "military")

    @property
    def label(self) -> str:
        """中文说明，如「双发宽体」「四发宽体」。"""
        body = KIND_LABELS.get(self.key, "未知")
        if self.key == "helicopter":
            return body  # 直升机不谈"几发"
        return f"{_ENGINE_LABELS.get(self.engines, f'{self.engines}发')}{body}"


_DEFAULT_KIND = AircraftKind("narrow", 2)

#: 机型码前缀 → (机身类别, 发动机数量)。
#:
#: 用前缀而非精确匹配，是因为数据源返回的机型码常带后缀：
#: 实测遇到过 ``A321-251NX``、``737NG 8HX/W``、``777 367ER``、``A330 343E``。
#:
#: **顺序在下方由代码按长度降序重排**，不靠人工维护——
#: 长前缀必须先命中，否则 ``C17`` 会抢走 ``C172``、``A31`` 会抢走 ``A310``。
_KIND_PREFIXES_UNSORTED: tuple[tuple[str, str, int], ...] = (
    # ---- 六发 / 八发（极罕见，但既然要分类就该分对）----
    ("AN225", "wide", 6),
    ("B52", "military", 8),
    # ---- 四发 ----
    ("A388", "wide", 4),
    ("A380", "wide", 4),
    ("A346", "wide", 4),
    ("A345", "wide", 4),
    ("A343", "wide", 4),
    ("A342", "wide", 4),
    ("A340", "wide", 4),
    ("BLCF", "wide", 4),
    ("C130", "military", 4),
    ("C5M", "military", 4),
    ("C17", "military", 4),
    ("A400", "military", 4),
    ("IL76", "military", 4),
    ("IL86", "wide", 4),
    ("IL96", "wide", 4),
    ("AN124", "wide", 4),
    ("AN22", "wide", 4),
    ("BA11", "regional", 4),
    ("B461", "regional", 4),
    ("B462", "regional", 4),
    ("B463", "regional", 4),
    ("RJ85", "regional", 4),
    ("RJ1H", "regional", 4),
    ("747", "wide", 4),
    ("B74", "wide", 4),
    # ---- 三发 ----
    ("L1011", "wide", 3),
    ("MD11", "wide", 3),
    ("DC10", "wide", 3),
    ("F900", "business", 3),
    ("F2TH", "business", 3),
    ("FA7X", "business", 3),
    ("FA8X", "business", 3),
    ("F7X", "business", 3),
    ("F8X", "business", 3),
    ("727", "narrow", 3),
    ("B72", "narrow", 3),
    # ---- 宽体双发 ----
    ("777", "wide", 2),
    ("787", "wide", 2),
    ("767", "wide", 2),
    ("A3ST", "wide", 2),
    ("A310", "wide", 2),
    ("A330", "wide", 2),
    ("A350", "wide", 2),
    ("B77", "wide", 2),
    ("B78", "wide", 2),
    ("B76", "wide", 2),
    ("A33", "wide", 2),
    ("A35", "wide", 2),
    ("A30", "wide", 2),
    # ---- 窄体双发 ----
    ("B3XM", "narrow", 2),
    ("C919", "narrow", 2),
    ("SU95", "narrow", 2),
    ("BCS1", "narrow", 2),
    ("BCS3", "narrow", 2),
    ("737", "narrow", 2),
    ("757", "narrow", 2),
    ("717", "narrow", 2),
    ("MD8", "narrow", 2),
    ("MD9", "narrow", 2),
    ("DC9", "narrow", 2),
    ("F100", "narrow", 2),
    ("T204", "narrow", 2),
    ("T214", "narrow", 2),
    ("B73", "narrow", 2),
    ("B75", "narrow", 2),
    ("B71", "narrow", 2),
    ("B38", "narrow", 2),
    ("B39", "narrow", 2),
    ("B37", "narrow", 2),
    ("A32", "narrow", 2),
    ("A31", "narrow", 2),   # 排在 A310 之后，命中 A318/A319
    ("A19", "narrow", 2),
    ("A20", "narrow", 2),
    ("A21", "narrow", 2),
    ("A22", "narrow", 2),
    ("F70", "narrow", 2),
    # ---- 支线喷气 ----
    ("CRJ1", "regional", 2),
    ("CRJ2", "regional", 2),
    ("CRJ7", "regional", 2),
    ("CRJ9", "regional", 2),
    ("CRJX", "regional", 2),
    ("E135", "regional", 2),
    ("E145", "regional", 2),
    ("E170", "regional", 2),
    ("E175", "regional", 2),
    ("E190", "regional", 2),
    ("E195", "regional", 2),
    ("E75L", "regional", 2),
    ("E75S", "regional", 2),
    ("E290", "regional", 2),
    ("E295", "regional", 2),
    ("ERJ", "regional", 2),
    # ---- 涡桨 ----
    ("AT43", "turboprop", 2),
    ("AT45", "turboprop", 2),
    ("AT72", "turboprop", 2),
    ("AT75", "turboprop", 2),
    ("AT76", "turboprop", 2),
    ("DH8", "turboprop", 2),
    ("DHC", "turboprop", 2),
    ("SF34", "turboprop", 2),
    ("SB20", "turboprop", 2),
    ("MA60", "turboprop", 2),
    ("Y12", "turboprop", 2),
    ("D228", "turboprop", 2),
    ("D328", "turboprop", 2),
    ("BE20", "turboprop", 2),
    ("L410", "turboprop", 2),
    ("AT7", "turboprop", 2),
    ("AT4", "turboprop", 2),
    ("F50", "turboprop", 2),
    ("F27", "turboprop", 2),
    ("C208", "turboprop", 1),
    ("PC12", "turboprop", 1),
    ("TBM7", "turboprop", 1),
    ("TBM8", "turboprop", 1),
    ("TBM9", "turboprop", 1),
    # ---- 公务机 ----
    ("GLF", "business", 2),
    ("GA5C", "business", 2),
    ("GA6C", "business", 2),
    ("GA7C", "business", 2),
    ("G280", "business", 2),
    ("G550", "business", 2),
    ("G650", "business", 2),
    ("CL30", "business", 2),
    ("CL35", "business", 2),
    ("CL60", "business", 2),
    ("C25A", "business", 2),
    ("C25B", "business", 2),
    ("C25C", "business", 2),
    ("C56X", "business", 2),
    ("C68A", "business", 2),
    ("C700", "business", 2),
    ("C750", "business", 2),
    ("LJ35", "business", 2),
    ("LJ45", "business", 2),
    ("LJ60", "business", 2),
    ("HDJT", "business", 2),
    ("PC24", "business", 2),
    ("BE40", "business", 2),
    ("E50P", "business", 2),
    ("E55P", "business", 2),
    # ---- 直升机（发动机数记 0，图标形态完全不同）----
    ("AW139", "helicopter", 0),
    ("AW189", "helicopter", 0),
    ("AW101", "helicopter", 0),
    ("EC35", "helicopter", 0),
    ("EC45", "helicopter", 0),
    ("EC55", "helicopter", 0),
    ("EC75", "helicopter", 0),
    ("H125", "helicopter", 0),
    ("H130", "helicopter", 0),
    ("H135", "helicopter", 0),
    ("H145", "helicopter", 0),
    ("H155", "helicopter", 0),
    ("H160", "helicopter", 0),
    ("H175", "helicopter", 0),
    ("H215", "helicopter", 0),
    ("H225", "helicopter", 0),
    ("B407", "helicopter", 0),
    ("B412", "helicopter", 0),
    ("B429", "helicopter", 0),
    ("B505", "helicopter", 0),
    ("B06", "helicopter", 0),
    ("AS50", "helicopter", 0),
    ("AS55", "helicopter", 0),
    ("AS65", "helicopter", 0),
    ("A109", "helicopter", 0),
    ("A119", "helicopter", 0),
    ("A139", "helicopter", 0),
    ("A169", "helicopter", 0),
    ("A189", "helicopter", 0),
    ("S76", "helicopter", 0),
    ("S92", "helicopter", 0),
    ("R22", "helicopter", 0),
    ("R44", "helicopter", 0),
    ("R66", "helicopter", 0),
    ("UH1", "helicopter", 0),
    ("MI8", "helicopter", 0),
    ("MI17", "helicopter", 0),
    ("MI24", "helicopter", 0),
    ("KA32", "helicopter", 0),
    ("H60", "helicopter", 0),
    ("H47", "helicopter", 0),
    ("EC1", "helicopter", 0),
    # ---- 轻型 ----
    ("C172", "light", 1),
    ("C182", "light", 1),
    ("C206", "light", 1),
    ("SR22", "light", 1),
    ("PA28", "light", 1),
    ("DA40", "light", 1),
    ("DA42", "light", 2),
    ("BE36", "light", 1),
    ("PA34", "light", 2),
    ("M20", "light", 1),
)

#: 按前缀长度降序重排。长前缀必须先命中，否则 ``C17`` 会抢走 ``C172``、
#: ``A31`` 会抢走 ``A310``。交给代码排序比人工维护可靠得多。
_KIND_PREFIXES: tuple[tuple[str, str, int], ...] = tuple(
    sorted(_KIND_PREFIXES_UNSORTED, key=lambda rule: -len(rule[0]))
)

#: 描述性机型文本的兜底关键词（同样按长度降序）。
#: 部分数据源的机型字段是 ``"ATR 72-600"``、``"Cessna 172"`` 这类文字，
#: 前缀匹配对它们无能为力，只能退回子串匹配。
_KIND_KEYWORDS: tuple[tuple[str, str, int], ...] = tuple(
    sorted(
        (
            ("BEECHCRAFT", "light", 1),
            ("CESSNA", "light", 1),
            ("CIRRUS", "light", 1),
            ("PIPER", "light", 1),
            ("SAAB", "turboprop", 2),
            ("ATR", "turboprop", 2),
        ),
        key=lambda rule: -len(rule[0]),
    )
)


def aircraft_kind(type_code: str | None, category: str | None = None) -> AircraftKind:
    """判定机型的图标形态。

    四级回退：机型码前缀 → 描述文本关键词 → ADS-B 类别码 → 默认窄体双发。
    """
    code = (type_code or "").strip().upper()
    if code:
        for prefix, key, engines in _KIND_PREFIXES:
            if code.startswith(prefix):
                return AircraftKind(key, engines)
        for keyword, key, engines in _KIND_KEYWORDS:
            if keyword in code:
                return AircraftKind(key, engines)

    # 机型码缺失或未收录时，用 ADS-B 发射类别兜底
    if category:
        kind_code = category.strip().upper()
        if kind_code == "A7":
            return AircraftKind("helicopter", 0)
        if kind_code in ("A1", "A2"):
            return AircraftKind("light", 1)
        if kind_code == "A5":
            return AircraftKind("wide", 2)

    return _DEFAULT_KIND


# ---------------------------------------------------------------------------
# 生产商官网（详情页点击"生产商"时打开）
# ---------------------------------------------------------------------------
MANUFACTURER_URLS: dict[str, str] = {
    "boeing": "https://www.boeing.com",
    "airbus": "https://www.airbus.com",
    "embraer": "https://embraer.com",
    "bombardier": "https://bombardier.com",
    "atr": "https://www.atr-aircraft.com",
    "comac": "https://www.comac.cc",
    "中国商飞": "https://www.comac.cc",
    "ilyushin": "https://www.ilyushin.org",
    "伊留申": "https://www.ilyushin.org",
    "tupolev": "https://www.tupolev.ru",
    "图波列夫": "https://www.tupolev.ru",
    "antonov": "https://www.antonov.com",
    "安托诺夫": "https://www.antonov.com",
    "sukhoi": "https://www.sukhoi.org",
    "苏霍伊": "https://www.sukhoi.org",
    "gulfstream": "https://www.gulfstream.com",
    "cessna": "https://cessna.txtav.com",
    "de havilland": "https://dehavilland.com",
    "pilatus": "https://www.pilatus-aircraft.com",
    "saab": "https://www.saab.com",
    "lockheed": "https://www.lockheedmartin.com",
    "mcdonnell douglas": "https://www.boeing.com",
    "airbus helicopters": "https://www.airbus.com/en/products-services/helicopters",
}


def manufacturer_url(manufacturer: str | None) -> str | None:
    """按生产商名查官网。未收录时返回 ``None``，界面据此禁用点击。"""
    if not manufacturer:
        return None
    key = manufacturer.strip().lower()
    if key in MANUFACTURER_URLS:
        return MANUFACTURER_URLS[key]
    # 处理 "The Boeing Company" 这类带前后缀的写法
    for name, url in MANUFACTURER_URLS.items():
        if name in key:
            return url
    return None


# ---------------------------------------------------------------------------
# 特殊机型（客观的机型特征，可自动识别）
# ---------------------------------------------------------------------------
SPECIAL_TYPES: dict[str, str] = {
    "A388": "空中客车 A380 · 全球最大双层客机",
    "A3ST": "空中客车大白鲸 · A300-600ST 特种运输机",
    "A337": "空中客车大白鲸 XL · A330-700ST 特种运输机",
    "BLCF": "波音 747 Dreamlifter · 特种超大件运输机",
    "AN124": "安托诺夫 An-124 · 重型战略运输机",
    "AN225": "安托诺夫 An-225 梦想 · 全球最大运输机",
    "AN22": "安托诺夫 An-22 安泰 · 涡桨重型运输机",
    "C5M": "洛克希德 C-5M 银河 · 军用战略运输机",
    "C17": "波音 C-17 环球霸王 III · 军用运输机",
    "A400": "空中客车 A400M · 军用运输机",
    "IL96": "伊留申 Il-96 · 宽体客机",
    "IL86": "伊留申 Il-86 · 宽体客机",
    "MD11": "麦克唐纳-道格拉斯 MD-11 · 三发宽体机",
    "DC10": "麦克唐纳-道格拉斯 DC-10 · 三发宽体机",
    "L1011": "洛克希德 L-1011 三星 · 三发宽体机",
    "B703": "波音 707-300 · 早期四发窄体/军用平台",
}


def special_type_note(type_code: str | None) -> str | None:
    """返回特殊机型说明；属常见机型则返回 ``None``。"""
    if not type_code:
        return None
    return SPECIAL_TYPES.get(type_code.strip().upper())


# ---------------------------------------------------------------------------
# 特殊涂装（外部可维护表）
# ---------------------------------------------------------------------------
# 公开数据源没有任何"特殊涂装"API，因此这里做成外部 JSON 由用户自主维护。
# 默认表为空——不预填任何未经核实的注册号，避免展示编造的数据。
SPECIAL_LIVERIES_PATH = PROJECT_ROOT / "assets" / "special_liveries.json"

_SPECIAL_LIVERIES_SKELETON = {
    "_说明": (
        "特殊涂装注册号对照表。键为飞机注册号（大写，如 B-2035），值为涂装说明。"
        "以 _ 开头的键为注释，不参与匹配。"
    ),
    "_填写示例": {
        "B-XXXX": "涂装名称 · 简短说明（请核实后填写，勿凭猜测录入）"
    },
}


@lru_cache(maxsize=1)
def _load_special_liveries() -> dict[str, str]:
    """读取特殊涂装表。文件不存在时写出骨架文件并返回空表。"""
    path = SPECIAL_LIVERIES_PATH
    if not path.exists():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(_SPECIAL_LIVERIES_SKELETON, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass
        return {}

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(raw, dict):
        return {}

    # 忽略以 _ 开头的注释键
    return {
        str(key).strip().upper(): str(value)
        for key, value in raw.items()
        if not str(key).startswith("_") and str(value).strip()
    }


def reload_special_liveries() -> None:
    """清空缓存，供用户在设置面板修改文件后立即生效。"""
    _load_special_liveries.cache_clear()


def special_livery(registration: str | None) -> str | None:
    """按注册号查特殊涂装说明。"""
    if not registration:
        return None
    return _load_special_liveries().get(registration.strip().upper())


# ---------------------------------------------------------------------------
# 航司名兜底表
# ---------------------------------------------------------------------------
# 正常情况航司名由 adsbdb 提供；富化失败时用本表按呼号前缀兜底，
# 至少让榜单不至于只剩一串三字代码。
AIRLINE_BY_PREFIX: dict[str, str] = {
    # 中国大陆
    "CCA": "中国国际航空", "CSN": "中国南方航空", "CES": "中国东方航空",
    "CHH": "海南航空", "CSZ": "深圳航空", "CXA": "厦门航空", "CQH": "春秋航空",
    "CSC": "四川航空", "CQN": "重庆航空", "CDG": "山东航空", "CHB": "上海航空",
    "CBJ": "北京航空", "GCR": "天津航空", "DKH": "吉祥航空", "HXA": "华夏航空",
    "OKA": "奥凯航空", "LKE": "祥鹏航空", "RLH": "瑞丽航空", "YZA": "西部航空",
    "UBA": "乌鲁木齐航空", "FZA": "福州航空", "EPA": "东海航空",
    # 港台
    "CPA": "国泰航空", "HDA": "香港航空", "HKE": "香港快运航空",
    "CRK": "香港航空", "AHK": "香港华民航空",
    "CAL": "中华航空", "EVA": "长荣航空", "MDA": "华信航空",
    "SJX": "星宇航空", "TTW": "台湾虎航",
    "AMU": "澳门航空",
    # 亚太
    "JAL": "日本航空", "ANA": "全日空", "APJ": "乐桃航空", "SJO": "春秋航空日本",
    "KAL": "大韩航空", "AAR": "韩亚航空", "JJA": "济州航空", "ABL": "釜山航空",
    "SIA": "新加坡航空", "SLK": "酷航", "THA": "泰国国际航空",
    "AIQ": "泰国亚洲航空", "MAS": "马来西亚航空", "AXM": "亚洲航空",
    "GIA": "鹰航", "PAL": "菲律宾航空", "CEB": "宿务太平洋航空",
    "VJC": "越捷航空", "HVN": "越南航空", "AIC": "印度航空",
    "IGO": "靛蓝航空", "SEJ": "香料航空",
    # 中东 / 非洲
    "UAE": "阿联酋航空", "QTR": "卡塔尔航空", "ETD": "阿提哈德航空",
    "SVA": "沙特航空", "THY": "土耳其航空", "ELY": "以色列航空",
    "ETH": "埃塞俄比亚航空", "SAA": "南非航空", "MSR": "埃及航空",
    # 欧洲
    "BAW": "英国航空", "DLH": "汉莎航空", "AFR": "法国航空",
    "KLM": "荷兰皇家航空", "IBE": "伊比利亚航空", "ITY": "意大利航空",
    "SWR": "瑞士国际航空", "AUA": "奥地利航空", "SAS": "北欧航空",
    "FIN": "芬兰航空", "TAP": "葡萄牙航空", "AEE": "爱琴海航空",
    "RYR": "瑞安航空", "EZY": "易捷航空", "WZZ": "维兹航空",
    "AFL": "俄罗斯航空", "LOT": "波兰航空", "CSA": "捷克航空",
    # 北美 / 南美
    "UAL": "美国联合航空", "DAL": "达美航空", "AAL": "美国航空",
    "SWA": "西南航空", "JBU": "捷蓝航空", "ASA": "阿拉斯加航空",
    "ACA": "加拿大航空", "WJA": "西捷航空",
    "AMX": "墨西哥航空", "LAN": "智利航空", "ARG": "阿根廷航空",
    # 货运
    "FDX": "联邦快递", "UPS": "UPS 航空", "GTI": "亚特拉斯航空",
    "CLX": "卢森堡货运", "CAO": "中国国际货运航空", "CKK": "中国货运航空",
    "BOX": "波音物流", "SQC": "新加坡货运",
}


def airline_from_callsign(callsign: str | None) -> str:
    """按呼号前 3 位 ICAO 航司代码查航司中文名。查不到返回空串。"""
    if not callsign:
        return ""
    prefix = callsign.strip().upper()[:3]
    return AIRLINE_BY_PREFIX.get(prefix, "")


# ---------------------------------------------------------------------------
# 时区估算（用于把 ETA 换算成目的地当地时间）
# ---------------------------------------------------------------------------
# 免费数据源不提供机场时区。这里用"国家代码 → UTC 偏移"覆盖绝大多数情况，
# 对跨时区大国（美/加/俄/澳/巴西/墨/印尼）退回按经度粗算。
_COUNTRY_UTC_OFFSETS: dict[str, float] = {
    "CN": 8, "HK": 8, "MO": 8, "TW": 8,
    "JP": 9, "KR": 9, "KP": 9,
    "SG": 8, "MY": 8, "PH": 8, "BN": 8,
    "TH": 7, "VN": 7, "KH": 7, "LA": 7,
    "IN": 5.5, "PK": 5, "BD": 6, "NP": 5.75, "LK": 5.5, "MM": 6.5,
    "AE": 4, "QA": 3, "SA": 3, "KW": 3, "BH": 3, "OM": 4,
    "IL": 2, "JO": 3, "TR": 3, "IR": 3.5, "IQ": 3,
    "GB": 0, "IE": 0, "PT": 0, "IS": 0, "MA": 1,
    "FR": 1, "DE": 1, "IT": 1, "ES": 1, "NL": 1, "BE": 1, "CH": 1,
    "AT": 1, "PL": 1, "CZ": 1, "HU": 1, "SK": 1, "SE": 1, "NO": 1,
    "DK": 1, "AL": 1, "RS": 1, "HR": 1, "SI": 1, "BA": 1, "MK": 1,
    "GR": 2, "FI": 2, "UA": 2, "RO": 2, "BG": 2, "EE": 2, "LV": 2,
    "LT": 2, "MD": 2, "CY": 2, "EG": 2, "ZA": 2, "LY": 2, "SD": 2,
    "KE": 3, "TZ": 3, "UG": 3, "SO": 3, "MG": 3,
    "NZ": 12, "FJ": 12, "PG": 10,
    # 跨时区国家：置 None 表示按经度粗算
    "US": None, "CA": None, "RU": None, "AU": None,
    "BR": None, "MX": None, "ID": None, "KZ": None,
    "AR": -3, "CL": -4, "PE": -5, "CO": -5, "VE": -4,
}


def utc_offset_hours(country_iso: str | None, lat: float | None = None, lon: float | None = None) -> float:
    """估算某地相对 UTC 的小时偏移。

    优先查国家表；国家为跨时区大国或未收录时，按经度粗算（每 15° 一小时），
    并夹到 ±14 小时的合理范围内。
    """
    if country_iso:
        code = country_iso.strip().upper()
        if code in _COUNTRY_UTC_OFFSETS:
            offset = _COUNTRY_UTC_OFFSETS[code]
            if offset is not None:
                return float(offset)

    if lon is None:
        return 0.0
    return max(-14.0, min(14.0, round(lon / 15.0)))
