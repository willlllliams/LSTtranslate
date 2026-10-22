"""OCR 识别与本地翻译两条流水线的节点与顺序（需求 UI-05，架构文档 11.7 节）。

每个节点对应代码里一个真实存在的处理步骤，顺序与代码的执行顺序一致。界面模块不能 import services 与 workers
（架构文档 4.2 节），所以这里把顺序写成常量，并在注释里写出每个节点对应的代码位置；
tests/ui/settings/test_flow_pages.py 逐项核对这些常量与代码位置。

OCR 环节节点的 key 取 ``OcrStage`` 的值，与环节卡片、配置节里的 ``<环节>_model`` 字段一一对应。

本模块不依赖 Qt 以外的界面状态，只定义结构与对应的配置字段。
"""

from collections.abc import Mapping
from typing import Final

from lst.contracts.ocr_engines import OcrEngineKind, OcrStage
from lst.ui.settings.flow import FlowBranch, FlowItem, FlowStep, flow_keys

# ---------------------------------------------------------------- OCR：非环节节点的 key

PREPROCESS: Final = "preprocess"
"""截图预处理：按 [ocr].resize_max_side 等比缩小，再按 [ocr].grayscale 灰度化。
paddle_backend.PaddleBackend.recognize、rapid_backend.RapidBackend.recognize、
winocr_backend.WinOcrBackend.recognize 的开头（plan_resize 或 target_size → resize → to_grayscale）。"""
SYSTEM_OCR: Final = "system_ocr"
"""Windows 自带 OCR 一次完成检测与识别：
winocr_backend._WinRtRecognizer.recognize（OcrEngine.recognize_async）。"""
ASSEMBLE: Final = "assemble"
"""把系统 OCR 给出的词拼成文本行：winocr_backend.build_blocks（assemble_line、union_box）。"""
CONVERT: Final = "convert"
"""把引擎输出转成版面块 JSON：PaddleOCR 是 extract.extract_structure / extract_text 加
convert.convert_structure / convert_text，RapidOCR 是 rapid_backend.build_blocks_json。"""
DECODE: Final = "decode"
"""主进程解码并校验版面块：lst/services/ocr/blocks.py 的 decode_blocks（OcrModule._complete 的第一步）。"""
CLEAN: Final = "clean"
"""结构归一化的清洗：lst/services/ocr/normalize.py 的 CleanStep，开关是 [ocr_normalize].clean。"""
SPLIT: Final = "split"
"""拆分跨栏长行：normalize.SplitStep，开关是 [ocr_normalize].split；只在文本模式下起作用。"""
AGGREGATE: Final = "aggregate"
"""文本模式的段落合并：normalize.AggregateStep（paragraphs），开关是 [ocr_normalize].aggregate。"""
MERGE_BLOCKS: Final = "merge_blocks"
"""版面模式的相邻正文块合并：normalize.AggregateStep（merge_blocks），开关是 [ocr_normalize].merge_blocks。"""
SEGMENTS: Final = "segments"
"""把版面块切成翻译用的段：lst/services/ocr/segments.py 的 blocks_to_segments。"""

_MAIN_TEXT: Final[tuple[FlowItem, ...]] = (
    FlowStep(DECODE),
    FlowStep(CLEAN),
    FlowStep(SPLIT),
    FlowStep(AGGREGATE),
    FlowStep(SEGMENTS),
)
"""文本模式下主进程的处理：OcrModule._complete 里 decode_blocks → NormalizePipeline.apply
（build_pipeline 固定按清洗、拆分、聚合的顺序组装）→ blocks_to_segments。"""

_MAIN_STRUCTURE: Final[tuple[FlowItem, ...]] = (
    FlowStep(DECODE),
    FlowStep(CLEAN),
    FlowStep(AGGREGATE),
    FlowStep(MERGE_BLOCKS),
    FlowStep(SEGMENTS),
)
"""版面模式下主进程的处理：先按截图整理文字单元，再按配置合并相邻正文块。"""


