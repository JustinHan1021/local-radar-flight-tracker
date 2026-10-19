"""主窗口：装配雷达视图与信息面板，串联定位、数据源与设置。

窗口结构::

    ┌──────────────────────────────────────────────────────┐
    │  标题条：应用名 · 状态 · 刷新 · 设置                    │
    ├───────────────────────────────┬──────────────────────┤
    │                               │                      │
    │   圆形雷达主视图（75%）        │  信息面板（25%）      │
    │                               │  排行榜 / 详情 双状态  │
    └───────────────────────────────┴──────────────────────┘

底图下载与系统定位都在后台线程执行：前者首次要拉 3.2 MB，后者要等系统
定位服务回传，任一放到主线程都会让窗口启动时白屏数秒。
"""

from __future__ import annotations

import time

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from app.airports import Airport, AirportDatabase
from app.basemap import Basemap
from app.config import Config
from app.data.manager import FlightManager
from app.locate import LocationError, locate
from app.places import PlaceDatabase
from app.roads import fetch_roads
from app.terrain import fetch_terrain
from app.theme import Theme
from app.ui.airport_picker import AirportPickerDialog
from app.ui.panel_detail import FlightDetailPanel
from app.ui.panel_list import FlightListPanel
from app.ui.radar_view import RadarView
from app.ui.settings_dialog import SettingsDialog
from app.weather import fetch_cloud_field, fetch_weather


class _LocateSignals(QObject):
    """定位结果信号载体（QRunnable 不能自带信号）。"""

    done = Signal(object)   # LocationResult
    failed = Signal(str)


class _LocateTask(QRunnable):
    """后台定位任务。"""

    def __init__(self, fallback: tuple[float, float] | None, signals: _LocateSignals) -> None:
        super().__init__()
        self._fallback = fallback
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable 约定
        try:
            result = locate(prefer_windows=True, fallback=self._fallback)
        except LocationError as exc:
            self._signals.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - 定位涉及外部依赖，需全兜住
            self._signals.failed.emit(f"{type(exc).__name__}: {exc}")
        else:
            self._signals.done.emit(result)


class _BasemapSignals(QObject):
    """底图加载结果信号载体。"""

    done = Signal(bool, str)


class _BasemapTask(QRunnable):
    """后台下载并加载矢量底图。"""

    def __init__(self, basemap: Basemap, signals: _BasemapSignals) -> None:
        super().__init__()
        self._basemap = basemap
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable 约定
        try:
            self._basemap.download_missing()
            loaded = self._basemap.load()
        except Exception:  # noqa: BLE001 - 底图缺失不该阻断主流程
            loaded = False

        if loaded:
            land, coast = self._basemap.ring_counts
            text = f"底图就绪（陆地 {land} 环 / 海岸线 {coast} 环）"
        else:
            text = "底图不可用，仅显示刻度与航班"
        self._signals.done.emit(loaded, text)


class _OverlaySignals(QObject):
    """覆盖层数据加载完成的信号载体。"""

    loaded = Signal(object)


class _OverlayTask(QRunnable):
    """后台加载与视口相关的覆盖层数据。

    地形、路网、云图都要联网（单次合计 1–3 秒），必须离开主线程，
    否则每次缩放都会卡住界面。城市与机场是本地查询，顺带一起做。
    """

    def __init__(
        self,
        lat: float,
        lon: float,
        radius_km: float,
        places: PlaceDatabase,
        airports: AirportDatabase,
        signals: _OverlaySignals,
    ) -> None:
        super().__init__()
        self._lat = lat
        self._lon = lon
        self._radius_km = radius_km
        self._places = places
        self._airports = airports
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable 约定
        def quiet(loader):
            """任一图层失败都不该影响其他图层，统一吞掉异常。"""
            try:
                return loader()
            except Exception:  # noqa: BLE001
                return None

        data = {
            "center": (self._lat, self._lon),
            "radius_km": self._radius_km,
            "places": quiet(lambda: self._places.visible(self._lat, self._lon, self._radius_km)) or [],
            "airports": quiet(lambda: self._airports.nearby(self._lat, self._lon, self._radius_km)) or [],
            "terrain": quiet(lambda: fetch_terrain(self._lat, self._lon, self._radius_km)),
            "roads": quiet(lambda: fetch_roads(self._lat, self._lon, self._radius_km)),
            "cloud": quiet(lambda: fetch_cloud_field(self._lat, self._lon, self._radius_km)),
        }
        self._signals.loaded.emit(data)


