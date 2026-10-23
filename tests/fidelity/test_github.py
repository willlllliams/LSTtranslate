"""用户提供的 GitHub 原图：正文成段，标题、链接与按钮保留独立位置。

只验证明确标出的正文区域；缩略图的字符准确率另行记录，不能用合并结果掩盖漏字。
"""

import pytest

from tests.fidelity.evaluate import canonical
from tests.fidelity.pipeline import ENGINES, FIXTURES, load_raw


@pytest.mark.parametrize("engine", list(ENGINES))
def test_dialog_body_is_one_paragraph_in_all_four_engines(engine: str) -> None:
    result = load_raw(ENGINES[engine], "github-dialog", FIXTURES / "raw")
    body = [p for p in result.segments if 60 < p.bbox.y0 < 160]
    assert len(body) == 1, [(p.bbox, p.segment.text) for p in body]
    paragraph = body[0]
    assert paragraph.bbox.y1 < 170
    assert "createapublicrepositorycalledgithub" in canonical(paragraph.segment.text)
    assert canonical(paragraph.segment.text).endswith("visibletoanyone")
    assert any(p.bbox.y1 < 40 and canonical(p.segment.text) == "createpublicreadme" for p in result.segments)
    assert any(
        170 < p.bbox.y0 < 200 and canonical(p.segment.text) == "readdocumentation" for p in result.segments
    )
    assert any(p.bbox.y0 > 220 and canonical(p.segment.text) == "cancel" for p in result.segments)


@pytest.mark.parametrize("engine", list(ENGINES))
def test_organization_subtitle_stays_on_one_line(engine: str) -> None:
    result = load_raw(ENGINES[engine], "github-org", FIXTURES / "raw")
    subtitle = [p for p in result.segments if 580 < p.bbox.y0 < 625 and p.bbox.x0 < 1500]
    assert len(subtitle) == 1, [(p.bbox, p.segment.text) for p in subtitle]
    assert canonical(subtitle[0].segment.text) == canonical(
        "We've suggested some tasks here in your organization's overview to help you get started."
    )
    assert subtitle[0].bbox.y1 < 630


def test_small_dialog_rapid_body_is_not_split() -> None:
    result = load_raw(ENGINES["rapid"], "github-dialog-small", FIXTURES / "raw")
    body = [p for p in result.segments if 20 < p.bbox.y0 < 55]
    assert len(body) == 1
    assert body[0].bbox.y1 < 58