def _steps(*keys: str) -> tuple[FlowItem, ...]:
    return tuple(FlowStep(key) for key in keys)


PADDLE_STRUCTURE_FLOW: Final[tuple[FlowItem, ...]] = (
    *_steps(
        PREPROCESS,
        OcrStage.DOC_ORIENTATION.value,
        OcrStage.DOC_UNWARPING.value,
        OcrStage.LAYOUT.value,
        OcrStage.REGION.value,
        OcrStage.FORMULA.value,
        OcrStage.DETECTION.value,
        OcrStage.TEXTLINE_ORIENTATION.value,
        OcrStage.RECOGNITION.value,
        OcrStage.TABLE.value,
        OcrStage.CHART.value,
        CONVERT,
    ),
    *_MAIN_STRUCTURE,
)
"""PaddleOCR 版面模式（PP-StructureV3）。

PaddleOCR 进程里的顺序取自 PaddleX 3.7 的 ``LayoutParsingPipelineV2.predict``
（paddlex/inference/pipelines/layout_parsing/pipeline_v2.py）：文档预处理子产线（先方向分类、再去畸变，
doc_preprocessor/pipeline.py）→ 版面检测 layout_det_model → 区域检测 region_detection_model →
公式识别 formula_recognition_pipeline（识别后把公式区域涂白，再做整页 OCR）→ 整页 OCR general_ocr_pipeline
（文字检测 → 文本行方向 → 文字识别，ocr/pipeline.py）→ 表格识别 table_recognition_pipeline →
图表识别 chart_recognition_model → get_layout_parsing_res 按阅读顺序整理版面块。
表格、公式、图表在代码里是依次执行的，公式在整页 OCR 之前，表格与图表在之后，所以画成一条链而不是分支。
本项目在它前面做截图预处理、在它后面转换结果（PaddleBackend.recognize）。"""

PADDLE_TEXT_FLOW: Final[tuple[FlowItem, ...]] = (
    *_steps(
        PREPROCESS,
        OcrStage.DOC_ORIENTATION.value,
        OcrStage.DOC_UNWARPING.value,
        OcrStage.DETECTION.value,
        OcrStage.TEXTLINE_ORIENTATION.value,
        OcrStage.RECOGNITION.value,
        CONVERT,
    ),
    *_MAIN_TEXT,
)
"""PaddleOCR 文本模式（OCR 产线）：paddle_config.text_config 只配置文档预处理与检测、方向、识别，
PaddleX ``_OCRPipeline.predict``（ocr/pipeline.py）依次执行文档预处理、text_det_model、
textline_orientation_model、text_rec_model；没有版面检测与表格、公式、图表。"""

RAPID_FLOW: Final[tuple[FlowItem, ...]] = (
    *_steps(
        PREPROCESS,
        OcrStage.DETECTION.value,
        OcrStage.TEXTLINE_ORIENTATION.value,
        OcrStage.RECOGNITION.value,
        CONVERT,
    ),
    *_MAIN_TEXT,
)
"""RapidOCR：RapidBackend.recognize 先预处理，再调用 RapidOCR（rapidocr 3.10 的 RapidOCR.__call__ 依次做
文字检测、文本行方向分类 use_cls、文字识别），最后 build_blocks_json 转成一个整页文本块。"""

WINDOWS_FLOW: Final[tuple[FlowItem, ...]] = (
    *_steps(PREPROCESS, SYSTEM_OCR, ASSEMBLE),
    *_MAIN_TEXT,
)
"""Windows 自带 OCR：WinOcrBackend.recognize 先预处理（缩放上限还要不超过 OcrEngine.max_image_dimension），
再交给系统 OCR 一次完成检测与识别，最后把词拼成行（build_blocks）。"""

