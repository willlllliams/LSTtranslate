"""截屏蒙版的文案表（需求 CFG-05）。导入本模块时登记文案。

通知的标题与正文也登记在这里：截屏模块发布 NotificationRequested 时只传文案键，托盘按键取文案。
"""

from lst.ui.i18n import Catalog, register_catalog

CATALOG: Catalog = {
    # 提示条按当前键位拼出（UI-09），每个操作一项，例如“框选区域：左键拖动”；没有绑定的操作不出现。
    "capture.hint.item": {"zh-Hans": "{action}：{keys}", "en": "{action}: {keys}"},
    "capture.hint.separator": {"zh-Hans": "　　", "en": "   "},
    "capture.size": {"zh-Hans": "{width} × {height}", "en": "{width} × {height}"},
    "capture.screens_changed.title": {"zh-Hans": "截屏已取消", "en": "Screen capture cancelled"},
    "capture.screens_changed.message": {
        "zh-Hans": "截屏期间显示器的连接、分辨率或缩放比例发生了变化，请重新截屏",
        "en": "A display was connected, disconnected, or rescaled during the capture. Please capture again",
    },
    "capture.failed.title": {"zh-Hans": "无法截屏", "en": "Cannot capture the screen"},
    "capture.failed.message": {
        "zh-Hans": "读取屏幕画面失败，请重新截屏",
        "en": "Reading the screen image failed. Please capture again",
    },
}

register_catalog(CATALOG)
