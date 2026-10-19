"""右侧面板·航班详情。

顶部是飞机注册实拍图，下方按「身份 / 机型 / 航线 / 飞行状态 / 相对位置」
分组列出全部字段，生产商名可点击跳转厂商官网。

实拍图通过后台线程下载并本地缓存：图片来自第三方站点，首屏同步下载会明显
卡顿，而且相当一部分飞机本来就没有照片（实测覆盖率约 40%），必须容错。

关于"计划起降时间"：免费数据源不提供航班时刻表，这里如实标注为**估算**，
用剩余大圆距离除以当前地速推算，并在面板底部写明数据来源限制，
而不是编造一个看起来像真的时刻。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QPixmap
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.data.enrich import download_photo
from app.data.models import Flight
from app.data.registry import aircraft_kind
from app.theme import Theme
from app.ui.widgets import Divider, LinkLabel, TagLabel, make_field_row


class _PhotoSignals(QObject):
    """图片下载完成信号。

    ``QRunnable`` 不是 ``QObject``、不能自带信号，需要一个 QObject 承载。
    """

    done = Signal(str, object)   # (图片 URL, 本地路径 或 None)


class _PhotoTask(QRunnable):
    """后台下载飞机实拍图。"""

    def __init__(self, url: str, signals: _PhotoSignals) -> None:
        super().__init__()
        self._url = url
        self._signals = signals

    def run(self) -> None:  # noqa: D102 - QRunnable 约定
        path = None
        try:
            path = download_photo(self._url)
        except Exception:  # noqa: BLE001 - 下载失败不该影响界面
            path = None
        self._signals.done.emit(self._url, path)


class FlightDetailPanel(QWidget):
    """航班详情面板。"""

    #: 用户点击左上角返回按钮
    back_requested = Signal()

    def __init__(self, config: Config, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        # 同 FlightListPanel：不开这个属性，QSS 背景不会绘制
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._config = config
        self._theme = theme
        self._flight: Flight | None = None
        self._photo_url = ""
        # 显式持有原图，用于面板宽度变化时重新缩放；
        # 不依赖 QLabel.pixmap()——它在 PySide6 中可能返回 None
        self._photo_pixmap: QPixmap | None = None

        self._photo_signals = _PhotoSignals()
        self._photo_signals.done.connect(self._on_photo_loaded)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ---------------- 顶部返回栏 ----------------
        top = QWidget()
        top_layout = QHBoxLayout(top)
        top_layout.setContentsMargins(10, 10, 14, 10)
        top_layout.setSpacing(6)

        self._back = QPushButton("← 返回")
        self._back.setObjectName("GhostButton")
        self._back.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back.clicked.connect(self.back_requested)

        self._title = QLabel("航班详情")
        self._title.setObjectName("SectionTitle")

        top_layout.addWidget(self._back)
        top_layout.addWidget(self._title)
        top_layout.addStretch(1)
        root.addWidget(top)
        root.addWidget(Divider())

        # ---------------- 可滚动主体 ----------------
        body = QWidget()
        self._body_layout = QVBoxLayout(body)
        self._body_layout.setContentsMargins(14, 12, 14, 16)
        self._body_layout.setSpacing(7)

        self._photo = QLabel("暂无实拍图")
        self._photo.setObjectName("PhotoFrame")
        self._photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._photo.setFixedHeight(150)
        self._photo.setMinimumWidth(80)
        self._body_layout.addWidget(self._photo)

        tags = QHBoxLayout()
        tags.setSpacing(6)
        self._wide_tag = TagLabel("宽体机")
        self._special_tag = TagLabel("特殊", kind="special")
        tags.addWidget(self._wide_tag)
        tags.addWidget(self._special_tag)
        tags.addStretch(1)
        self._body_layout.addLayout(tags)

        # ---- 身份 ----
        self._body_layout.addWidget(_section("机身与归属"))
        self._f_number = self._add_field("航班号")
        self._f_callsign = self._add_field("ICAO 呼号")
        self._f_registration = self._add_field("注册号")
        self._f_airline = self._add_field("所属航司")
        self._f_operator = self._add_field("运营人")

        # ---- 机型 ----
        self._body_layout.addWidget(Divider())
        self._body_layout.addWidget(_section("机型"))
        self._f_model = self._add_field("机型全称")
        self._f_type_code = self._add_field("ICAO 机型")
        self._f_kind = self._add_field("机型类别", mono=False)
        self._f_manufacturer = self._add_link_field("生产商")

        # ---- 航线 ----
        self._body_layout.addWidget(Divider())
        self._body_layout.addWidget(_section("航线"))
        self._f_origin = self._add_field("出发地", mono=False)
        self._f_destination = self._add_field("目的地", mono=False)
        self._f_eta = self._add_field("预计到达")
        self._f_remaining = self._add_field("剩余距离")

        # ---- 飞行状态 ----
        self._body_layout.addWidget(Divider())
        self._body_layout.addWidget(_section("飞行状态"))
        self._f_altitude = self._add_field("当前海拔")
        self._f_geometric = self._add_field("几何高度")
        self._f_speed = self._add_field("地速")
        self._f_track = self._add_field("航向")
        self._f_vertical = self._add_field("垂直速度")
        self._f_phase = self._add_field("飞行阶段")

        # ---- 相对位置 ----
        self._body_layout.addWidget(Divider())
        self._body_layout.addWidget(_section("与我的相对位置"))
        self._f_distance = self._add_field("直线距离")
        self._f_bearing = self._add_field("相对方位")

        # ---- 备注 ----
        self._body_layout.addWidget(Divider())
        self._body_layout.addWidget(_section("备注"))
        self._f_special = self._add_field("特殊涂装", mono=False)

        self._footnote = QLabel(
            "说明：免费数据源不提供航班时刻表，「预计到达」为按当前地速与"
            "剩余大圆距离推算的估算值；「计划 / 实际起降时间」无数据来源，故不列出。"
        )
        self._footnote.setObjectName("CaptionText")
        self._footnote.setWordWrap(True)
        self._body_layout.addSpacing(6)
        self._body_layout.addWidget(self._footnote)
        self._body_layout.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(body)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(scroll, 1)

    # ==================================================================
    # 字段构造
    # ==================================================================

    def _add_field(self, label_text: str, mono: bool = True) -> QLabel:
        row, value = make_field_row(label_text, mono)
        self._body_layout.addWidget(row)
        return value

    def _add_link_field(self, label_text: str) -> LinkLabel:
        """生产商字段用可点击链接，无官网时自动退化为普通文本。"""
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        label = QLabel(label_text)
        label.setObjectName("FieldLabel")
        label.setFixedWidth(72)

        value = LinkLabel()
        value.activated.connect(self._open_url)

        layout.addWidget(label)
        layout.addWidget(value, 1)
        self._body_layout.addWidget(row)
        return value

    # ==================================================================
    # 数据填充
    # ==================================================================

    def show_flight(self, flight: Flight | None) -> None:
        """展示指定航班；传 ``None`` 表示该航班已离开雷达范围。"""
        self._flight = flight

        if flight is None:
            self._title.setText("航班详情")
            self._photo.setPixmap(QPixmap())
            self._photo.setText("该航班已离开雷达范围")
            for label in self._all_value_labels():
                label.setText("—")
            self._wide_tag.setVisible(False)
            self._special_tag.setVisible(False)
            self._photo_url = ""
            self._photo_pixmap = None
            return

        unit = self._config.altitude_unit
        self._title.setText(flight.flight_number)

        self._wide_tag.setVisible(flight.is_widebody)

        note = flight.special_note
        self._special_tag.setVisible(bool(note))
        if note:
            self._special_tag.setText("特殊涂装")
            self._special_tag.setToolTip(note)

        # ---- 身份 ----
        self._f_number.setText(flight.flight_number)
        self._f_callsign.setText(flight.icao_callsign or "—")
        self._f_registration.setText(flight.info.registration or "—")
        self._f_airline.setText(flight.airline_name or "—")
        self._f_operator.setText(flight.info.operator or "—")

        # ---- 机型 ----
        self._f_model.setText(flight.info.type_name or "—")
        self._f_type_code.setText(flight.info.type_code or "—")

        kind = aircraft_kind(flight.info.type_code, flight.category)
        self._f_kind.setText(
            f"{kind.label} · {kind.engines} 台发动机" if kind.engines else kind.label
        )
        self._f_kind.setToolTip("决定雷达图上该机的图标形状")

        self._f_manufacturer.set_link(
            flight.info.manufacturer or "—",
            flight.manufacturer_url,
        )
        if flight.info.manufacturer and not flight.manufacturer_url:
            self._f_manufacturer.setToolTip("未收录该生产商的官网地址")

        # ---- 航线 ----
        route = flight.route
        if route is not None and route.available:
            self._f_origin.setText(_airport_text(route.origin_iata, route.origin_name))
            self._f_destination.setText(
                _airport_text(route.destination_iata, route.destination_name)
            )
        else:
            self._f_origin.setText("—")
            self._f_destination.setText("—")
            self._f_origin.setToolTip("该航班暂无航线数据")
            self._f_destination.setToolTip("该航班暂无航线数据")

        eta = flight.eta()
        if eta is not None:
            arrival, remaining = eta
            offset_text = flight.destination_local_offset_text()
            self._f_eta.setText(f"{arrival:%H:%M}（当地时间 {offset_text}）· 估算")
            self._f_remaining.setText(f"{remaining:,.0f} km")
        else:
            self._f_eta.setText(flight.eta_text())
            self._f_remaining.setText("—")

        # ---- 飞行状态 ----
        self._f_altitude.setText(flight.display_altitude(unit))
        if flight.geo_altitude_ft is None:
            self._f_geometric.setText("—")
        elif unit == "ft":
            self._f_geometric.setText(f"{flight.geo_altitude_ft:,.0f} ft")
        else:
            self._f_geometric.setText(f"{flight.geo_altitude_ft * 0.3048:,.0f} m")
        self._f_speed.setText(flight.display_speed())
        self._f_track.setText(flight.display_track() if flight.track is not None else "—")
        self._f_vertical.setText(flight.display_vertical_rate())
        self._f_phase.setText(_flight_phase(flight))

        # ---- 相对位置 ----
        self._f_distance.setText(flight.display_distance())
        self._f_bearing.setText(flight.display_bearing())

        # ---- 备注 ----
        if note:
            self._f_special.setText(note)
        elif flight.info.registration:
            self._f_special.setText("未标注")
            self._f_special.setToolTip(
                "可在 assets/special_liveries.json 中按「注册号: 涂装说明」格式自行补充"
            )
        else:
            self._f_special.setText("—")

        self._load_photo(flight.info.photo_url)

    def _all_value_labels(self) -> list[QLabel]:
        return [
            self._f_number, self._f_callsign, self._f_registration, self._f_airline,
            self._f_operator, self._f_model, self._f_type_code, self._f_kind,
            self._f_manufacturer,
            self._f_origin, self._f_destination, self._f_eta, self._f_remaining,
            self._f_altitude, self._f_geometric, self._f_speed, self._f_track,
            self._f_vertical, self._f_phase, self._f_distance, self._f_bearing,
            self._f_special,
        ]

    # ==================================================================
    # 实拍图
    # ==================================================================

    def _load_photo(self, url: str) -> None:
        """异步加载实拍图。"""
        # 同一张图已经加载过就直接复用：详情页每次数据刷新都会重填字段，
        # 不加这个判断会每 10 秒重新触发一次加载，图片会闪烁
        if url and url == self._photo_url and self._photo_pixmap is not None:
            return

        self._photo_url = url or ""
        self._photo_pixmap = None
        if not url:
            self._photo.setPixmap(QPixmap())
            model = self._flight.model_name if self._flight else ""
            self._photo.setText(f"暂无实拍图\n{model}" if model and model != "—" else "暂无实拍图")
            return

        self._photo.setPixmap(QPixmap())
        self._photo.setText("图片加载中…")
        QThreadPool.globalInstance().start(_PhotoTask(url, self._photo_signals))

    def _on_photo_loaded(self, url: str, path) -> None:
        # 用户在下载期间切换了航班，丢弃过期结果
        if url != self._photo_url:
            return

        if path is None:
            self._photo_pixmap = None
            self._photo.setPixmap(QPixmap())
            model = self._flight.model_name if self._flight else ""
            self._photo.setText(f"图片加载失败\n{model}" if model and model != "—"
                                else "图片加载失败")
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._photo_pixmap = None
            self._photo.setPixmap(QPixmap())
            self._photo.setText("图片无法解析")
            return

        self._photo_pixmap = pixmap
        self._rescale_photo()

    def _rescale_photo(self) -> None:
        """按当前控件尺寸等比缩放已加载的图片。"""
        if self._photo_pixmap is None or self._photo_pixmap.isNull():
            return
        scaled = self._photo_pixmap.scaled(
            max(1, self._photo.width() - 2),
            max(1, self._photo.height() - 2),
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        self._photo.setPixmap(scaled)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        super().resizeEvent(event)
        self._rescale_photo()

    # ==================================================================

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme

    @staticmethod
    def _open_url(url: str) -> None:
        QDesktopServices.openUrl(QUrl(url))


def _section(text: str) -> QLabel:
    """分组小标题。"""
    label = QLabel(text)
    label.setObjectName("CaptionText")
    return label


def _airport_text(code: str, name: str) -> str:
    """把机场拼成 ``HKG 香港国际机场`` 形式；缺一项时只显示有数据的那部分。"""
    if code and name:
        return f"{code}  {name}"
    return code or name or "—"


def _flight_phase(flight: Flight) -> str:
    """由地速与垂直速率推断飞行阶段。

    免费源没有"阶段"字段，但用地速和爬升率推出来的结果足够可靠：
    地面 / 起飞爬升 / 巡航 / 下降进近。
    """
    if flight.on_ground:
        return "地面（停机或滑行）"

    rate = flight.vertical_rate_fpm or 0.0
    speed = flight.ground_speed_kt or 0.0

    if rate > 300:
        return "爬升中"
    if rate < -300:
        return "下降中"
    if speed < 120:
        return "低速（可能进近或等待）"
    return "巡航"
