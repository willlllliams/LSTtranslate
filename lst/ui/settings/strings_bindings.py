# ruff: noqa: E501  文案表里一条文案写在一行，方便对照中英文；折行后反而难读。
"""“热键与键位”下三个窗口键位子页的界面文案（需求 UI-09）。导入本模块时登记文案。

操作名用 ``lst.ui.common.bindings`` 登记的 ``bindings.action.<操作 id>``，子页的名称用 ``strings_nav`` 的
``settings.nav.hotkeys.<窗口>``，这里都不重复。键的约定：

- 每个窗口子页开头的说明：``settings.bindings.scope.<窗口>.intro``；
- 录制提示：``settings.bindings.record.<操作类型>``；
- 键位的显示名：``settings.bindings.button.<按键>``、``settings.bindings.gesture.<动作>``、``settings.bindings.wheel``。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog


def _pair(zh: str, en: str) -> dict[str, str]:
    return {"zh-Hans": zh, "en": en}


CATALOG: Final[Catalog] = {
    "settings.bindings.intro": _pair(
        "每个操作可以换成别的按键或鼠标动作，最多 {count} 个键位，也可以不绑定。同一个窗口里的两个操作不能用同一个键位，不同窗口之间可以重复。",
        "Each action can use other keys or mouse actions, up to {count} bindings, or none. Two actions in the same window cannot share a binding; different windows can.",
    ),
    "settings.bindings.unavailable": _pair(
        "这个版本没有提供窗口内键位的设置。", "This build does not provide in-window binding settings."
    ),
    "settings.bindings.reset_page": _pair("恢复本页默认(&R)", "&Reset this page"),
    "settings.bindings.reset_page.confirm": _pair(
        "把“{window}”的全部键位恢复成默认值？其他窗口的键位不变。",
        "Reset every binding of “{window}” to its default? Bindings of the other windows stay as they are.",
    ),
    # 三个窗口子页开头的说明
    "settings.bindings.scope.capture.intro": _pair(
        "按下截屏热键后盖在屏幕上的蒙版，用来框选要翻译的区域。",
        "The overlay that covers the screen after the capture hotkey, used to select the area to translate.",
    ),
    "settings.bindings.scope.overlay.intro": _pair(
        "盖在截屏位置上显示译文的窗口。缩放的是整张图（底图与译文一起），默认用滚轮。窗口里的原文或译文可以逐字选中，用系统的复制键（Ctrl+C）复制，这个键不在这里设置。",
        "The window that shows the translation over the captured area. Zooming scales the whole image (the screenshot and the translation together); the mouse wheel does it by default. The source or translated text in the window can be selected character by character and copied with the system copy key (Ctrl+C), which is not set here.",
    ),
    "settings.bindings.scope.bilingual.intro": _pair(
        "双语互译窗口里的快捷键。译文与各引擎的结果可以选中后用系统的复制键（Ctrl+C）复制，复制全部译文用“复制译文”按钮。",
        "Shortcuts in the bilingual translator window. The translation and the engine results can be selected and copied with the system copy key (Ctrl+C); the Copy translation button copies the whole translation.",
    ),
    # 每个操作一行
    "settings.bindings.none": _pair("未绑定", "Not bound"),
    "settings.bindings.add.tip": _pair("添加键位", "Add a binding"),
    "settings.bindings.add.name": _pair("为“{action}”添加键位", "Add a binding to “{action}”"),
    "settings.bindings.reset.tip": _pair("恢复默认键位：{defaults}", "Reset to the default: {defaults}"),
    "settings.bindings.reset.name": _pair(
        "把“{action}”恢复成默认键位", "Reset “{action}” to its default bindings"
    ),
    "settings.bindings.remove.name": _pair("删除键位 {binding}", "Remove the binding {binding}"),
    "settings.bindings.separator": _pair("、", ", "),
    # 录制
    "settings.bindings.record.trigger": _pair(
        "按下组合键，或在这里单击、双击鼠标；Esc 取消",
        "Press a key combination, or click or double-click here; Esc cancels",
    ),
    "settings.bindings.record.drag": _pair(
        "按住鼠标按键在这里拖动，可以同时按住 Ctrl、Alt、Shift；Esc 取消",
        "Hold a mouse button and drag here, optionally with Ctrl, Alt or Shift; Esc cancels",
    ),
    "settings.bindings.record.wheel": _pair(
        "在这里滚动鼠标滚轮，可以同时按住 Ctrl、Alt、Shift；Esc 取消",
        "Turn the mouse wheel here, optionally with Ctrl, Alt or Shift; Esc cancels",
    ),
    "settings.bindings.record.name": _pair("为“{action}”录制键位", "Record a binding for “{action}”"),
    "settings.bindings.manual": _pair("改为手动输入(&M)", "Type it in &manually"),
    "settings.bindings.manual.tip": _pair(
        "要绑定 Esc，或者想直接写键位时用。写法例如 Ctrl+Shift+C、Esc、RightClick、Ctrl+LeftDrag、Ctrl+Wheel。",
        "Use this to bind Esc, or to type a binding directly, for example Ctrl+Shift+C, Esc, RightClick, Ctrl+LeftDrag or Ctrl+Wheel.",
    ),
    "settings.bindings.manual.placeholder": _pair(
        "例如 Esc、Ctrl+C、RightClick、Ctrl+Wheel", "For example Esc, Ctrl+C, RightClick, Ctrl+Wheel"
    ),
    "settings.bindings.manual.name": _pair("“{action}”的新键位", "New binding for “{action}”"),
    "settings.bindings.manual.ok": _pair("确定(&O)", "&OK"),
    "settings.bindings.cancel": _pair("取消(&C)", "&Cancel"),
    # 冲突与错误
    "settings.bindings.conflict": _pair(
        "与“{action}”冲突：两者都用了 {binding}。",
        "Conflicts with “{action}”: both use {binding}.",
    ),
    "settings.bindings.conflict.hint": _pair(
        "替换会把这个键位从原来的操作上移除，再加到这里。",
        "Replacing removes the binding from the other action and adds it here.",
    ),
    "settings.bindings.replace": _pair("替换(&P)", "Re&place"),
    "settings.bindings.exists": _pair("这个操作已经有 {binding} 了", "This action already has {binding}"),
    # 键位的显示名
    "settings.bindings.button.Left": _pair("左键", "Left"),
    "settings.bindings.button.Right": _pair("右键", "Right"),
    "settings.bindings.button.Middle": _pair("中键", "Middle"),
    "settings.bindings.gesture.Click": _pair("{button}单击", "{button} click"),
    "settings.bindings.gesture.DoubleClick": _pair("{button}双击", "{button} double-click"),
    "settings.bindings.gesture.Drag": _pair("{button}拖动", "{button} drag"),
    "settings.bindings.wheel": _pair("滚轮", "Wheel"),
}

register_catalog(CATALOG)

__all__ = ["CATALOG"]
