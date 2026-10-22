"""无位置引擎的输出。

位置约定（lst/contracts/ocr_engines.py 的模块说明）：只返回纯文本、给不出文字位置的引擎（例如以后接入的
大模型视觉识别），把整张截图作为一个大包围盒，输出一个 text 块：

- 块的 bbox 为整张截图，order 为 0，text 为全部识别文字（保留原有的换行）；
- lines 只有一行，bbox 同样覆盖整张截图，text 是把各行拼成一行后的文字。

主进程据此把译文铺满整个选区。只给一行，是因为文本模式下主进程把 lines 交给段落合并器：
同一个包围盒的多行会被当成重叠的行，合并结果取决于合并器的阈值；拼成一行后整段文字一定是一个段。

各行拼成一行用 lst.contracts.text.join_lines，与主进程拼接段落的规则相同：任一侧是中日文字符时直接相连，
其余情况加一个空格。
"""

from lst.contracts.ocr import BlockCategory
from lst.contracts.text import join_lines
from lst.workers.ocr_worker.blocks import OutBlock, OutLine


def plain_text_blocks(text: str, width: int, height: int, score: float = 1.0) -> list[OutBlock]:
    """无位置引擎的输出块，见模块说明。全部文字为空白时返回空列表。

    width、height 是原始截图的宽高；score 是整段文字的置信度，引擎不给置信度时用 1.0。
    """
    lines = [line.strip() for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")]
    lines = [line for line in lines if line]
    if not lines:
        return []
    whole = (0, 0, width, height)
    return [
        OutBlock(
            BlockCategory.TEXT,
            whole,
            0,
            score,
            text="\n".join(lines),
            lines=[OutLine(whole, join_lines(lines), score)],
        )
    ]
