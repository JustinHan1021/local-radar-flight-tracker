"""军用雷达暗色主题：配色令牌、字号档位与全局样式。

设计目标：低对比度、长时间盯屏不刺眼、控件精致不廉价。

配色策略是"深炭黑打底 + 亮绿唯一强调 + 琥珀黄第二强调"：
整体只用三个色系，避免出现廉价感常见的多彩拼贴；
所有控件共享同一套描边与圆角，保证界面像同一个系统里长出来的。

样式表用 :class:`string.Template` 而非 f-string 组装，
这样 CSS 的花括号无需转义，样式本体保持可读、可整段复制调试。
"""

from __future__ import annotations

from dataclasses import dataclass
from string import Template

from PySide6.QtGui import QColor, QFont, QPalette

# ---------- 字体族 ----------
# 数字与标号用等宽字体：雷达角标秒针跳动时不会左右抖动
MONO_FONT = "Consolas"
UI_FONT = "Microsoft YaHei UI"

# 三档字号：(次要文字, 主要文字)，单位为像素
FONT_SCALES: dict[str, tuple[int, int]] = {
    "small": (11, 12),
    "medium": (13, 14),
    "large": (15, 17),
}

# ---------- 基础色（bg_level = 0 时的取值）----------
_WINDOW_BG = "#0B0E0F"      # 窗口底色，深炭黑
_PANEL_BG = "#12171A"       # 右侧信息面板
_CARD_BG = "#151B1E"        # 卡片、下拉列表
_DIVIDER = "#1F2729"        # 细分隔线
_BORDER = "#242E31"         # 控件描边
_BUTTON_BG = "#1A2225"      # 按钮底
_RADAR_DISH = "#0D1512"     # 雷达盘底，带一丝绿意

# ---------- 地理要素色 ----------
LAND = "#16241C"            # 陆地填充：深墨绿
SEA = "#0F1B2A"             # 海洋填充：藏蓝
COASTLINE = "#6C7B7F"       # 海岸线：浅灰白细线

# ---------- 文字层级 ----------
_TEXT_PRIMARY = "#B9C7C2"
_TEXT_SECONDARY = "#6E7F7A"
_TEXT_DIM = "#4A5D57"

# ---------- 航班与状态色 ----------
AIRCRAFT = "#C8D6D0"        # 常规航班：灰白
AIRCRAFT_WIDEBODY = "#E8C547"  # 宽体机：琥珀黄
HIGHLIGHT = "#5AFFC2"       # 选中 / 悬停高亮

# 背景深浅档位对应的提亮百分比（100 = 不提亮）。
# 早先取的 118 / 136 太保守——档位 0 与 2 的实际亮度差只有 5（0–255 刻度），
# 切换后几乎看不出变化；现在拉开到 170 / 240，差异清晰可辨。
_BG_LIFT = {0: 100, 1: 170, 2: 240}


def rgba(hex_color: str, alpha: float) -> str:
    """把 ``#RRGGBB`` 转成带透明度的 ``rgba(...)`` 字符串。

    雷达盘大量使用半透明叠色（刻度圈、扫描余晖、底图淡化），
    统一走这个函数便于全局调透明度。
    """
    color = QColor(hex_color)
    return f"rgba({color.red()}, {color.green()}, {color.blue()}, {alpha:.3f})"


def make_font(size: int, bold: bool = False, mono: bool = False) -> QFont:
    """构造像素字号字体。用像素而非点值，保证不同 DPI 下观感一致。"""
    font = QFont(MONO_FONT if mono else UI_FONT)
    font.setPixelSize(int(size))
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    return font