class _DatabaseSignals(QObject):
    """基础数据（地名 / 机场）加载完成的信号载体。"""

    loaded = Signal(bool)


class _DatabaseTask(QRunnable):
    """后台下载并加载地名与机场数据（合计约 35 MB，首次运行需要下载）。

    两者都是本地查询用的静态数据，与视口无关，因此在启动时加载一次即可。
    """

    def __init__(
        self,
        places: PlaceDatabase,
        airports: AirportDatabase,
        signals: _DatabaseSignals,
    ) -> None:
        super().__init__()
        self._places = places
        self._airports = airports
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable 约定
        ok = False
        try:
            self._places.download_missing()
            ok = self._places.load() or ok
        except Exception:  # noqa: BLE001
            pass
        try:
            self._airports.download_missing()
            ok = self._airports.load() or ok
        except Exception:  # noqa: BLE001
            pass
        self._signals.loaded.emit(ok)


class _WeatherSignals(QObject):
    """天气加载完成的信号载体。"""

    loaded = Signal(object)


class _WeatherTask(QRunnable):
    """后台获取当前位置的天气。只与用户位置相关，不随视口变化。"""

    def __init__(self, lat: float, lon: float, signals: _WeatherSignals) -> None:
        super().__init__()
        self._lat = lat
        self._lon = lon
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable 约定
        try:
            weather = fetch_weather(self._lat, self._lon)
        except Exception:  # noqa: BLE001
            weather = None
        self._signals.loaded.emit(weather)


