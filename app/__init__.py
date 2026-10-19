"""本地雷达式航班追踪软件。

模块分层：
    app.geo       地理计算与雷达投影引擎（纯 Python，可独立测试）
    app.config    用户配置读写与校验
    app.theme     军用雷达暗色主题令牌与样式
    app.locate    定位（Windows 高精度 → IP → 手动校准）
    app.basemap   矢量海岸线底图加载与投影缓存
    app.data      航班数据获取、富化与调度
    app.ui        PySide6 界面层
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
