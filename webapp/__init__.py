"""Web 版服务端（aiohttp + PIL，**不依赖 PySide6**）。

与桌面版共用同一套数据与几何逻辑（``app/geo.py``、``app/data/*``、
``app/airports.py`` 等），只有渲染分成两半：

* 服务端预渲染**底图**为 PNG（``webapp/render.py``，PIL 实现）；
* 目标、航迹、扫描线、量程环等随帧变化的东西由客户端 Canvas 画。

模块划分见 ``docs/WEB.md``。
"""

__all__: list[str] = []
