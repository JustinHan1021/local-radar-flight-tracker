"""机场快速跳转：按城市名排序的全国民航机场列表。

这是需求里的"点开就是全国所有正在启用的民航机场（城市-机场-代码）"。
数据来自 :class:`app.airports.AirportDatabase`，已按城市名（等价于拼音首字母）
排好序——实测 241 个机场（含港澳台），军用机场被 ``scheduled_service`` 自然排除。

列表支持搜索：输入城市名、机场名、中文名或 IATA / ICAO 代码都能过滤。
选中后把雷达圆心移到该机场上空。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.airports import Airport
from app.theme import Theme


class AirportPickerDialog(QDialog):
    """机场选择对话框。"""

    #: 用户选定了某个机场
    airport_chosen = Signal(object)

    def __init__(
        self,
        airports: list[Airport],
        theme: Theme,
        current_ident: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("跳转到机场")
        self.setObjectName("SettingsDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.resize(520, 640)

        self._airports = airports
        self._theme = theme

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 12)
        layout.setSpacing(9)

        title = QLabel(f"全国在运营民航机场 · 共 {len(airports)} 个")
        title.setObjectName("SectionTitle")

        hint = QLabel("可输入城市、机场名或 IATA / ICAO 代码过滤，双击即可跳转")
        hint.setObjectName("CaptionText")

        self._search = QLineEdit()
        self._search.setPlaceholderText("搜索城市 / 机场 / 代码…")
        self._search.textChanged.connect(self._apply_filter)

        self._list = QListWidget()
        self._list.setObjectName("AirportList")
        self._list.setAlternatingRowColors(False)
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list.itemActivated.connect(self._on_activated)
        self._list.itemDoubleClicked.connect(self._on_activated)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消")
        cancel.setObjectName("GhostButton")
        cancel.clicked.connect(self.reject)
        confirm = QPushButton("跳转")
        confirm.clicked.connect(self._on_confirm)
        buttons.addWidget(cancel)
        buttons.addWidget(confirm)

        layout.addWidget(title)
        layout.addWidget(hint)
        layout.addWidget(self._search)
        layout.addWidget(self._list, 1)
        layout.addLayout(buttons)

        self._populate(current_ident)
        self._search.setFocus()

    # ------------------------------------------------------------------

    def _populate(self, current_ident: str) -> None:
        """把所有机场填进列表，并高亮当前所在的机场。"""
        self._list.clear()
        for airport in self._airports:
            item = QListWidgetItem(airport.label)
            # 搜索用的关键词：中文城市、英文城市、机场名、两种代码
            item.setData(Qt.ItemDataRole.UserRole, airport)
            item.setData(Qt.ItemDataRole.UserRole + 1, " ".join(filter(None, (
                airport.city_zh,
                airport.city,
                airport.name,
                airport.iata,
                airport.ident,
            ))).lower())
            item.setToolTip(f"{airport.name}\n{airport.codes}\n"
                            f"{airport.lat:.4f}, {airport.lon:.4f}")
            self._list.addItem(item)

            if current_ident and airport.ident == current_ident:
                self._list.setCurrentItem(item)

    def _apply_filter(self, text: str) -> None:
        keyword = text.strip().lower()
        visible = 0
        for row in range(self._list.count()):
            item = self._list.item(row)
            haystack = item.data(Qt.ItemDataRole.UserRole + 1) or ""
            matched = (not keyword) or (keyword in haystack)
            item.setHidden(not matched)
            if matched:
                visible += 1

        # 过滤后只剩一条时自动选中，回车即可跳转
        if visible == 1:
            for row in range(self._list.count()):
                item = self._list.item(row)
                if not item.isHidden():
                    self._list.setCurrentItem(item)
                    break

    def _current_airport(self) -> Airport | None:
        item = self._list.currentItem()
        if item is None or item.isHidden():
            return None
        return item.data(Qt.ItemDataRole.UserRole)

    def _on_activated(self, _item: QListWidgetItem) -> None:
        self._on_confirm()

    def _on_confirm(self) -> None:
        airport = self._current_airport()
        if airport is None:
            return
        self.airport_chosen.emit(airport)
        self.accept()

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        """回车确认、Esc 取消；其余按键交给搜索框。"""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._on_confirm()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)
