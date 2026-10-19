"""设置面板。

按「雷达与数据 / 显示 / 定位」分组呈现全部可调项。对话框自身**不改配置**，
确认后返回一份完整参数字典由主窗口统一应用——这样点"取消"不会有任何副作用。
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from app.config import (
    SOURCE_LABELS,
    VALID_SOURCES,
    Config,
)
from app.theme import Theme

#: 背景深浅档位 → 显示名
_BG_LEVEL_LABELS = ((0, "深（默认）"), (1, "中"), (2, "浅"))
#: 字号档位 → 显示名
_FONT_LABELS = (("small", "小"), ("medium", "中（默认）"), ("large", "大"))
#: 海拔单位
_UNIT_LABELS = (("m", "米 (m)"), ("ft", "英尺 (ft)"))


class SettingsDialog(QDialog):
    """全部可调参数的编辑对话框。"""

    def __init__(self, config: Config, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.setObjectName("SettingsDialog")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(520)

        self._config = config

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 14)
        root.setSpacing(12)

        root.addWidget(self._build_radar_group())
        root.addWidget(self._build_display_group())
        root.addWidget(self._build_location_group())

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).setText("恢复默认")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self._restore_defaults
        )
        root.addWidget(buttons)

        self._load_from_config()

    # ==================================================================
    # 分组构建
    # ==================================================================

    def _build_radar_group(self) -> QGroupBox:
        group = QGroupBox("雷达与数据")
        form = QFormLayout(group)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._radius = QDoubleSpinBox()
        self._radius.setRange(1.0, 460.0)
        self._radius.setDecimals(1)
        self._radius.setSingleStep(5.0)
        self._radius.setSuffix(" km")
        # 免费数据源单次查询上限 250 海里 ≈ 463 km，故上限设 460
        self._radius.setToolTip("雷达半径。免费数据源单次查询上限约 460 km")
        form.addRow("雷达半径", self._radius)

        self._interval = QSpinBox()
        self._interval.setRange(3, 300)
        self._interval.setSuffix(" 秒")
        self._interval.setToolTip("数据刷新间隔。adsb.fi 实测可稳定支撑 10 秒刷新")
        form.addRow("刷新间隔", self._interval)

        self._max_flights = QSpinBox()
        self._max_flights.setRange(5, 200)
        self._max_flights.setSuffix(" 架")
        self._max_flights.setToolTip("排行榜最多显示的航班数量")
        form.addRow("榜单条数", self._max_flights)

        self._show_ground = QCheckBox("在雷达上显示地面航班（停机坪 / 滑行）")
        self._show_ground.setToolTip(
            "默认关闭：机场停机坪上的航班数量极多，会让雷达盘难以辨认"
        )
        form.addRow("", self._show_ground)

        self._source = QComboBox()
        for key in VALID_SOURCES:
            self._source.addItem(SOURCE_LABELS.get(key, key), key)
        self._source.currentIndexChanged.connect(self._on_source_changed)
        form.addRow("数据源", self._source)

        self._fr24_key = QLineEdit()
        self._fr24_key.setPlaceholderText("Flightradar24 企业订阅 API Key（可选）")
        self._fr24_key.setEchoMode(QLineEdit.EchoMode.Password)
        self._fr24_hint = QLabel(
            "Flightradar24 需企业订阅，当前版本仅保留配置项，未接入其接口。"
        )
        self._fr24_hint.setObjectName("CaptionText")
        self._fr24_hint.setWordWrap(True)
        form.addRow("FR24 Key", self._fr24_key)
        form.addRow("", self._fr24_hint)

        return group

    def _build_display_group(self) -> QGroupBox:
        group = QGroupBox("显示")
        form = QFormLayout(group)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._unit = QComboBox()
        for key, label in _UNIT_LABELS:
            self._unit.addItem(label, key)
        form.addRow("海拔单位", self._unit)

        self._font_size = QComboBox()
        for key, label in _FONT_LABELS:
            self._font_size.addItem(label, key)
        form.addRow("字号", self._font_size)

        self._bg_level = QComboBox()
        for key, label in _BG_LEVEL_LABELS:
            self._bg_level.addItem(label, key)
        form.addRow("背景深浅", self._bg_level)

        color_row = QWidget()
        color_layout = QHBoxLayout(color_row)
        color_layout.setContentsMargins(0, 0, 0, 0)
        color_layout.setSpacing(8)
        self._color_button = QPushButton()
        self._color_button.setFixedSize(64, 26)
        self._color_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._color_button.clicked.connect(self._pick_color)
        self._color_value = QLabel()
        self._color_value.setObjectName("MonoDimText")
        color_layout.addWidget(self._color_button)
        color_layout.addWidget(self._color_value)
        color_layout.addStretch(1)
        form.addRow("扫描线颜色", color_row)

        # ---- 底图覆盖层开关 ----
        overlay_title = QLabel("底图图层")
        overlay_title.setObjectName("CaptionText")
        form.addRow("", overlay_title)

        self._overlays: dict[str, QCheckBox] = {}
        overlay_items = (
            ("show_terrain", "海拔高程着色（低绿高红）"),
            ("show_roads", "主要路网（放大到 100 km 内显示）"),
            ("show_clouds", "云图（半透明白色）"),
            ("show_places", "城市名（按半径自动分级）"),
            ("show_airports", "机场跑道"),
            ("show_weather", "天气（温度 / 风速 / 云量）"),
        )
        for key, label in overlay_items:
            box = QCheckBox(label)
            self._overlays[key] = box
            form.addRow("", box)

        return group

    def _build_location_group(self) -> QGroupBox:
        group = QGroupBox("定位")
        form = QFormLayout(group)
        form.setContentsMargins(12, 14, 12, 12)
        form.setSpacing(8)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

        self._manual = QCheckBox("使用手动校准坐标（忽略自动定位）")
        form.addRow("", self._manual)

        self._lat = QDoubleSpinBox()
        self._lat.setRange(-90.0, 90.0)
        self._lat.setDecimals(6)
        self._lat.setSingleStep(0.01)
        self._lat.setSuffix(" °")
        form.addRow("纬度", self._lat)

        self._lon = QDoubleSpinBox()
        self._lon.setRange(-180.0, 180.0)
        self._lon.setDecimals(6)
        self._lon.setSingleStep(0.01)
        self._lon.setSuffix(" °")
        form.addRow("经度", self._lon)

        hint = QLabel(
            "自动定位顺序：Windows 系统定位（精度约百米级）→ IP 定位（城市级）。"
            "手动坐标仅在上方勾选时生效。"
        )
        hint.setObjectName("CaptionText")
        hint.setWordWrap(True)
        form.addRow("", hint)

        return group

    # ==================================================================
    # 数据装载与回收
    # ==================================================================

    def _load_from_config(self) -> None:
        cfg = self._config

        self._radius.setValue(cfg.radius_km)
        self._interval.setValue(cfg.refresh_sec)
        self._max_flights.setValue(cfg.max_flights)
        self._show_ground.setChecked(cfg.show_ground)

        index = self._source.findData(cfg.source)
        self._source.setCurrentIndex(index if index >= 0 else 0)
        self._fr24_key.setText(cfg.fr24_api_key)

        self._set_combo(self._unit, cfg.altitude_unit)
        self._set_combo(self._font_size, cfg.font_size)
        self._set_combo(self._bg_level, cfg.bg_level)

        self._scan_color = cfg.scan_color
        self._update_color_widgets()

        for key, box in self._overlays.items():
            box.setChecked(bool(cfg.get(key, True)))

        manual = cfg.manual_location()
        self._manual.setChecked(cfg.location_mode == "manual")
        if manual is not None:
            self._lat.setValue(manual[0])
            self._lon.setValue(manual[1])

        self._on_source_changed()

    @staticmethod
    def _set_combo(combo: QComboBox, value) -> None:
        index = combo.findData(value)
        if index >= 0:
            combo.setCurrentIndex(index)

    def _on_source_changed(self) -> None:
        """选用 fr24 时才启用 Key 输入框，其余情况保持只读观感。"""
        is_fr24 = self._source.currentData() == "fr24"
        self._fr24_key.setEnabled(is_fr24)
        self._fr24_hint.setVisible(is_fr24)

    def _pick_color(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._scan_color), self, "选择扫描线颜色")
        if chosen.isValid():
            self._scan_color = chosen.name().upper()
            self._update_color_widgets()

    def _update_color_widgets(self) -> None:
        color = QColor(self._scan_color)
        text_color = "#0B0E0F" if color.lightness() > 140 else "#FFFFFF"
        self._color_button.setStyleSheet(
            f"background: {self._scan_color}; border: 1px solid #242E31; border-radius: 4px;"
            f" color: {text_color};"
        )
        self._color_button.setText(self._scan_color)
        self._color_value.setText(self._scan_color)

    def _restore_defaults(self) -> None:
        """把控件恢复成出厂默认值（此时尚未写入配置，仍需点"保存"）。"""
        from app.config import DEFAULTS

        self._radius.setValue(float(DEFAULTS["radius_km"]))
        self._interval.setValue(int(DEFAULTS["refresh_sec"]))
        self._max_flights.setValue(int(DEFAULTS["max_flights"]))
        self._show_ground.setChecked(bool(DEFAULTS["show_ground"]))
        index = self._source.findData(DEFAULTS["source"])
        self._source.setCurrentIndex(index if index >= 0 else 0)
        self._fr24_key.clear()
        self._set_combo(self._unit, DEFAULTS["altitude_unit"])
        self._set_combo(self._font_size, DEFAULTS["font_size"])
        self._set_combo(self._bg_level, DEFAULTS["bg_level"])
        self._scan_color = str(DEFAULTS["scan_color"])
        self._update_color_widgets()
        for key, box in self._overlays.items():
            box.setChecked(bool(DEFAULTS.get(key, True)))
        self._manual.setChecked(False)

    def values(self) -> dict:
        """返回一份完整的参数字典，供主窗口写入配置。"""
        result = {
            "radius_km": round(self._radius.value(), 2),
            "refresh_sec": self._interval.value(),
            "max_flights": self._max_flights.value(),
            "show_ground": self._show_ground.isChecked(),
            "source": self._source.currentData(),
            "fr24_api_key": self._fr24_key.text().strip(),
            "altitude_unit": self._unit.currentData(),
            "font_size": self._font_size.currentData(),
            "bg_level": self._bg_level.currentData(),
            "scan_color": self._scan_color,
        }
        for key, box in self._overlays.items():
            result[key] = box.isChecked()
        if self._manual.isChecked():
            result["location"] = {
                "mode": "manual",
                "lat": round(self._lat.value(), 6),
                "lon": round(self._lon.value(), 6),
            }
        else:
            # 保留已有坐标，只把模式切回自动
            existing = self._config.manual_location()
            result["location"] = {
                "mode": "auto",
                "lat": existing[0] if existing else None,
                "lon": existing[1] if existing else None,
            }
        return result
