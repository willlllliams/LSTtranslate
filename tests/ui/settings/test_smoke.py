from tests.ui.settings.conftest import Harness


async def test_module_starts_and_window_opens(harness: Harness) -> None:
    window = await harness.open()
    assert window.isVisible()
    assert window.windowTitle() == "设置"
