"""Windows 10 的无 Mica 背景与缺少 windows11 样式时的回退。"""

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QStyleFactory, QWidget

from lst.ui import theme


def test_missing_windows11_style_uses_fusion(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(QStyleFactory, "keys", lambda: ["Windows", "Fusion"])
        theme.apply_app_style(qapp)
        assert qapp.style().objectName().lower() == "fusion"
    theme.apply_app_style(qapp)


def test_unsupported_mica_removes_transparency(qapp: QApplication, monkeypatch: pytest.MonkeyPatch) -> None:
    widget = QWidget()
    widget.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen)
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    monkeypatch.setattr(theme, "mica_supported", lambda: False)
    assert not theme.enable_mica(widget, dark=True)
    assert not widget.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    widget.close()
