"""流程图组件的测试（lst/ui/settings/flow.py，需求 UI-05、UI-07）：节点、箭头、换行、分支、绕行虚线、选中、
键盘操作与节点上的开关。"""

import pytest
from PySide6.QtCore import QPoint, QSizeF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from lst.ui import i18n
from lst.ui.settings import strings_flow  # noqa: F401  # pyright: ignore[reportUnusedImport]  登记文案
from lst.ui.settings.flow import (
    GUTTER,
    ConnectorKind,
    FlowBranch,
    FlowChart,
    FlowItem,
    FlowLayout,
    FlowStep,
    FlowView,
    NodeState,
    flow_keys,
    layout_flow,
)

NODE = QSizeF(150, 58)
STEPS: tuple[FlowItem, ...] = (FlowStep("a"), FlowStep("b"), FlowStep("c"), FlowStep("d"))
BRANCHED: tuple[FlowItem, ...] = (FlowStep("a"), FlowBranch((("x",), ("y", "z"))), FlowStep("d"))


def flows(items: tuple[FlowItem, ...], width: float, bypassed: frozenset[str] = frozenset()) -> FlowLayout:
    return layout_flow(items, width=width, node=NODE, bypassed=bypassed)


# ---------------------------------------------------------------- 纯几何


def test_keys_follow_reading_order_with_lanes_in_turn() -> None:
    assert flow_keys(BRANCHED) == ["a", "x", "y", "z", "d"]


def test_nodes_are_placed_left_to_right_and_joined_by_arrows() -> None:
    result = flows(STEPS, 2000)
    assert result.rows == (("a", "b", "c", "d"),)
    lefts = [result.nodes[key].left() for key in "abcd"]
    assert lefts == sorted(lefts) and lefts[0] == GUTTER
    assert len({result.nodes[key].top() for key in "abcd"}) == 1
    arrows = [c for c in result.connectors if c.kind is ConnectorKind.FLOW]
    assert [(c.source, c.target) for c in arrows] == [("a", "b"), ("b", "c"), ("c", "d")]
    for connector in arrows:
        assert connector.arrow and len(connector.points) == 2
        # 箭头从前一个节点的右边缘画到后一个节点的左边缘，高度在两个节点的中线上。
        start, end = connector.points
        assert start.x() == result.nodes[connector.source].right()
        assert end.x() == result.nodes[connector.target].left()
        assert start.y() == end.y() == result.nodes[connector.target].center().y()


def test_rows_wrap_and_the_wrap_line_runs_back_through_the_left_gutter() -> None:
    result = flows(STEPS, 2 * GUTTER + 2 * NODE.width() + 40)
    assert result.rows == (("a", "b"), ("c", "d"))
    assert result.nodes["c"].top() > result.nodes["a"].bottom()
    wrap = next(c for c in result.connectors if c.source == "b" and c.target == "c")
    assert wrap.arrow and len(wrap.points) == 6
    first, *_, last = wrap.points
    assert first.x() == result.nodes["b"].right()
    assert last.x() == result.nodes["c"].left() and last.y() == result.nodes["c"].center().y()
    # 折返的横线在两行之间，竖线在左侧留白里，不穿过任何节点。
    middle_y = wrap.points[2].y()
    assert result.nodes["b"].bottom() < middle_y < result.nodes["c"].top()
    assert wrap.points[3].x() < result.nodes["c"].left()
    assert result.height >= result.nodes["d"].bottom()


