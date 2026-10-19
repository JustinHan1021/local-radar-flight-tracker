"""数据源公共接口与错误类型。

各数据源只需专注于"把某一时刻的航班状态解析成 :class:`Flight` 列表"，
距离与方位的计算、富化、缓存、平滑插值全部由调度层统一处理，
因此新增数据源不必重复实现这些逻辑。
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod

from app.data.models import Flight


class SourceError(RuntimeError):
    """数据源请求失败：网络异常、被限流或响应格式不符合预期。"""


class SourceRateLimited(SourceError):
    """数据源限流（HTTP 429）。

    这是"请求太密"而不是"数据源失效"——降级到别的源毫无意义
    （反而会拿到质量差得多的数据），正确做法是保留上一帧内容、稍后重试。
    """


class SourceUnavailable(RuntimeError):
    """数据源当前不可用：未配置凭据、服务未订阅或已停止服务。"""


class SourceError(RuntimeError):
    """数据源请求失败：网络异常、被限流或响应格式不符合预期。"""


class FlightSource(ABC):
    """航班数据源接口。"""

    #: 内部标识，与配置文件 ``source`` 字段取值一致
    name: str = ""
    #: 界面显示名
    label: str = ""
    #: 单次查询可覆盖的最大半径（km）；无上限时为 ``math.inf``
    max_radius_km: float = math.inf

    @abstractmethod
    def fetch(
        self, lat: float, lon: float, radius_km: float, timeout: float = 15.0
    ) -> list[Flight]:
        """拉取半径内的航班列表。

        网络或解析失败时抛出 :class:`SourceError`；
        凭据缺失等配置性问题抛出 :class:`SourceUnavailable`，由调度层决定是否降级。
        """

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