class HeaderBar(QWidget):
    """顶部标题条。"""

    refresh_clicked = Signal()
    settings_clicked = Signal()
    airports_clicked = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("HeaderBar")
        # 自定义 QWidget 子类需要显式开启样式表背景绘制
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(40)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 0, 12, 0)
        layout.setSpacing(12)

        title = QLabel("本地雷达航班追踪")
        title.setObjectName("SectionTitle")

        self._status = QLabel("正在启动…")
        self._status.setObjectName("CaptionText")

        airports = QPushButton("机场")
        airports.setObjectName("GhostButton")
        airports.setCursor(Qt.CursorShape.PointingHandCursor)
        airports.setToolTip("按城市名浏览全国在运营的民航机场，选中即跳转")
        airports.clicked.connect(self.airports_clicked)

        refresh = QPushButton("刷新")
        refresh.setObjectName("GhostButton")
        refresh.setCursor(Qt.CursorShape.PointingHandCursor)
        refresh.clicked.connect(self.refresh_clicked)

        settings = QPushButton("设置")
        settings.setObjectName("GhostButton")
        settings.setCursor(Qt.CursorShape.PointingHandCursor)
        settings.clicked.connect(self.settings_clicked)

        layout.addWidget(title)
        layout.addSpacing(14)
        layout.addWidget(self._status)
        layout.addStretch(1)
        layout.addWidget(airports)
        layout.addWidget(refresh)
        layout.addWidget(settings)

    def set_status(self, text: str) -> None:
        self._status.setText(text)


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(self, config: Config, theme: Theme) -> None:
        super().__init__()
        self._config = config
        self._theme = theme
        self._basemap = Basemap()
        #: 覆盖层数据源（地名 / 机场），在后台加载
        self._places = PlaceDatabase()
        self._airports = AirportDatabase()
        self._airports_ready = False
        self._manager: FlightManager | None = None
        self._detail_id = ""

        #: 上一次已加载覆盖层的视口，用于避免重复请求
        self._overlay_center: tuple[float, float] | None = None
        self._overlay_radius = 0.0
        self._overlay_busy = False

        self.setWindowTitle("本地雷达航班追踪")
        self.setMinimumSize(1200, 760)
        self.resize(1500, 950)

        # 缩放时连续触发会拖垮网络，这里做防抖：停下来 400 ms 才真正加载
        self._overlay_timer = QTimer(self)
        self._overlay_timer.setSingleShot(True)
        self._overlay_timer.setInterval(400)
        self._overlay_timer.timeout.connect(self._load_overlays)

        # 天气 15 分钟刷新一次即可
        self._weather_timer = QTimer(self)
        self._weather_timer.setInterval(15 * 60 * 1000)
        self._weather_timer.timeout.connect(self._refresh_weather)

        self._build_ui()
        self._connect_signals()

    # ==================================================================
    # 装配
    # ==================================================================

    def _build_ui(self) -> None:
        self._radar = RadarView(self._config, self._theme, self._basemap)
        self._list_panel = FlightListPanel(self._config, self._theme)
        self._detail_panel = FlightDetailPanel(self._config, self._theme)

        self._stack = QStackedWidget()
        self._stack.setObjectName("Panel")
        self._stack.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._stack.addWidget(self._list_panel)
        self._stack.addWidget(self._detail_panel)
        self._stack.setCurrentWidget(self._list_panel)

        self._header = HeaderBar()

        divider = QFrame()
        divider.setObjectName("VDivider")
        divider.setFixedWidth(1)

        body = QWidget()
        body_layout = QHBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(0)
        # 拉伸系数 3 : 1 即需求要求的 75% : 25%
        body_layout.addWidget(self._radar, 3)
        body_layout.addWidget(divider)
        body_layout.addWidget(self._stack, 1)

        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._header)
        root.addWidget(body, 1)
        self.setCentralWidget(central)

    def _connect_signals(self) -> None:
        self._header.refresh_clicked.connect(self._refresh_now)
        self._header.settings_clicked.connect(self._open_settings)
        self._header.airports_clicked.connect(self._open_airport_picker)

        self._radar.flight_selected.connect(self._on_radar_selection)
        self._radar.radius_changed.connect(self._on_radius_changed)
        self._radar.home_requested.connect(self._go_home)

        self._list_panel.flight_activated.connect(self._show_detail)
        self._detail_panel.back_requested.connect(self._show_list)

        self._overlay_signals = _OverlaySignals()
        self._overlay_signals.loaded.connect(self._on_overlays_loaded)
        self._weather_signals = _WeatherSignals()
        self._weather_signals.loaded.connect(self._on_weather_loaded)

    # ==================================================================
    # 启动
    # ==================================================================

    def start(self) -> None:
        """显示窗口并在后台启动底图、基础数据与定位。"""
        self.show()

        self._database_signals = _DatabaseSignals()
        self._database_signals.loaded.connect(self._on_database_ready)
        QThreadPool.globalInstance().start(_DatabaseTask(
            self._places, self._airports, self._database_signals
        ))

        self._basemap_signals = _BasemapSignals()
        self._basemap_signals.done.connect(self._on_basemap_ready)
        QThreadPool.globalInstance().start(
            _BasemapTask(self._basemap, self._basemap_signals)
        )

        manual = self._config.manual_location()
        if self._config.location_mode == "manual" and manual is not None:
            self._header.set_status(
                f"使用手动校准坐标 {manual[0]:.4f}, {manual[1]:.4f}"
            )
            self._start_manager(*manual)
            return

        self._header.set_status("正在定位…")
        self._radar.set_status("正在获取你的位置…")
        self._locate_signals = _LocateSignals()
        self._locate_signals.done.connect(self._on_located)
        self._locate_signals.failed.connect(self._on_locate_failed)
        QThreadPool.globalInstance().start(_LocateTask(manual, self._locate_signals))

    def _on_database_ready(self, ok: bool) -> None:
        """地名与机场数据就绪。"""
        self._airports_ready = bool(ok) and self._airports.is_loaded
        if self._airports_ready:
            count = len(self._airports.scheduled_airports())
            self._header.set_status(f"机场数据就绪（{count} 个在运营民航机场）")
        # 数据到了就重新加载一次覆盖层（之前可能因为没数据而漏掉城市与机场）
        self._overlay_center = None
        self._schedule_overlays()

    # ==================================================================
    # 定位回调
    # ==================================================================

    def _on_located(self, result) -> None:
        self._header.set_status(result.detail)
        self._radar.set_location(result.lat, result.lon)
        # 记下真实位置：雷达中心之后可能被"跳转到机场"移走，
        # 左下角按钮要靠它回到这里
        self._radar.set_home_location(result.lat, result.lon)

        # 记下这次坐标，作为下次定位失败时的兜底
        if result.source != "manual":
            self._config.set_last_location(result.lat, result.lon)

        self._start_manager(result.lat, result.lon)
        # 视口确定后再拉覆盖层与天气
        self._schedule_overlays()
        self._refresh_weather()
        self._weather_timer.start()

    def _on_locate_failed(self, message: str) -> None:
        self._header.set_status(f"定位失败：{message}")
        self._radar.set_status("定位失败，请在「设置 → 定位」中手动校准坐标")

    def _start_manager(self, lat: float, lon: float) -> None:
        self._manager = FlightManager(self._config)
        self._manager.flights_updated.connect(self._on_flights)
        self._manager.status_changed.connect(self._header.set_status)
        self._manager.start(lat, lon)

    def _on_basemap_ready(self, loaded: bool, text: str) -> None:
        # 底图就绪后触发一次重绘即可——静态层的缓存键包含底图状态，
        # 会自动重建带海岸线的版本
        if not loaded:
            self._header.set_status(text)
        self._radar.update()

    # ==================================================================
    # 覆盖层与天气
    # ==================================================================

    def _schedule_overlays(self) -> None:
        """视口变化后延迟加载覆盖层数据（防抖）。"""
        self._overlay_timer.start()

    def _load_overlays(self) -> None:
        """在后台加载当前视口的覆盖层数据。"""
        if self._overlay_busy:
            return

        center = (self._radar._lat, self._radar._lon)
        radius = self._radar._radius_km
        # 视口没变就不重复请求
        if (self._overlay_center is not None
                and abs(center[0] - self._overlay_center[0]) < 1e-6
                and abs(center[1] - self._overlay_center[1]) < 1e-6
                and abs(radius - self._overlay_radius) < 1e-6):
            return

        self._overlay_busy = True
        QThreadPool.globalInstance().start(_OverlayTask(
            center[0], center[1], radius,
            self._places, self._airports, self._overlay_signals,
        ))

    def _on_overlays_loaded(self, data: dict) -> None:
        """覆盖层数据到位后注入雷达视图。"""
        self._overlay_busy = False
        center = data.get("center")
        radius = data.get("radius_km")

        # 加载期间用户可能又移动了视口，丢弃过期结果
        if center and (abs(center[0] - self._radar._lat) > 1e-6
                       or abs(center[1] - self._radar._lon) > 1e-6
                       or abs(radius - self._radar._radius_km) > 1e-6):
            self._schedule_overlays()
            return

        self._overlay_center = center
        self._overlay_radius = radius or 0.0

        self._radar.set_places(data.get("places") or [])
        self._radar.set_airports(data.get("airports") or [])
        self._radar.set_terrain(data.get("terrain"))
        self._radar.set_roads(data.get("roads"))
        self._radar.set_cloud_field(data.get("cloud"))

    def _refresh_weather(self) -> None:
        """取用户真实位置的天气。"""
        home = self._radar.home_location()
        if home is None:
            return
        QThreadPool.globalInstance().start(
            _WeatherTask(home[0], home[1], self._weather_signals)
        )

    def _on_weather_loaded(self, weather) -> None:
        self._radar.set_weather(weather)

    # ==================================================================
    # 机场跳转与回位
    # ==================================================================

    def _open_airport_picker(self) -> None:
        """打开机场列表。"""
        if not self._airports_ready:
            self._header.set_status("机场数据还在加载，请稍候…")
            return

        airports = self._airports.scheduled_airports()
        if not airports:
            self._header.set_status("机场数据为空，请检查网络后重试")
            return

        # 对话框构造失败时把原因显示出来。Qt 会吞掉事件处理器里抛出的异常，
        # 界面上只会表现为"点了没反应"，非常难查——之前就漏过一次 import。
        try:
            dialog = AirportPickerDialog(airports, self._theme, parent=self)
        except Exception as exc:  # noqa: BLE001
            self._header.set_status(f"机场列表打开失败：{type(exc).__name__}: {exc}")
            return

        dialog.airport_chosen.connect(self._jump_to_airport)
        dialog.exec()

    def _jump_to_airport(self, airport: Airport) -> None:
        """把雷达圆心移到指定机场上空。"""
        self._show_list()
        self._radar.set_location(airport.lat, airport.lon)
        if self._manager is not None:
            self._manager.set_location(airport.lat, airport.lon)
        self._header.set_status(
            f"已定位到 {airport.city_zh} · {airport.name}（{airport.codes}）"
        )
        self._schedule_overlays()

    def _go_home(self) -> None:
        """回到用户当前位置。"""
        home = self._radar.home_location()
        if home is None:
            return
        self._show_list()
        self._radar.set_location(home[0], home[1])
        if self._manager is not None:
            self._manager.set_location(home[0], home[1])
        self._header.set_status("已回到当前位置")
        self._schedule_overlays()
        self._refresh_weather()

    # ==================================================================
    # 数据回调
    # ==================================================================

    def _on_flights(self, payload: dict) -> None:
        self._radar.set_flights(payload)

        flights = payload.get("flights") or []
        self._list_panel.update_flights(flights, self._config.altitude_unit)

        # 详情页跟随刷新；航班飞出范围时面板会显示"已离开雷达范围"
        if self._stack.currentWidget() is self._detail_panel and self._detail_id:
            self._detail_panel.show_flight(self._radar.flight_by_id(self._detail_id))

        notice = payload.get("notice")
        if notice:
            self._header.set_status(notice)
            return

        parts = [f"数据源 {payload.get('source') or '—'}", f"{len(flights)} 架"]
        if payload.get("stale"):
            parts.append("数据延迟，正在重试")

        # 一架都没有时补一句说明。社区 ADS-B 网络在中国大陆的接收站很少，
        # 空结果多半是覆盖问题而不是"附近真的没航班"，不说明用户会以为程序坏了。
        if not flights:
            radius = int(payload.get("radius_km") or 0)
            if radius and radius < 200:
                parts.append("可尝试滚轮放大半径")
            else:
                parts.append("该区域数据源覆盖较少")

        self._header.set_status("  ·  ".join(parts))

    # ==================================================================
    # 面板切换
    # ==================================================================

    def _on_radar_selection(self, icao24: str) -> None:
        if icao24:
            self._show_detail(icao24)
        else:
            # 点空白处取消选中，回到排行榜
            self._show_list()

    def _show_detail(self, icao24: str) -> None:
        self._detail_id = icao24
        self._detail_panel.show_flight(self._radar.flight_by_id(icao24))
        self._stack.setCurrentWidget(self._detail_panel)
        self._radar.set_selected(icao24)
        self._list_panel.set_selected(icao24)

    def _show_list(self) -> None:
        self._detail_id = ""
        self._stack.setCurrentWidget(self._list_panel)
        self._radar.set_selected("")
        self._list_panel.set_selected("")

    # ==================================================================
    # 交互
    # ==================================================================

    def _on_radius_changed(self, radius_km: float) -> None:
        """滚轮缩放：先本地立即生效（雷达马上重投影），再通知数据层按新半径拉取。"""
        self._config.update(radius_km=radius_km)
        self._radar.set_radius(radius_km)
        if self._manager is not None:
            self._manager.set_radius(radius_km)
        # 城市分级、路网层级、地形分辨率都跟着半径走
        self._schedule_overlays()

    def _refresh_now(self) -> None:
        if self._manager is not None:
            self._manager.refresh_now()

    def _open_settings(self) -> None:
        dialog = SettingsDialog(self._config, self._theme, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._apply_settings(dialog.values())

    def _apply_settings(self, values: dict) -> None:
        """把设置面板的结果写入配置并逐项生效。"""
        previous = self._config.as_dict()
        self._config.update(**values)

        # 主题
        theme = Theme(
            font_size=self._config.font_size,
            scan_color=self._config.scan_color,
            bg_level=self._config.bg_level,
        )
        self._apply_theme(theme)

        # 半径：本地立即重投影，不必等下一轮数据
        self._radar.set_radius(self._config.radius_km)

        # 底图图层开关
        self._radar.set_overlays_enabled(
            terrain=self._config.get("show_terrain", True),
            roads=self._config.get("show_roads", True),
            clouds=self._config.get("show_clouds", True),
            places=self._config.get("show_places", True),
            airports=self._config.get("show_airports", True),
            weather=self._config.get("show_weather", True),
        )

        # 单位或主题变化后，已显示的航班文本需要按新设置重绘
        self._list_panel.update_flights(self._radar.flights, self._config.altitude_unit)
        self._radar.update()
        if self._stack.currentWidget() is self._detail_panel and self._detail_id:
            self._detail_panel.show_flight(self._radar.flight_by_id(self._detail_id))

        if self._manager is None:
            return

        self._manager.set_interval(self._config.refresh_sec)

        source_changed = (
            values.get("source") != previous.get("source")
            or values.get("fr24_api_key") != previous.get("fr24_api_key")
        )
        if source_changed:
            self._manager.set_source(self._config.source, self._config.fr24_api_key)

        location = values.get("location") or {}
        if location.get("mode") == "manual":
            manual = self._config.manual_location()
            if manual is not None:
                self._radar.set_location(*manual)
                self._manager.set_location(*manual)

        self._manager.refresh_now()

    def _apply_theme(self, theme: Theme) -> None:
        """全局应用主题：样式表 + 调色板 + 各控件。"""
        self._theme = theme
        app = QApplication.instance()
        if app is not None:
            app.setPalette(theme.palette())
            app.setStyleSheet(theme.qss())
        self._radar.set_theme(theme)
        self._list_panel.set_theme(theme)
        self._detail_panel.set_theme(theme)

    # ==================================================================

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        """退出前停掉后台线程，让富化缓存有机会落盘。"""
        if self._manager is not None:
            self._manager.stop()
        super().closeEvent(event)