def test_branch_forks_into_lanes_and_joins_before_the_next_node() -> None:
    result = flows(BRANCHED, 2000)
    connectors = result.connectors
    forks = [c for c in connectors if c.target in ("x", "y") and c.source == ""]
    assert len(forks) == 2 and all(c.arrow for c in forks)
    assert [c.points[-1].y() for c in forks] == [
        result.nodes["x"].center().y(),
        result.nodes["y"].center().y(),
    ]
    inside = next(c for c in connectors if (c.source, c.target) == ("y", "z"))
    assert inside.arrow
    joins = [c for c in connectors if c.source in ("x", "z") and c.target == ""]
    assert len(joins) == 2 and not any(c.arrow for c in joins)
    # 两条路的末端汇合到同一点，再由一条带箭头的线接到下一个节点。
    assert joins[0].points[-1] == joins[1].points[-1]
    out = next(c for c in connectors if c.target == "d")
    assert out.arrow and out.points[0] == joins[0].points[-1]
    into = next(c for c in connectors if c.source == "a")
    assert not into.arrow and into.points[-1] == forks[0].points[0]
    # 两条路上下排开，较短的一条靠左对齐。
    assert result.nodes["x"].left() == result.nodes["y"].left()
    assert result.nodes["x"].bottom() < result.nodes["y"].top()


def test_skipped_nodes_fade_and_get_one_dashed_bypass_per_run() -> None:
    result = flows(STEPS, 2000, frozenset({"b", "c"}))
    bypasses = [c for c in result.connectors if c.kind is ConnectorKind.BYPASS]
    assert len(bypasses) == 1
    bypass = bypasses[0]
    assert (bypass.source, bypass.target) == ("b", "c") and bypass.arrow
    left, top_left, top_right, right = bypass.points
    # 从 b 前面的连线上引出，越过 b、c 的上方，落在 c 后面的连线上。
    assert result.nodes["a"].right() < left.x() < result.nodes["b"].left()
    assert result.nodes["c"].right() < right.x() < result.nodes["d"].left()
    assert top_left.y() == top_right.y() < result.nodes["b"].top()
    assert left.y() == right.y() == result.nodes["b"].center().y()
    faded = {(c.source, c.target) for c in result.connectors if c.faded}
    assert faded == {("a", "b"), ("b", "c"), ("c", "d")}


def test_bypass_runs_are_split_at_a_wrap() -> None:
    result = flows(STEPS, 2 * GUTTER + 2 * NODE.width() + 40, frozenset({"b", "c"}))
    bypasses = [c for c in result.connectors if c.kind is ConnectorKind.BYPASS]
    assert [(c.source, c.target) for c in bypasses] == [("b", "b"), ("c", "c")]
    assert bypasses[1].points[0].x() > 0  # 行首的绕行虚线仍在左侧留白之内


# ---------------------------------------------------------------- 控件


@pytest.fixture
def chart(qapp: QApplication) -> FlowChart:
    i18n.set_language("zh-Hans")
    widget = FlowChart(["a", "x", "y", "z", "d", "unused"])
    widget.resize(1600, 300)
    widget.set_items(BRANCHED)
    widget.set_state("a", NodeState("开始", "第一步"))
    widget.set_state("x", NodeState("开关步骤", "已开启", switch=True))
    widget.set_state("y", NodeState("关闭的步骤", "已关闭", switch=False, bypassed=True))
    return widget


def test_only_listed_nodes_are_shown_and_unknown_keys_are_rejected(chart: FlowChart) -> None:
    assert [key for key, node in chart.nodes.items() if not node.isHidden()] == ["a", "x", "y", "z", "d"]
    with pytest.raises(KeyError):
        chart.set_items((FlowStep("nope"),))


def test_nodes_describe_themselves_for_screen_readers(chart: FlowChart) -> None:
    chart.set_selected("x")
    node = chart.nodes["x"]
    assert node.accessibleName() == "开关步骤"
    description = node.accessibleDescription()
    for part in ("已开启", "开关已打开", "第 2 步，共 5 步", "设置显示在流程图下方", "按空格切换开关"):
        assert part in description
    assert "已跳过" in chart.nodes["y"].accessibleDescription()
    assert chart.nodes["x"].selected and not chart.nodes["a"].selected


