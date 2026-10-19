"""通用小控件：分隔线、状态标签、可点击链接、字段行。

把这些抽出来是为了让排行榜与详情页保持一致的间距与视觉语言——
面板的精致感很大程度上来自"每个留白都相同"。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget


class Divider(QFrame):
    """一条细水平分隔线。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Divider")
        self.setFixedHeight(1)


class TagLabel(QLabel):
    """小状态标签。

    宽体机用琥珀黄，特殊涂装 / 特殊机型用青绿，两者都能一眼区分。
    """

    def __init__(self, text: str, kind: str = "widebody", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setObjectName("WidebodyTag" if kind == "widebody" else "SpecialTag")
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)


class LinkLabel(QLabel):
    """可点击的文字链接，用于跳转生产商官网。

    没有可用链接时退化为普通文本并把鼠标指针还原成箭头——
    让用户从指针形状就能看出"这里能不能点"。
    """

    activated = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._url = ""
        self.setObjectName("FieldValueMono")
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_link(self, text: str, url: str | None) -> None:
        self._url = url or ""
        if self._url:
            # 加一个箭头符号，明确提示"可跳转"
            self.setText(f"{text}  ↗")
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setObjectName("AccentText")
        else:
            self.setText(text or "—")
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.setObjectName("FieldValueMono")
        # objectName 变化后需要重新应用样式表
        self.style().unpolish(self)
        self.style().polish(self)

    @property
    def url(self) -> str:
        return self._url

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        if self._url and event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self._url)


def make_field_row(label_text: str, mono: bool = True) -> tuple[QWidget, QLabel]:
    """构造「字段名 + 字段值」的一行，返回值标签以便后续更新。

    标签列固定宽度，保证多行字段的取值左边缘严格对齐——
    这是详情页看起来整齐的关键。
    """
    container = QWidget()
    layout = QHBoxLayout(container)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)

    label = QLabel(label_text)
    label.setObjectName("FieldLabel")
    label.setFixedWidth(72)
    label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)

    value = QLabel("—")
    value.setObjectName("FieldValueMono" if mono else "FieldValue")
    value.setWordWrap(True)
    value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

    layout.addWidget(label)
    layout.addWidget(value, 1)
    return container, value