OCR_FLOW_KEYS: Final[tuple[str, ...]] = (
    PREPROCESS,
    SYSTEM_OCR,
    OcrStage.DOC_ORIENTATION.value,
    OcrStage.DOC_UNWARPING.value,
    OcrStage.LAYOUT.value,
    OcrStage.REGION.value,
    OcrStage.FORMULA.value,
    OcrStage.DETECTION.value,
    OcrStage.TEXTLINE_ORIENTATION.value,
    OcrStage.RECOGNITION.value,
    OcrStage.TABLE.value,
    OcrStage.CHART.value,
    ASSEMBLE,
    CONVERT,
    DECODE,
    CLEAN,
    SPLIT,
    AGGREGATE,
    MERGE_BLOCKS,
    SEGMENTS,
)
"""OCR 引擎子页上可能出现的全部节点，按流程顺序合并。各个流程都是它的子序列。"""


def ocr_flow(kind: OcrEngineKind, *, structure: bool) -> tuple[FlowItem, ...]:
    """引擎的流程。structure 是 [ocr].mode 是否为版面模式，只对 PaddleOCR 有意义。"""
    match kind:
        case OcrEngineKind.PADDLE:
            return PADDLE_STRUCTURE_FLOW if structure else PADDLE_TEXT_FLOW
        case OcrEngineKind.RAPID:
            return RAPID_FLOW
        case OcrEngineKind.WINDOWS:
            return WINDOWS_FLOW


NORMALIZE_SWITCHES: Final[Mapping[str, str]] = {
    CLEAN: "clean",
    SPLIT: "split",
    AGGREGATE: "aggregate",
    MERGE_BLOCKS: "merge_blocks",
}
"""结构归一化节点 → [ocr_normalize] 节里的步骤开关（normalize.build_pipeline 按这些字段决定是否加入步骤）。"""

PARAM_STAGE_PREFIXES: Final[tuple[tuple[str, OcrStage], ...]] = (
    ("text_det_", OcrStage.DETECTION),
    ("det_", OcrStage.DETECTION),
    ("text_rec_", OcrStage.RECOGNITION),
    ("text_score", OcrStage.RECOGNITION),
    ("rec_", OcrStage.RECOGNITION),
    ("cls_", OcrStage.TEXTLINE_ORIENTATION),
    ("layout_", OcrStage.LAYOUT),
)
"""引擎参数按字段名前缀归到环节。字段的 json_schema_extra 带 ocr_stage 时以它为准；
前缀对应后端的用法：PaddleOCR 的 text_det_* 与 layout_* 是 PP-StructureV3 predict 的检测、版面参数，
text_rec_score_thresh 是识别分数阈值；RapidOCR 的 det_*、cls_*、rec_* 分别是 rapidocr 配置的
Det、Cls、Rec 节，text_score 是识别结果的分数阈值。
其余参数（线程数、oneDNN、RapidOCR 的整体尺寸限制）不属于某一环节。"""


# ---------------------------------------------------------------- 本地翻译