@dataclass(frozen=True)
class Theme:
    """一套完整的主题配置，由字号档位、扫描线颜色与背景深浅共同决定。"""

    font_size: str = "medium"
    scan_color: str = "#3FE08A"
    bg_level: int = 0

    # ---------- 字号 ----------

    @property
    def font_minor(self) -> int:
        return FONT_SCALES.get(self.font_size, FONT_SCALES["medium"])[0]

    @property
    def font_major(self) -> int:
        return FONT_SCALES.get(self.font_size, FONT_SCALES["medium"])[1]

    # ---------- 背景（受 bg_level 影响）----------

    def _lift(self, hex_color: str) -> str:
        """按背景档位提亮颜色。档位越高，整体基调越亮。"""
        level = max(0, min(2, int(self.bg_level)))
        if level == 0:
            return hex_color.upper()
        return QColor(hex_color).lighter(_BG_LIFT[level]).name().upper()

    @property
    def window_bg(self) -> str:
        return self._lift(_WINDOW_BG)

    @property
    def panel_bg(self) -> str:
        return self._lift(_PANEL_BG)

    @property
    def card_bg(self) -> str:
        return self._lift(_CARD_BG)

    @property
    def divider(self) -> str:
        return self._lift(_DIVIDER)

    @property
    def border(self) -> str:
        return self._lift(_BORDER)

    @property
    def button_bg(self) -> str:
        return self._lift(_BUTTON_BG)

    @property
    def radar_dish(self) -> str:
        return self._lift(_RADAR_DISH)

    # ---------- 固定色 ----------

    @property
    def land(self) -> str:
        return LAND

    @property
    def sea(self) -> str:
        return SEA

    @property
    def coastline(self) -> str:
        return COASTLINE

    @property
    def text_primary(self) -> str:
        return _TEXT_PRIMARY

    @property
    def text_secondary(self) -> str:
        return _TEXT_SECONDARY

    @property
    def text_dim(self) -> str:
        return _TEXT_DIM

    @property
    def scan(self) -> str:
        return self.scan_color.upper()

    @property
    def aircraft(self) -> str:
        return AIRCRAFT

    @property
    def widebody(self) -> str:
        return AIRCRAFT_WIDEBODY

    @property
    def highlight(self) -> str:
        return HIGHLIGHT

    # ---------- 派生色 ----------

    @property
    def row_hover_bg(self) -> str:
        return rgba(self.scan, 0.07)

    @property
    def row_selected_bg(self) -> str:
        return rgba(self.scan, 0.13)

    @property
    def accent_border(self) -> str:
        return rgba(self.scan, 0.55)

    # ---------- 应用 ----------

    def palette(self) -> QPalette:
        """构造 Qt 调色板。

        配合 Fusion 风格使用——QSS 覆盖不到的绘制路径（原生对话框、
        滚动条箭头等）会走调色板，二者一致才不会出现刺眼的浅色控件。
        """
        palette = QPalette()
        role = QPalette.ColorRole

        palette.setColor(role.Window, QColor(self.window_bg))
        palette.setColor(role.WindowText, QColor(self.text_primary))
        palette.setColor(role.Base, QColor(self.panel_bg))
        palette.setColor(role.AlternateBase, QColor(self.card_bg))
        palette.setColor(role.Text, QColor(self.text_primary))
        palette.setColor(role.Button, QColor(self.button_bg))
        palette.setColor(role.ButtonText, QColor(self.text_primary))
        palette.setColor(role.BrightText, QColor(self.highlight))
        palette.setColor(role.Highlight, QColor(self.highlight))
        palette.setColor(role.HighlightedText, QColor(self.window_bg))
        palette.setColor(role.ToolTipBase, QColor(self.card_bg))
        palette.setColor(role.ToolTipText, QColor(self.text_primary))
        palette.setColor(role.PlaceholderText, QColor(self.text_dim))
        palette.setColor(role.Link, QColor(self.highlight))

        # 禁用态整体压暗，避免灰白文字在暗底上反而更抢眼
        disabled = QPalette.ColorGroup.Disabled
        palette.setColor(disabled, role.Text, QColor(self.text_dim))
        palette.setColor(disabled, role.WindowText, QColor(self.text_dim))
        palette.setColor(disabled, role.ButtonText, QColor(self.text_dim))
        palette.setColor(disabled, role.Button, QColor(self.panel_bg))

        return palette

    def qss(self) -> str:
        """生成全局样式表。"""

        return _QSS_TEMPLATE.substitute(
            ui_font=UI_FONT,
            mono_font=MONO_FONT,
            font_minor=self.font_minor,
            font_major=self.font_major,
            window_bg=self.window_bg,
            panel_bg=self.panel_bg,
            card_bg=self.card_bg,
            divider=self.divider,
            border=self.border,
            button_bg=self.button_bg,
            text_primary=self.text_primary,
            text_secondary=self.text_secondary,
            text_dim=self.text_dim,
            scan=self.scan,
            highlight=self.highlight,
            row_hover=self.row_hover_bg,
            row_selected=self.row_selected_bg,
            accent_border=self.accent_border,
        )


