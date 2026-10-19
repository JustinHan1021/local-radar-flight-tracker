"""右侧面板·距离排行榜。

展示雷达范围内按直线距离最近的若干航班。数据刷新时行组件按 ``icao24``
复用而不是整列表重建，位置变化用属性动画过渡——这样顺序变化时是"滑动"
而不是"闪一下重排"，观感平滑得多。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPropertyAnimation,
    Qt,
    Signal,
)
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.config import Config
from app.data.models import Flight
from app.theme import Theme
from app.ui.widgets import Divider, TagLabel


class FlightRow(QFrame):
    """排行榜中的一条航班。"""

    activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("FlightRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setProperty("selected", False)
        self._icao24 = ""
        self._full_model = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(3)

        # 第一行：航班号 +（右）宽体 / 特殊标签
        top = QHBoxLayout()
        top.setSpacing(6)
        self._number = QLabel("—")
        self._number.setObjectName("RowNumber")
        self._wide_tag = TagLabel("宽体")
        self._special_tag = TagLabel("特殊", kind="special")
        top.addWidget(self._number)
        top.addStretch(1)
        top.addWidget(self._wide_tag)
        top.addWidget(self._special_tag)
        layout.addLayout(top)

        # 第二行：出发地 → 目的地 +（右）预计到达
        middle = QHBoxLayout()
        middle.setSpacing(6)
        self._route = QLabel("—")
        self._route.setObjectName("MetaText")
        self._eta = QLabel("")
        self._eta.setObjectName("MonoDimText")
        middle.addWidget(self._route)
        middle.addStretch(1)
        middle.addWidget(self._eta)
        layout.addLayout(middle)

        # 第三行：机型 · 航司 +（右）直线距离
        bottom = QHBoxLayout()
        bottom.setSpacing(6)
        self._model = QLabel("—")
        self._model.setObjectName("CaptionText")
        self._distance = QLabel("—")
        self._distance.setObjectName("RowDistance")
        bottom.addWidget(self._model, 1)
        bottom.addWidget(self._distance)
        layout.addLayout(bottom)

    # ------------------------------------------------------------------

    @property
    def icao24(self) -> str:
        return self._icao24

    def bind(self, flight: Flight, unit: str) -> None:
        """把航班数据填入本行。"""
        self._icao24 = flight.icao24
        self._number.setText(flight.flight_number)

        self._wide_tag.setVisible(flight.is_widebody)

        note = flight.special_note
        self._special_tag.setVisible(bool(note))
        if note:
            self._special_tag.setToolTip(note)

        self._route.setText(flight.route_label)
        self._route.setToolTip(
            f"{flight.route.origin_name or '—'} → {flight.route.destination_name or '—'}"
            if flight.route else "暂无航线数据"
        )

        # ETA 只取时刻，详细说明留在详情页，避免榜单行过宽
        result = flight.eta()
        if result is not None:
            self._eta.setText(f"预计 {result[0]:%H:%M}")
            self._eta.setToolTip(flight.eta_text())
        elif flight.on_ground:
            self._eta.setText("已落地")
        else:
            self._eta.setText("")

        model = flight.model_name
        airline = flight.airline_name
        if model != "—" and airline:
            self._full_model = f"{model} · {airline}"
        else:
            self._full_model = model if model != "—" else (airline or "机型未知")
        self._model.setToolTip(
            f"{flight.info.type_name or '—'} / {flight.info.manufacturer or '—'} / "
            f"{airline or '—'}"
        )

        self._distance.setText(flight.display_distance())
        self._distance.setToolTip(f"方位 {flight.display_bearing()}")
        self._apply_elide()

    def set_selected(self, selected: bool) -> None:
        if bool(self.property("selected")) == selected:
            return
        self.setProperty("selected", selected)
        # 动态属性变化后必须重新应用样式表才会生效
        self.style().unpolish(self)
        self.style().polish(self)

    def _apply_elide(self) -> None:
        """按当前宽度截断机型·航司文本，避免撑破行宽。"""
        if not self._full_model:
            self._model.setText("—")
            return
        reserved = 20 + self._distance.sizeHint().width() + 8
        available = self.width() - reserved
        if available <= 24:
            self._model.setText(self._full_model)
            return
        self._model.setText(
            QFontMetrics(self._model.font()).elidedText(
                self._full_model, Qt.TextElideMode.ElideRight, available
            )
        )

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        super().resizeEvent(event)
        self._apply_elide()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        if event.button() == Qt.MouseButton.LeftButton and self._icao24:
            self.activated.emit(self._icao24)


class FlightListPanel(QWidget):
    """距离排行榜面板。"""

    flight_activated = Signal(str)

    #: 榜单最多显示的行数
    MAX_ROWS = 10
    #: 行间距（像素）
    ROW_GAP = 6
    #: 重排动画时长（毫秒）
    ANIM_MS = 260

    def __init__(self, config: Config, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Panel")
        # 自定义 QWidget 子类必须显式开启样式表背景绘制，
        # 否则 QSS 的 background 不生效，面板会露出系统默认浅色底
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._config = config
        self._theme = theme
        self._rows: dict[str, FlightRow] = {}
        self._order: list[str] = []
        self._animations: list[QPropertyAnimation] = []
        self._selected = ""
        self._unit = config.altitude_unit
        self._count = 0

        title = QLabel("周边航班")
        title.setObjectName("SectionTitle")
        self._subtitle = QLabel("按直线距离排序")
        self._subtitle.setObjectName("CaptionText")

        header = QWidget()
        header_layout = QVBoxLayout(header)
        header_layout.setContentsMargins(14, 14, 14, 10)
        header_layout.setSpacing(3)
        header_layout.addWidget(title)
        header_layout.addWidget(self._subtitle)

        # 排行榜行用绝对定位排布，才能对位置变化做动画
        self._viewport = QWidget()
        self._viewport.setObjectName("ListContainer")

        self._scroll = QScrollArea()
        self._scroll.setWidget(self._viewport)
        self._scroll.setWidgetResizable(False)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._scroll.viewport().installEventFilter(self)

        self._empty = QLabel("雷达范围内暂无航班")
        self._empty.setObjectName("CaptionText")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(header)
        layout.addWidget(Divider())
        layout.addWidget(self._scroll, 1)
        layout.addWidget(self._empty, 1)
        self._empty.setVisible(False)

    # ------------------------------------------------------------------
    # 数据更新
    # ------------------------------------------------------------------

    def update_flights(self, flights: list[Flight], unit: str) -> None:
        """刷新榜单。

        行组件按 ``icao24`` 复用：上榜的更新内容，掉榜的销毁，
        新上榜的从上方滑入，其余按新顺序做位移动画。
        """
        self._unit = unit
        self._count = len(flights)
        top = flights[: self.MAX_ROWS]
        wanted = [flight.icao24 for flight in top]

        for icao24 in list(self._rows.keys()):
            if icao24 not in wanted:
                row = self._rows.pop(icao24)
                row.setParent(None)
                row.deleteLater()

        for flight in top:
            row = self._rows.get(flight.icao24)
            if row is None:
                row = FlightRow(self._viewport)
                row.activated.connect(self.flight_activated)
                self._rows[flight.icao24] = row
                row.bind(flight, unit)
                row.setFixedWidth(max(1, self._scroll.viewport().width()))
                # 从上方偏移处滑入，形成"新条目进入"的动势
                row.move(0, -(row.sizeHint().height() + self.ROW_GAP))
                row.show()
            else:
                row.bind(flight, unit)
            row.set_selected(flight.icao24 == self._selected)

        self._order = wanted
        has_rows = bool(wanted)
        self._scroll.setVisible(has_rows)
        self._empty.setVisible(not has_rows)

        total = self._count
        suffix = f"最近 {len(wanted)} 架" if total > len(wanted) else f"共 {total} 架"
        self._subtitle.setText(f"按直线距离排序 · {suffix}")

        self._relayout(animate=True)

    def set_selected(self, icao24: str) -> None:
        self._selected = (icao24 or "").lower()
        for key, row in self._rows.items():
            row.set_selected(key == self._selected)

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._relayout(animate=False)

    def set_altitude_unit(self, unit: str) -> None:
        self._unit = unit

    # ------------------------------------------------------------------
    # 排布与动画
    # ------------------------------------------------------------------

    def _relayout(self, animate: bool) -> None:
        """按当前顺序重排所有行。"""
        width = max(1, self._scroll.viewport().width())
        # setWidgetResizable(False) 下 viewport 不会自动跟随滚动区尺寸，
        # 必须手动设定宽高，否则行会因为父容器只有 1x1 而完全不可见。
        self._viewport.setFixedWidth(width)

        y = 0
        for icao24 in self._order:
            row = self._rows.get(icao24)
            if row is None:
                continue
            row.setFixedWidth(width)
            height = row.sizeHint().height()
            target = QPoint(0, y)
            if animate and row.pos() != target:
                self._animate(row, target)
            else:
                row.move(target)
            y += height + self.ROW_GAP

        self._viewport.setFixedHeight(max(y, 1))

    def _animate(self, row: FlightRow, target: QPoint) -> None:
        animation = QPropertyAnimation(row, b"pos", self)
        animation.setDuration(self.ANIM_MS)
        animation.setStartValue(row.pos())
        animation.setEndValue(target)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        # 必须持有引用，否则动画对象会被 Python 回收导致中途停止
        self._animations.append(animation)
        animation.finished.connect(lambda a=animation: self._forget(a))
        animation.start()

    def _forget(self, animation: QPropertyAnimation) -> None:
        if animation in self._animations:
            self._animations.remove(animation)

    def eventFilter(self, obj, event):  # noqa: N802 - Qt 约定
        if obj is self._scroll.viewport() and event.type() == QEvent.Type.Resize:
            self._relayout(animate=False)
        return super().eventFilter(obj, event)
