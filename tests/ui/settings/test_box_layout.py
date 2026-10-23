"""翻译框参数经过真实配置服务保存、恢复并热更新覆盖窗口。"""

import tomllib

from lst.ui.settings.controls import ChoiceEditor, FloatEditor
from tests.ui.settings.conftest import StartFull


async def test_box_parameters_round_trip_through_settings_and_restart(start_full: StartFull) -> None:
    full = await start_full()
    window = await full.open()
    window.show_page("capture.options")
    page = window.page("capture.options")
    bindings = {b.location[-1]: b for b in page.all_bindings() if b.location[0] == "overlay_layout"}
    assert len(bindings) == 12
    scale = bindings["font_scale"].editor
    growth = bindings["growth"].editor
    assert isinstance(scale, FloatEditor) and isinstance(growth, ChoiceEditor)
    scale.spin.setValue(1.4)
    growth.combo.setCurrentIndex(growth.combo.findData("vertical"))
    await full.settle()
    values = tomllib.loads(full.config_path.read_text("utf-8"))["overlay_layout"]
    assert values["font_scale"] == 1.4 and values["growth"] == "vertical"
    await full.stop()
    second = await start_full()
    restored = await second.open()
    restored.show_page("capture.options")
    page = restored.page("capture.options")
    assert (
        next(
            b for b in page.all_bindings() if b.location == ("overlay_layout", "font_scale")
        ).editor.json_value()
        == 1.4
    )
