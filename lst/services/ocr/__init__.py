"""OCR 服务的主进程一侧：OCR 进程客户端、版面结果转换与段落合并（架构文档第 7 章第 4、5 步，8.4 节）。

- config：[ocr] 配置节 OcrConfig，实际生效的识别模式、请求 options、处理时限，以及决定 OCR 进程加载内容的
  process_key（组合根据此判断是否重载 OCR 进程）。
- blocks：OCR 进程返回的 JSON 版面块格式（见该模块文档）与解码 decode_blocks。
- paragraphs：文本行的段落合并器协议 ParagraphMerger、默认实现 GeometricMerger 与阈值 MergeParams。
- segments：版面块到参赛段的转换 blocks_to_segments。
- module：OcrModule，把 CaptureCompleted → OCR 进程 → OcrCompleted / OcrFailed 接到事件总线上。
"""

from lst.contracts.text import is_cjk, join_lines
from lst.services.ocr.blocks import BLOCK_ID_PATTERN, decode_blocks
from lst.services.ocr.config import (
    AUTO_LANGUAGE,
    CONFIG_SECTIONS,
    OcrConfig,
    OcrDevice,
    OcrMode,
    effective_mode,
    process_key,
    processing_timeout,
    request_options,
    worker_settings,
)
from lst.services.ocr.module import OcrModule
from lst.services.ocr.paragraphs import (
    GeometricMerger,
    MergeParams,
    Paragraph,
    ParagraphMerger,
    exact_match_rate,
)
from lst.services.ocr.segments import block_text, blocks_to_segments

__all__ = [
    "AUTO_LANGUAGE",
    "BLOCK_ID_PATTERN",
    "CONFIG_SECTIONS",
    "GeometricMerger",
    "MergeParams",
    "OcrConfig",
    "OcrDevice",
    "OcrMode",
    "OcrModule",
    "Paragraph",
    "ParagraphMerger",
    "block_text",
    "blocks_to_segments",
    "decode_blocks",
    "effective_mode",
    "exact_match_rate",
    "is_cjk",
    "join_lines",
    "process_key",
    "processing_timeout",
    "request_options",
    "worker_settings",
]