LANGID: Final = "langid"
"""语言检测：lst/services/race/module.py 的 _detect 调用 langid.LanguageDetector.detect_many
（文字系统规则 → lingua → 简繁区分）。双语窗口手动指定源语言时跳过。"""
CACHE: Final = "cache"
"""查译文缓存：lst/services/race/scheduler.py 的 _dispatch 先调用 _apply_cache，命中的段不再派发。"""
BATCH: Final = "batch"
"""分批：scheduler._enqueue_pairs 里调用 pack_segments，按引擎实例的 max_batch_segments、max_batch_chars
（local_nmt 默认 16 段、800 字）把同一语言对的段贪心打包。"""
SUBMIT: Final = "submit"
"""提交到本地翻译进程：lst/services/engines/local_nmt/engine.py 的 LocalNmtEngine.translate 把一批段转成
TranslateRequest，经 WorkerChannel 交给监管器；NMT 进程里 handler.TranslateHandler.handle 接收。"""
ROUTE: Final = "route"
"""路线规划：lst/workers/nmt_worker/routing.py 的 plan_route，有直连模型走一跳，否则经英语走两跳。"""
SENTENCES: Final = "sentences"
"""分句：lst/workers/nmt_worker/sentences.py 的 split_sentences（translate_segments 逐段调用）。"""
DIRECT: Final = "direct"
"""直连：路线只有一跳（源语言 → 目标语言），translate_segments 里 for hop in route 执行一次。"""
PIVOT: Final = "pivot"
"""经英语中转：路线有两跳（源语言 → 英语 → 目标语言），第一跳的译文直接送进第二跳，后面四步执行两遍。"""
MODEL: Final = "model"
"""取模型：ct2_backend.Ct2Backend._model 从 LRU 缓存（lru.LruCache，最多常驻 max_loaded_models 个，默认 4）
取直连模型，不在缓存里时 _load_model 解压模型包并创建 ctranslate2.Translator 与 SentencePieceProcessor。"""
TOKENIZE: Final = "tokenize"
"""分词：Ct2Backend.translate_sentences 里 tokenizer.encode(sentences, out_type=str)，
切成 SentencePiece 子词。"""
INFER: Final = "infer"
"""CTranslate2 推理：translator.translate_batch（beam_size、suppress_sequences、decoding_limit）。"""
DETOKENIZE: Final = "detokenize"
"""还原与整理：ct2_backend.detokenize 去掉特殊标记、把 ▁ 换成空格、去掉私用区字符，
中文译文再经 tidy_chinese 整理。"""
JOIN: Final = "join"
"""拼回：sentences.join_sentences 按原文的换行与目标语言的句间分隔把译句拼成一段，
handler 逐段发回 SegmentResult。"""

NMT_FLOW: Final[tuple[FlowItem, ...]] = (
    FlowStep(LANGID),
    FlowStep(CACHE),
    FlowStep(BATCH),
    FlowStep(SUBMIT),
    FlowStep(ROUTE),
    FlowStep(SENTENCES),
    FlowBranch(((DIRECT,), (PIVOT,))),
    FlowStep(MODEL),
    FlowStep(TOKENIZE),
    FlowStep(INFER),
    FlowStep(DETOKENIZE),
    FlowStep(JOIN),
)
"""本地翻译的流程。主进程（lst/services/race）：语言检测（module.py 先 _detect，再把段交给调度器）→
查缓存（scheduler.py 的 _dispatch 先 _apply_cache）→ 分批（_enqueue_pairs 里 pack_segments）→
提交（LocalNmtEngine.translate）。NMT 进程：translate_segments 先 plan_route，再逐段 split_sentences，
按路线逐跳调用 Ct2Backend.translate_sentences（取模型 → 分词 → 推理 → 还原），最后 join_sentences。
路线规划在分句之前，而直连与中转的区别体现在逐跳翻译的次数上，所以分支画在分句之后。"""

NMT_FLOW_KEYS: Final[tuple[str, ...]] = tuple(flow_keys(NMT_FLOW))

__all__ = [
    "AGGREGATE",
    "ASSEMBLE",
    "BATCH",
    "CACHE",
    "CLEAN",
    "CONVERT",
    "DECODE",
    "DETOKENIZE",
    "DIRECT",
    "INFER",
    "JOIN",
    "LANGID",
    "MERGE_BLOCKS",
    "MODEL",
    "NMT_FLOW",
    "NMT_FLOW_KEYS",
    "NORMALIZE_SWITCHES",
    "OCR_FLOW_KEYS",
    "PADDLE_STRUCTURE_FLOW",
    "PADDLE_TEXT_FLOW",
    "PARAM_STAGE_PREFIXES",
    "PIVOT",
    "PREPROCESS",
    "RAPID_FLOW",
    "ROUTE",
    "SEGMENTS",
    "SENTENCES",
    "SPLIT",
    "SUBMIT",
    "SYSTEM_OCR",
    "TOKENIZE",
    "WINDOWS_FLOW",
    "ocr_flow",
]