def test_only_the_current_node_takes_tab_focus(chart: FlowChart) -> None:
    policies = {key: chart.nodes[key].focusPolicy() for key in chart.order}
    assert policies["a"] == Qt.FocusPolicy.StrongFocus
    assert all(policy == Qt.FocusPolicy.ClickFocus for key, policy in policies.items() if key != "a")
    chart.set_selected("z")
    assert chart.nodes["z"].focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert chart.nodes["a"].focusPolicy() == Qt.FocusPolicy.ClickFocus


def test_keyboard_moves_between_nodes_opens_and_toggles(chart: FlowChart) -> None:
    activated: list[str] = []
    toggled: list[tuple[str, bool]] = []
    chart.activated.connect(activated.append)

    def on_toggle(key: str, value: bool) -> None:
        toggled.append((key, value))

    chart.toggled.connect(on_toggle)
    QTest.keyClick(chart.nodes["a"], Qt.Key.Key_Right)
    assert chart.current == "x"
    QTest.keyClick(chart.nodes["x"], Qt.Key.Key_Space)  # 有开关：空格切换开关
    QTest.keyClick(chart.nodes["x"], Qt.Key.Key_Return)  # Enter 打开设置
    QTest.keyClick(chart.nodes["x"], Qt.Key.Key_Left)
    QTest.keyClick(chart.nodes["a"], Qt.Key.Key_Space)  # 没有开关：空格打开设置
    QTest.keyClick(chart.nodes["a"], Qt.Key.Key_End)
    assert chart.current == "d"
    QTest.keyClick(chart.nodes["d"], Qt.Key.Key_Home)
    assert chart.current == "a"
    assert toggled == [("x", False)]
    assert activated == ["x", "a"]


def test_clicking_the_switch_toggles_and_clicking_elsewhere_opens(chart: FlowChart) -> None:
    activated: list[str] = []
    toggled: list[tuple[str, bool]] = []
    chart.activated.connect(activated.append)

    def on_toggle(key: str, value: bool) -> None:
        toggled.append((key, value))

    chart.toggled.connect(on_toggle)
    node = chart.nodes["y"]
    switch = node.switch_rect
    assert switch is not None and chart.nodes["a"].switch_rect is None
    QTest.mouseClick(node, Qt.MouseButton.LeftButton, pos=switch.center().toPoint())
    QTest.mouseClick(node, Qt.MouseButton.LeftButton, pos=QPoint(20, node.height() // 2))
    assert toggled == [("y", True)] and activated == ["y"]


def test_nodes_are_positioned_where_the_layout_says(chart: FlowChart) -> None:
    for key in chart.order:
        rect = chart.layout_result.nodes[key]
        geometry = chart.nodes[key].geometry()
        assert abs(geometry.center().x() - rect.center().x()) <= 1
        assert abs(geometry.center().y() - rect.center().y()) <= 1
    assert chart.heightForWidth(1600) < chart.heightForWidth(400)  # 窄了就换行，变高


def test_view_shows_only_the_opened_panel_and_falls_back_when_its_node_goes_away(qapp: QApplication) -> None:
    i18n.set_language("zh-Hans")
    view = FlowView(["a", "b", "c"])
    panels = {key: QLabel(key) for key in "abc"}
    for key, panel in panels.items():
        view.add_panel(key, panel)
    view.set_items(tuple(FlowStep(key) for key in "abc"), fallback="b")
    assert view.selected == "b"
    opened: list[str] = []
    view.opened.connect(opened.append)
    view.chart.activate("c")  # 点击节点
    assert view.selected == "c" and opened == ["c"]
    assert [key for key, panel in panels.items() if not panel.isHidden()] == ["c"]
    view.set_items((FlowStep("a"), FlowStep("b")), fallback="a")
    assert view.selected == "a" and not panels["a"].isHidden() and panels["c"].isHidden()
    with pytest.raises(KeyError):
        view.add_panel("nope", QWidget())
