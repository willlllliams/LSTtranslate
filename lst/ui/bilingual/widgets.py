"""双语窗口的译文区控件。只负责绘制和收集输入。"""

from collections.abc import Collection, Iterable, Sequence
from functools import partial

from PySide6.QtCore import QEasingCurve, Qt, QVariantAnimation
from PySide6.QtGui import QBrush, QColor, QPalette, QTextBlockFormat, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextEdit, QWidget

from lst.ui.theme import TOKENS

# 段内换行用 Unicode 行分隔符（U+2028）表示，一段因此恰好对应文档里的一个文本块，序号可以直接换算。
_LINE_SEPARATOR = chr(0x2028)
_PARAGRAPH_SEPARATOR = chr(0x2029)


def _one_block(text: str) -> str:
    """把一段文字里的各种换行统一成行分隔符。"""
    unified = text.replace("\r\n", "\n").replace("\r", "\n").replace(_PARAGRAPH_SEPARATOR, "\n")
    return unified.replace("\n", _LINE_SEPARATOR)


class TranslationPane(QTextEdit):
    """译文区。每段一个文本块，段的译文替换时播放淡入（需求 RACE-06）。

    更新只改动给出的文本块，并且整批更新放在一个编辑块里，文档只重新排版一次（需求 REL-04）。
    淡入用文字颜色的透明度实现：同一批开始淡入的段共用一个 QVariantAnimation，每一帧也只排版一次。
    一段再次淡入时离开原来那一组，动画结束后恢复为默认文字格式；其他组照常继续，互不重置。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setAcceptRichText(False)
        self.setUndoRedoEnabled(False)
        self.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._block_format = QTextBlockFormat()
        self._block_format.setBottomMargin(TOKENS.spacing_s)
        self._count = 0
        self._fade_of: dict[int, QVariantAnimation] = {}
        self._fade_members: dict[QVariantAnimation, set[int]] = {}

    # ------------------------------------------------------------------ 查询

    def segment_count(self) -> int:
        return self._count

    def segment_text(self, index: int) -> str:
        return self.document().findBlockByNumber(index).text().replace(_LINE_SEPARATOR, "\n")

    def fade_animation(self, index: int) -> QVariantAnimation | None:
        """第 index 段正在播放的淡入动画。"""
        return self._fade_of.get(index)

    # ------------------------------------------------------------------ 更新

    def set_segments(self, texts: Sequence[str], fade: Collection[int]) -> None:
        """整体替换全部段。"""
        self._stop_fades()
        self._count = len(texts)
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        # 在原文档上整体替换，不用 setPlainText：后者会把滚动条拉回顶部，用户边输入边看长文的末尾时，
        # 每次停顿都会跳回开头。没有段时文档里留一个空文本块。
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText("\n".join(_one_block(text) for text in texts), QTextCharFormat())
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.setBlockFormat(self._block_format)
        transparent = self._alpha_format(0.0)
        for index in fade:
            self._select_block(cursor, index)
            cursor.mergeCharFormat(transparent)
        cursor.endEditBlock()
        self._start_fade(fade)
        self.viewport().update()

    def update_segments(self, changes: Sequence[tuple[int, str]], fade: Collection[int]) -> None:
        """只替换给出的段；序号等于当前段数时追加。fade 中的段从透明开始淡入。"""
        fading = set(fade)
        transparent = self._alpha_format(0.0)
        opaque = QTextCharFormat()
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        for index, text in changes:
            if index in fading:
                char_format = transparent
            else:
                self._leave_fade(index)
                char_format = opaque
            if index < self._count:
                self._select_block(cursor, index)
            elif index == self._count:
                if self._count:
                    cursor.movePosition(QTextCursor.MoveOperation.End)
                    cursor.insertBlock(self._block_format, QTextCharFormat())
                else:
                    # 空文档里只有一个空文本块，第一段直接写进去。
                    cursor.select(QTextCursor.SelectionType.Document)
                    cursor.setBlockFormat(self._block_format)
                self._count += 1
            else:
                raise IndexError(f"段序号 {index} 超出当前段数 {self._count}")
            cursor.insertText(_one_block(text), char_format)
        cursor.endEditBlock()
        self._start_fade(fading)
        self.viewport().update()

    # ------------------------------------------------------------------ 淡入

    def _select_block(self, cursor: QTextCursor, index: int) -> None:
        block = self.document().findBlockByNumber(index)
        cursor.setPosition(block.position())
        cursor.setPosition(block.position() + block.length() - 1, QTextCursor.MoveMode.KeepAnchor)

    def _alpha_format(self, alpha: float) -> QTextCharFormat:
        color = QColor(self.palette().color(QPalette.ColorRole.Text))
        color.setAlphaF(max(0.0, min(1.0, alpha)))
        char_format = QTextCharFormat()
        char_format.setForeground(QBrush(color))
        return char_format

    def _start_fade(self, indices: Iterable[int]) -> None:
        members = {index for index in indices if 0 <= index < self._count}
        if not members:
            return
        for index in members:
            self._leave_fade(index)
        animation = QVariantAnimation(self)
        animation.setDuration(TOKENS.fade_ms)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        animation.valueChanged.connect(partial(self._apply_alpha, animation))
        animation.finished.connect(partial(self._finish_fade, animation))
        self._fade_members[animation] = members
        for index in members:
            self._fade_of[index] = animation
        animation.start()

    def _apply_alpha(self, animation: QVariantAnimation, value: object) -> None:
        members = self._fade_members.get(animation)
        if not members:
            return
        alpha = float(value) if isinstance(value, int | float) else 1.0
        self._format_blocks(members, self._alpha_format(alpha))

    def _finish_fade(self, animation: QVariantAnimation) -> None:
        members = self._fade_members.pop(animation, set[int]())
        for index in members:
            if self._fade_of.get(index) is animation:
                del self._fade_of[index]
        self._format_blocks(members, QTextCharFormat(), replace=True)
        animation.deleteLater()

    def _leave_fade(self, index: int) -> None:
        """让一段离开它所在的淡入组；组空了就停止并释放动画。"""
        animation = self._fade_of.pop(index, None)
        if animation is None:
            return
        members = self._fade_members.get(animation)
        if members is not None:
            members.discard(index)
            if not members:
                del self._fade_members[animation]
                animation.stop()
                animation.deleteLater()

    def _stop_fades(self) -> None:
        for animation in self._fade_members:
            animation.stop()
            animation.deleteLater()
        self._fade_members.clear()
        self._fade_of.clear()

    def _format_blocks(
        self, indices: Iterable[int], char_format: QTextCharFormat, *, replace: bool = False
    ) -> None:
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        for index in indices:
            if index >= self._count:
                continue
            self._select_block(cursor, index)
            if replace:
                cursor.setCharFormat(char_format)
            else:
                cursor.mergeCharFormat(char_format)
        cursor.endEditBlock()