# 样式表模板。$ 变量由 Theme.qss() 注入；CSS 花括号保持原样。
_QSS_TEMPLATE = Template(
    """
/* ================= 全局 ================= */
QWidget {
    font-family: "$ui_font";
    font-size: ${font_minor}px;
    color: $text_primary;
    background: transparent;
}
QMainWindow, QDialog {
    background: $window_bg;
}

/* ================= 容器 ================= */
/* 注意：直接继承 QWidget 的自定义类必须额外设置 WA_StyledBackground，
   否则 Qt 不会绘制 QSS 里的 background，界面会露出系统默认的浅色底。 */
QFrame#Panel, QWidget#Panel {
    background: $panel_bg;
    border: none;
}
QFrame#Card {
    background: $card_bg;
    border: 1px solid $border;
    border-radius: 6px;
}
QFrame#Divider {
    background: $divider;
    border: none;
    max-height: 1px;
    min-height: 1px;
}
QFrame#VDivider {
    background: $divider;
    border: none;
    max-width: 1px;
    min-width: 1px;
}
QWidget#HeaderBar {
    background: $card_bg;
    border-bottom: 1px solid $divider;
}

/* ================= 文字层级 ================= */
QLabel#TitleText {
    font-size: ${font_major}px;
    font-weight: 600;
    color: $text_primary;
}
QLabel#MetaText {
    color: $text_secondary;
}
QLabel#CaptionText {
    color: $text_dim;
    font-size: ${font_minor}px;
}
QLabel#MonoText {
    font-family: "$mono_font";
    color: $text_primary;
}
QLabel#MonoDimText {
    font-family: "$mono_font";
    color: $text_secondary;
}
QLabel#AccentText {
    color: $highlight;
}
QLabel#WidebodyTag {
    color: $text_dim;
    background: rgba(232, 197, 71, 0.14);
    border: 1px solid rgba(232, 197, 71, 0.42);
    border-radius: 3px;
    padding: 1px 5px;
    font-size: ${font_minor}px;
}
QLabel#SpecialTag {
    color: $text_dim;
    background: rgba(90, 255, 194, 0.12);
    border: 1px solid rgba(90, 255, 194, 0.40);
    border-radius: 3px;
    padding: 1px 5px;
    font-size: ${font_minor}px;
}

/* ================= 排行榜行 ================= */
QFrame#FlightRow {
    background: transparent;
    border: 1px solid transparent;
    border-radius: 5px;
}
QFrame#FlightRow:hover {
    background: $row_hover;
    border-color: $border;
}
QFrame#FlightRow[selected="true"] {
    background: $row_selected;
    border-color: $accent_border;
}
QLabel#RowNumber {
    font-family: "$mono_font";
    font-size: ${font_major}px;
    font-weight: 600;
    color: $text_primary;
}
QLabel#RowDistance {
    font-family: "$mono_font";
    font-size: ${font_major}px;
    color: $highlight;
}
QFrame#ListContainer {
    background: transparent;
}
QListWidget#AirportList {
    background: $card_bg;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px;
    outline: none;
}
QListWidget#AirportList::item {
    padding: 6px 8px;
    border-radius: 4px;
    color: $text_primary;
}
QListWidget#AirportList::item:hover {
    background: $row_hover;
}
QListWidget#AirportList::item:selected {
    background: $row_selected;
    color: $highlight;
}
QLabel#SectionTitle {
    font-size: ${font_major}px;
    font-weight: 600;
    color: $text_primary;
}

/* ================= 详情页字段 ================= */
QLabel#FieldLabel {
    color: $text_dim;
    font-size: ${font_minor}px;
}
QLabel#FieldValue {
    color: $text_primary;
}
QLabel#FieldValueMono {
    font-family: "$mono_font";
    color: $text_primary;
}
QLabel#PhotoFrame {
    background: $card_bg;
    border: 1px solid $border;
    border-radius: 6px;
    color: $text_dim;
}

/* ================= 按钮 ================= */
QPushButton {
    background: $button_bg;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 5px 14px;
    color: $text_primary;
}
QPushButton:hover {
    border-color: $scan;
    color: $highlight;
}
QPushButton:pressed {
    background: $panel_bg;
}
QPushButton:disabled {
    color: $text_dim;
    border-color: $divider;
}
QPushButton#GhostButton {
    background: transparent;
    border: none;
    padding: 4px 8px;
    color: $text_secondary;
}
QPushButton#GhostButton:hover {
    color: $highlight;
}
QPushButton#GhostButton:pressed {
    color: $scan;
}

/* ================= 输入控件 ================= */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {
    background: $card_bg;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 4px 8px;
    color: $text_primary;
    selection-background-color: $scan;
    selection-color: $window_bg;
}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {
    border-color: $scan;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {
    border-color: $scan;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled {
    color: $text_dim;
    border-color: $divider;
}
QLineEdit[readOnly="true"] {
    color: $text_secondary;
    background: $panel_bg;
}

/* 数字输入框的微调按钮做成窄条，避免默认大箭头破坏精致感 */
QSpinBox::up-button, QDoubleSpinBox::up-button,
QSpinBox::down-button, QDoubleSpinBox::down-button {
    background: transparent;
    border: none;
    width: 14px;
}
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-bottom: 4px solid $text_secondary;
    width: 0;
    height: 0;
}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid $text_secondary;
    width: 0;
    height: 0;
}
QSpinBox::up-arrow:hover, QDoubleSpinBox::up-arrow:hover {
    border-bottom-color: $highlight;
}
QSpinBox::down-arrow:hover, QDoubleSpinBox::down-arrow:hover {
    border-top-color: $highlight;
}

/* 下拉框箭头同样用 border 三角绘制 */
QComboBox::drop-down {
    border: none;
    width: 20px;
}
QComboBox::down-arrow {
    image: none;
    border-left: 3px solid transparent;
    border-right: 3px solid transparent;
    border-top: 4px solid $text_secondary;
    width: 0;
    height: 0;
    margin-right: 7px;
}
QComboBox::down-arrow:hover {
    border-top-color: $highlight;
}
QComboBox QAbstractItemView {
    background: $card_bg;
    border: 1px solid $border;
    border-radius: 4px;
    outline: none;
    padding: 2px;
    selection-background-color: $panel_bg;
    selection-color: $highlight;
}

/* ================= 复选框 / 单选 ================= */
QCheckBox, QRadioButton {
    spacing: 8px;
    color: $text_primary;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 14px;
    height: 14px;
    border: 1px solid $border;
    background: $card_bg;
}
QCheckBox::indicator {
    border-radius: 3px;
}
QRadioButton::indicator {
    border-radius: 7px;
}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {
    border-color: $scan;
}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {
    background: $scan;
    border-color: $scan;
}

/* ================= 滑块 ================= */
QSlider::groove:horizontal {
    background: $divider;
    height: 3px;
    border-radius: 2px;
}
QSlider::sub-page:horizontal {
    background: $scan;
    height: 3px;
    border-radius: 2px;
}
QSlider::handle:horizontal {
    background: $text_primary;
    border: none;
    width: 11px;
    height: 11px;
    margin: -4px 0;
    border-radius: 6px;
}
QSlider::handle:horizontal:hover {
    background: $highlight;
}

/* ================= 滚动条 ================= */
QScrollArea {
    border: none;
    background: transparent;
}
QScrollBar:vertical {
    background: transparent;
    width: 8px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: $border;
    border-radius: 4px;
    min-height: 28px;
}
QScrollBar::handle:vertical:hover {
    background: $scan;
}
QScrollBar:horizontal {
    background: transparent;
    height: 8px;
    margin: 0;
}
QScrollBar::handle:horizontal {
    background: $border;
    border-radius: 4px;
    min-width: 28px;
}
QScrollBar::handle:horizontal:hover {
    background: $scan;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0;
    height: 0;
    background: transparent;
    border: none;
}
QScrollBar::add-page, QScrollBar::sub-page {
    background: transparent;
}

/* ================= 分组框 ================= */
QGroupBox {
    border: 1px solid $border;
    border-radius: 6px;
    margin-top: 14px;
    padding-top: 8px;
    color: $text_secondary;
}
QGroupBox::title {
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 10px;
    padding: 0 5px;
    color: $text_secondary;
    background: transparent;
}

/* ================= 工具提示 ================= */
QToolTip {
    background: $card_bg;
    color: $text_primary;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 4px 7px;
}
"""
)
